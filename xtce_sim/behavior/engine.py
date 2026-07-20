"""Run-time side of behavior: the engine that executes a validated spec.

Owns the overlay of field values that wins over the synthetic layer when
packets are packed, applies command effects, advances continuous
behaviors every beacon tick, and converts engineering-unit values to wire
form at the boundary. Verb-specific math lives with each verb (the
``verbs`` package): the engine dispatches through the Effect and
ActiveBehavior hooks and provides the shared services they call back into
(``_store``, ``_live_number``, ``_noisy``). The full DSL documentation is
the package docstring (``xtce_sim/behavior/__init__.py``).
"""

from __future__ import annotations

import logging
import math
import random
import re
from typing import Optional

from xtce_sim.behavior.spec import _TEMPLATE_RE, ActiveBehavior, BehaviorSpec
from xtce_sim.definition import SimDefinition, label_for
from xtce_sim.dynamics.model import AdcsModel

logger = logging.getLogger("xtce_sim.behavior")


def _field_rng(fname: str) -> random.Random:
    """A per-field RNG with a stable seed: noisy runs are reproducible."""
    return random.Random(f"xtce-sim:{fname}")


class BehaviorEngine:
    """Executes a BehaviorSpec: an overlay of field values driven by commands.

    The overlay maps field name -> wire-ready value (enum labels resolved to
    raw ints, strings encoded to bytes) and wins over the synthetic layer
    when a packet is packed. All effect application is loud-but-liberal:
    a value that cannot be applied is warned about and skipped — a behavior
    problem must never take down the beacon or a command dispatch.
    """

    def __init__(self, spec: BehaviorSpec, simdef: SimDefinition):
        self.spec = spec
        self._fields = {f.name: f for p in simdef.packets for f in p.fields}
        self._params = {c.name: {p.name: p for p in c.params} for c in simdef.commands}
        self.state: dict[str, object] = {}
        self._field_apid = {f.name: p.apid for p in simdef.packets for f in p.fields}
        # APIDs whose packets should be emitted out-of-cycle because a just-
        # applied effect was marked emit = "immediate". The server drains this
        # via pop_immediate_apids() after each apply_command.
        self._pending_immediate: set[int] = set()
        # Active continuous behaviors (ramps/oscillations/holds), keyed by
        # resolved field name — newest replaces oldest.
        self._behaviors: dict[str, ActiveBehavior] = {}
        # One RNG per field for the engine's lifetime: restarting a behavior
        # continues its noise stream instead of replaying it, while separate
        # runs still reproduce (the seed is stable per field).
        self._rngs: dict[str, random.Random] = {}
        # Fields owned by [_models] outputs. Load rejects literal writers;
        # this set backs the runtime guard for TEMPLATE-resolved writers,
        # which load validation cannot see.
        self._model_owned = {f for cfg in spec.models for f in cfg.outputs}
        for fname, value in spec.initial.items():
            self._store(f"[_initial] {fname}", fname, value)
        # Boot signals: ambient behaviors running from t=0, no command needed.
        # The loader only emits continuous effects here; gate anyway so a
        # hand-built spec can't crash the constructor with a discrete one.
        for eff in spec.signals:
            if eff.continuous:
                self._start_behavior(None, eff, {})
            else:
                logger.warning("[_signals] %s: not a continuous behavior; skipped", eff.field)
        # Physics models: instantiate, route their commands, seed outputs so
        # the very first beacon already carries a live attitude.
        self.models = [AdcsModel(cfg) for cfg in spec.models]
        self._model_by_command = {
            name: model for model in self.models for name in model.config.commands.values()
        }
        for model in self.models:
            self._store_model_outputs(model)

    # ---- packing side ------------------------------------------------------

    def values_for(self, packet) -> dict:
        """The overlay entries belonging to one packet (merge over synth)."""
        return {f.name: self.state[f.name] for f in packet.fields if f.name in self.state}

    # ---- command side ------------------------------------------------------

    def apply_command(self, command, args: dict) -> list[str]:
        """Apply a command's effects to the overlay.

        ``args`` is the decoded argument dict (enum arguments arrive as
        labels, exactly as ``codec.decode_command`` produces them). Returns
        human-readable descriptions of the effects applied, for the server
        log. Ramps register as active behaviors advanced by ``tick()``; a
        new ramp on a field replaces any earlier one (HEATER_OFF's cooling
        displaces HEATER_ON's warming).
        """
        applied: list[str] = []
        # A fresh start per command: nothing left over from an earlier apply
        # that errored before its APIDs were drained.
        self._pending_immediate.clear()
        model = self._model_by_command.get(command.name)
        if model is not None:
            results = model.apply_command(command.name, args)
            if results:
                applied.extend(results)
                self._store_model_outputs(model)
                # The operator just steered the vehicle: show the result now,
                # not a beacon interval later. A rejected command emits
                # nothing — an out-of-cycle ADCS burst must always mean
                # "the command took effect".
                self._pending_immediate |= {self._field_apid[f] for f in model.config.outputs}
        for eff in self.spec.commands.get(command.name, []):
            if eff.continuous:
                desc = self._start_behavior(command, eff, args)
            else:
                desc = self._apply_effect(command, eff, args)
            if desc is not None:
                applied.append(desc)
        return applied

    def _start_behavior(self, command, eff, args: dict) -> Optional[str]:
        """Register a continuous behavior, resolving templates now.

        ``command`` is None for boot signals (whose loader forbids templates,
        so resolution is a no-op there).
        """
        origin = command.name if command is not None else "_signals"
        where = f"[{origin}] {eff.field}"
        fname = self._resolve_template(where, eff.field, command, args)
        if fname is None:
            return None
        if fname in self._model_owned:
            # Template-resolved landing on a model output: the load-time
            # ownership check cannot see templates, so enforce here.
            logger.warning("%s: %s is owned by a model; skipped", where, fname)
            return None
        if self._fields[fname].python_type in ("string", "bytes"):
            # A continuous behavior on a text field would warn every tick
            # forever (it never retires); refuse it once instead.
            logger.warning("%s: %s is not a numeric field; skipped", where, fname)
            return None
        cal = self._fields[fname].calibrator
        if cal is not None and not cal.is_invertible:
            # Deferred-template case: load validation could not see this
            # concrete field. Refuse once, with the real reason.
            logger.warning("%s: %s has a non-invertible calibrator; skipped", where, fname)
            return None
        ref = eff.reference
        if isinstance(ref, str):  # "@FIELD", possibly templated
            resolved = self._resolve_template(where, ref[1:], command, args)
            if resolved is None:
                return None
            if resolved == fname:  # template args can make @ref land on itself
                logger.warning("%s: reference @%s names its own field; skipped", where, fname)
                return None
            ref = f"@{resolved}"
        rng = self._rngs.setdefault(fname, _field_rng(fname)) if eff.noise else None
        self._behaviors[fname] = eff.make_active(fname, ref, rng)
        return f"{fname} {eff.describe_active(ref)}"

    def tick(self, dt: float) -> None:
        """Advance every active behavior by *dt* seconds.

        Ramps use the closed-form first-order step, so the curve is exact
        for any tick size — a 5-second beacon interval samples the same
        trajectory a 0.5-second one does. An @FIELD target is re-read every
        tick, so changing a setpoint mid-ramp bends the curve.
        """
        if dt <= 0:
            return
        # Iterate a snapshot: completed ramps delete themselves mid-loop.
        snapshot = self._behaviors.copy()
        for beh in snapshot.values():
            beh.advance(self, dt)
        for model in self.models:
            model.advance(dt)
            self._store_model_outputs(model)

    def _store_model_outputs(self, model: AdcsModel) -> None:
        where = f"[_models.{model.config.name}]"
        for fname, value in model.outputs().items():
            self._store(where, fname, value)

    @staticmethod
    def _noisy(beh, value: float) -> float:
        return value + beh.rng.gauss(0.0, beh.noise) if beh.rng else value

    def _live_number(self, beh, ref) -> Optional[float]:
        """A behavior's number-or-@FIELD reference, resolved right now.

        The overlay stores wire counts; behavior math runs in engineering
        units, so calibrated fields convert on the way out.
        """
        if isinstance(ref, str):  # "@FIELD"
            value = self.state.get(ref[1:])
            if not isinstance(value, (int, float)):
                if not beh.warned:  # once per behavior, not once per tick
                    beh.warned = True
                    logger.warning(
                        "%s: reference %s has no numeric value yet; holding "
                        "(suppressing further warnings for this behavior)",
                        beh.field,
                        ref,
                    )
                return None
            beh.warned = False
            return float(self._engineering(ref[1:], value))
        return float(ref)

    def _ramp_current(self, fname: str) -> float:
        current = self.state.get(fname, 0)
        if not isinstance(current, (int, float)):
            return 0.0
        return float(self._engineering(fname, current))

    def _engineering(self, fname: str, value):
        """A stored wire count as its engineering value (identity when
        the field has no calibrator)."""
        f = self._fields.get(fname)
        if f is not None and f.calibrator is not None and isinstance(value, (int, float)):
            return f.calibrator.apply(value)
        return value

    def _apply_effect(self, command, eff, args: dict) -> Optional[str]:
        where = f"[{command.name}] {eff.field}"
        fname = self._resolve_template(where, eff.field, command, args)
        if fname is None:
            return None
        if fname in self._model_owned:
            # Template-resolved landing on a model output: the load-time
            # ownership check cannot see templates, so enforce here.
            logger.warning("%s: %s is owned by a model; skipped", where, fname)
            return None
        cal = self._fields[fname].calibrator
        if cal is not None and not cal.is_invertible:
            # Deferred-template / copy case load validation could not see.
            logger.warning("%s: %s has a non-invertible calibrator; skipped", where, fname)
            return None
        value = eff.value_for(self, command, args, where, fname)
        if value is None:
            return None
        stored = self._store(where, fname, value)
        if stored is None:
            return None
        # Last command wins: an explicit set/copy/increment on a field with an
        # active behavior cancels it — otherwise the next tick would silently
        # revert the write.
        if self._behaviors.pop(fname, None) is not None:
            logger.debug("[behavior] %s cancelled by direct write", fname)
        if eff.emit == "immediate":
            self._pending_immediate.add(self._field_apid[fname])
        if cal is not None:
            # The operator commanded engineering units; confirm in kind.
            return f"{fname}={self._engineering(fname, stored)!r} ({stored} counts)"
        return f"{fname}={stored!r}"

    def pop_immediate_apids(self) -> set[int]:
        """APIDs needing an out-of-cycle emission, cleared on read.

        Several immediate effects landing in one packet yield that packet's
        APID once; a skipped effect contributes nothing.
        """
        apids, self._pending_immediate = self._pending_immediate, set()
        return apids

    def _resolve_template(self, where: str, template: str, command, args: dict) -> Optional[str]:
        """Fill {Arg} placeholders (enum labels, raw ints); None on failure."""

        def sub(match: re.Match) -> str:
            return self._template_arg(command, args, match.group(1))

        try:
            fname = _TEMPLATE_RE.sub(sub, template)
        except LookupError as exc:
            logger.warning("%s: template %s; skipped", where, exc)
            return None
        if fname not in self._fields:
            logger.warning("%s: resolved field %r does not exist; skipped", where, fname)
            return None
        return fname

    def _template_arg(self, command, args: dict, name: str) -> str:
        """The substitution text for one ``{Arg}``: enum label, else raw value.

        Mirrors the load-time expansion rule in ``_arg_values``. Raises
        LookupError when the argument is absent, its raw value has no
        declared label, or a string is not a declared label — the template
        cannot honestly name a field, so the caller skips the effect.
        """
        if name not in args:
            raise LookupError(f"argument {name!r} missing")
        value = args[name]
        param = self._params.get(command.name, {}).get(name)
        if param is not None and param.enumerations:
            if isinstance(value, str):
                # The codec decodes enum args to labels, but direct callers
                # can pass anything — a string that is not a declared label
                # must not name a field this command cannot legally address.
                if value not in param.enumerations:
                    raise LookupError(f"{{{name}}}: {value!r} is not a declared label")
                return value
            label = label_for(param.enumerations, value)
            if label is None:
                raise LookupError(f"{{{name}}}: raw value {value!r} has no label")
            return label
        # Integral floats substitute like the integers load-time expansion
        # produces ('2', never '2.0'), keeping the two rules in step.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    def _raw_arg(self, command, args: dict, name: str):
        """A decoded argument as its raw wire value (labels back to ints)."""
        if name not in args:
            raise KeyError(name)
        value = args[name]
        param = self._params.get(command.name, {}).get(name)
        if isinstance(value, str) and param is not None and param.enumerations:
            return param.enumerations.get(value, value)
        return value

    def _store(self, where: str, fname: str, value) -> Optional[object]:
        """Coerce a value to wire form and write it into the overlay.

        This is the low-level write used by ticks and seeding; it does NOT
        cancel an active behavior on the field. A write that should count as
        "the operator set this" must go through _apply_effect, or the field's
        behavior will silently revert it on the next tick.
        """
        field = self._fields[fname]
        wire = _wire_value(field, value)
        if wire is None:
            logger.warning(
                "%s: value %r does not fit field %s (%s); skipped",
                where,
                value,
                fname,
                field.python_type,
            )
            return None
        self.state[fname] = wire
        return wire


def _wire_value(field, value) -> Optional[object]:
    """Coerce a behavior value to what struct packing expects, else None.

    Enum labels resolve through the field's enumeration; strings encode to
    bytes for string fields; numeric fields get ints (rounded and clamped to
    the wire width) or floats. Unresolvable values return None.
    """
    if isinstance(value, (str, bytes, bytearray)):
        return _wire_text(field, value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # nan/inf can arrive via a copied float argument
    if field.python_type in ("string", "bytes"):
        return None
    if field.calibrator is not None:
        # Behavior values are ENGINEERING units; the wire carries raw
        # counts. Convert here, at the one boundary where values become
        # wire-ready. (Non-invertible calibrators are rejected at load.)
        raw = field.calibrator.invert(float(value))
        if raw is None:
            return None
        value = raw
    if field.python_type.startswith("float"):
        return float(value)
    return _clamp_int(field, int(round(value)))


def _wire_text(field, value) -> Optional[object]:
    """The wire form of a str/bytes behavior value, else None."""
    if isinstance(value, str):
        if field.enumerations and value in field.enumerations:
            return field.enumerations[value]
        if field.python_type in ("string", "bytes"):
            return value.encode()
        return None
    return value if field.python_type in ("string", "bytes") else None


def _clamp_int(field, value: int) -> int:
    """Clamp an integer to the field's wire range so packing never overflows."""
    bits = field.size_bits or 8
    if field.python_type.startswith("u"):
        lo, hi = 0, (1 << bits) - 1
    else:
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return max(lo, min(hi, value))
