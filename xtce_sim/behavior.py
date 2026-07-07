"""Declarative command→telemetry behavior, loaded from a TOML sidecar.

The XTCE defines the command/telemetry *interface*; a behavior file defines
what each command *does* to telemetry. It lives next to the XTCE as
``<stem>.behavior.toml`` (or wherever ``--behavior`` points) and contains one
table per command plus an optional ``[_initial]`` table of start-up values:

    [_initial]
    THM_HEATER1_TEMP = 20.0

    [HEATER_ON]
    "THM_HEATER{HeaterId}_STATE" = 1                     # set
    "THM_HEATER{HeaterId}_TEMP" = { ramp_to = "@THM_HEATER{HeaterId}_SETPOINT", tau = 30.0 }

    [SET_EXPOSURE]
    IMG_EXPOSURE_MS = "@arg:ExposureMs"                  # copy an argument

Verbs: a bare scalar sets the field; ``"@arg:Name"`` copies a command
argument; ``{ increment = n }`` adds; ``{ ramp_to = X, tau = S }`` starts a
first-order approach toward X (a number, or ``"@FIELD"`` read live each
tick). ``{ArgName}`` inside a field name or ``@`` target is filled with the
argument's decoded **raw integer** value at execution time (an enumerated
argument substitutes its raw value, not its label). Any effect may carry
``emit = "immediate"`` to request out-of-cycle emission of its packet
(reserved; the fast path is a later feature) — for a copy that is written
``{ set = "@arg:Name", emit = "immediate" }``. Booleans are rejected as
values: write ``0``/``1`` or an enum label.

Validation is strict and total: every command table, field name, argument
reference, enum label, and verb key is checked against the resolved
SimDefinition, and *all* problems are reported in one BehaviorError.

BehaviorEngine executes a loaded spec at runtime: it keeps an overlay of
field values that wins over the synthetic generator when packets are packed,
applies set/copy/increment effects when commands execute, and seeds the
``[_initial]`` values at start. Ramps are parsed and validated but not yet
executed (the tick engine is a separate feature).
"""

from __future__ import annotations

import itertools
import logging
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from xtce_sim.definition import CommandDef, SimDefinition

_TEMPLATE_RE = re.compile(r"\{(\w+)\}")
_EMIT_VALUES = ("interval", "immediate")
_VERB_KEYS = {"set", "ramp_to", "tau", "increment", "emit"}
# Templated args are expanded for load-time validation up to this many
# combinations; beyond it (or for unbounded args) field checks defer to
# execution time.
_MAX_EXPANSIONS = 100

Scalar = int | float | bool | str


class BehaviorError(ValueError):
    """A behavior file failed validation; the message lists every problem."""


@dataclass
class SetEffect:
    field: str  # possibly templated
    value: Scalar  # number/bool, enum label, or string payload
    emit: str = "interval"


@dataclass
class CopyArgEffect:
    field: str
    arg: str
    emit: str = "interval"


@dataclass
class IncrementEffect:
    field: str
    by: float
    emit: str = "interval"


@dataclass
class RampEffect:
    field: str
    target: float | str  # number, or "@FIELD" (possibly templated)
    tau: float
    emit: str = "interval"


Effect = SetEffect | CopyArgEffect | IncrementEffect | RampEffect


@dataclass
class BehaviorSpec:
    """A validated behavior file: initial values plus per-command effects."""

    path: Path
    initial: dict[str, Scalar]
    commands: dict[str, list[Effect]]  # command name -> effects


def sidecar_path(xtce_paths: list[Path]) -> Optional[Path]:
    """The conventional sidecar for a set of XTCE files, if one exists.

    Named after the first file: ``my_vehicle.xml`` -> ``my_vehicle.behavior.toml``.
    (``with_name`` on the stem, so a dotted stem like ``v1.2.xml`` maps to
    ``v1.2.behavior.toml`` rather than being truncated.)
    """
    if not xtce_paths:
        return None
    first = Path(xtce_paths[0])
    candidate = first.with_name(first.stem + ".behavior.toml")
    return candidate if candidate.exists() else None


def load_behavior(path: Path, simdef: SimDefinition) -> BehaviorSpec:
    """Parse and fully validate a behavior file against a definition.

    Raises BehaviorError listing every problem found (all-or-nothing, like
    the sequence-file parser: a file with any error is rejected whole).
    """
    with open(path, "rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise BehaviorError(f"{path}: not valid TOML: {exc}") from exc

    ctx = _Context(simdef)
    initial: dict[str, Scalar] = {}
    commands: dict[str, list[Effect]] = {}

    for table, body in data.items():
        if table == "_initial":
            initial = _load_initial(body, ctx)
            continue
        command = simdef.command_by_name(table)
        if command is None:
            ctx.error(f"[{table}]: unknown command (not in the definition)")
            continue
        commands[table] = _load_command_table(table, body, command, ctx)

    if ctx.errors:
        problems = "\n  - ".join(ctx.errors)
        raise BehaviorError(
            f"{path}: {len(ctx.errors)} problem(s):\n  - {problems}"
        )
    return BehaviorSpec(path=Path(path), initial=initial, commands=commands)


def describe(spec: BehaviorSpec) -> list[str]:
    """Human-readable narration of a behavior spec, one line per fact."""
    lines = []
    if spec.initial:
        lines.append(f"initial values: {len(spec.initial)} field(s)")
        for fname, value in spec.initial.items():
            lines.append(f"  {fname} = {value!r}")
    for cmd, effects in spec.commands.items():
        lines.append(f"{cmd}:")
        for eff in effects:
            lines.append(f"  {_describe_effect(eff)}")
    return lines


def _describe_effect(eff: Effect) -> str:
    tail = "  [emit: immediate]" if eff.emit == "immediate" else ""
    if isinstance(eff, SetEffect):
        return f"{eff.field} = {eff.value!r}{tail}"
    if isinstance(eff, CopyArgEffect):
        return f"{eff.field} = @arg:{eff.arg}{tail}"
    if isinstance(eff, IncrementEffect):
        return f"{eff.field} += {eff.by}{tail}"
    return f"{eff.field} ramps to {eff.target} (tau={eff.tau}s){tail}"


# ---------------------------------------------------------------------------
# validation internals
# ---------------------------------------------------------------------------


class _Context:
    """Validation scratchpad: the definition's lookup tables + error list."""

    def __init__(self, simdef: SimDefinition):
        self.fields = {f.name: f for p in simdef.packets for f in p.fields}
        self.errors: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def is_numeric(self, field_name: str) -> bool:
        f = self.fields.get(field_name)
        return f is not None and f.python_type not in ("string", "bytes")


def _load_initial(body, ctx: _Context) -> dict[str, Scalar]:
    initial: dict[str, Scalar] = {}
    if not isinstance(body, dict):
        ctx.error("[_initial]: must be a table of FIELD = value")
        return initial
    for fname, value in body.items():
        if _TEMPLATE_RE.search(fname):
            ctx.error(
                f"[_initial] {fname}: templates are not allowed here — "
                "there is no command context; list each field explicitly"
            )
            continue
        if fname not in ctx.fields:
            ctx.error(f"[_initial] {fname}: unknown telemetry field")
            continue
        _check_scalar_for_field(f"[_initial] {fname}", fname, value, None, ctx)
        initial[fname] = value
    return initial


def _load_command_table(
    table: str, body, command: CommandDef, ctx: _Context
) -> list[Effect]:
    effects: list[Effect] = []
    if not isinstance(body, dict):
        ctx.error(f"[{table}]: must be a table of FIELD = effect")
        return effects
    for fname, spec in body.items():
        where = f"[{table}] {fname}"
        _check_field_template(where, fname, command, ctx)
        eff = _parse_effect(where, fname, spec, command, ctx)
        if eff is not None:
            effects.append(eff)
    return effects


def _parse_effect(
    where: str, fname: str, spec, command: CommandDef, ctx: _Context
) -> Optional[Effect]:
    if isinstance(spec, dict):
        return _parse_effect_table(where, fname, spec, command, ctx)
    return _scalar_effect(where, fname, spec, command, ctx, "interval")


def _scalar_effect(
    where: str, fname: str, value, command: CommandDef, ctx: _Context, emit: str
) -> Optional[Effect]:
    """A set-or-copy from a scalar value (bare form, or the table 'set' key)."""
    if isinstance(value, str) and value.startswith("@arg:"):
        arg = value[len("@arg:"):]
        if not _has_arg(command, arg):
            ctx.error(f"{where}: @arg:{arg} — command has no argument {arg!r}")
            return None
        return CopyArgEffect(field=fname, arg=arg, emit=emit)
    if isinstance(value, str) and value.startswith("@"):
        ctx.error(
            f"{where}: {value!r} — did you mean \"@arg:...\"? "
            "(@FIELD references are only valid as ramp_to targets)"
        )
        return None
    _check_scalar_for_field(where, fname, value, command, ctx)
    return SetEffect(field=fname, value=value, emit=emit)


def _parse_effect_table(
    where: str, fname: str, spec: dict, command: CommandDef, ctx: _Context
) -> Optional[Effect]:
    unknown = set(spec) - _VERB_KEYS
    if unknown:
        ctx.error(f"{where}: unknown key(s) {sorted(unknown)}; valid: {sorted(_VERB_KEYS)}")
        return None
    emit = spec.get("emit", "interval")
    if emit not in _EMIT_VALUES:
        ctx.error(f"{where}: emit must be one of {_EMIT_VALUES}, got {emit!r}")
        return None
    verbs = [k for k in ("set", "ramp_to", "increment") if k in spec]
    if len(verbs) != 1:
        ctx.error(f"{where}: exactly one of set/ramp_to/increment required, got {verbs}")
        return None
    if "tau" in spec and verbs[0] != "ramp_to":
        ctx.error(f"{where}: tau is only valid with ramp_to")
        return None
    if verbs[0] == "set":
        return _scalar_effect(where, fname, spec["set"], command, ctx, emit)
    if verbs[0] == "increment":
        return _parse_increment(where, fname, spec, command, emit, ctx)
    return _parse_ramp(where, fname, spec, command, emit, ctx)


def _parse_increment(
    where: str, fname: str, spec: dict, command: CommandDef, emit: str, ctx: _Context
) -> Optional[IncrementEffect]:
    by = spec["increment"]
    if isinstance(by, bool) or not isinstance(by, (int, float)):
        ctx.error(f"{where}: increment must be a number, got {by!r}")
        return None
    _check_numeric_field(where, fname, command, ctx)
    return IncrementEffect(field=fname, by=by, emit=emit)


def _parse_ramp(
    where: str, fname: str, spec: dict, command: CommandDef, emit: str, ctx: _Context
) -> Optional[RampEffect]:
    if "tau" not in spec:
        ctx.error(f"{where}: ramp_to requires tau (time constant in seconds)")
        return None
    tau = spec["tau"]
    if isinstance(tau, bool) or not isinstance(tau, (int, float)) or tau <= 0:
        ctx.error(f"{where}: tau must be a positive number, got {tau!r}")
        return None
    target = spec["ramp_to"]
    if isinstance(target, str):
        if not target.startswith("@"):
            ctx.error(f"{where}: ramp_to string target must be an @FIELD reference")
            return None
        _check_field_template(where, target[1:], command, ctx, numeric=True)
    elif isinstance(target, bool) or not isinstance(target, (int, float)):
        ctx.error(f"{where}: ramp_to must be a number or an @FIELD reference")
        return None
    else:
        target = float(target)
    _check_numeric_field(where, fname, command, ctx)
    return RampEffect(field=fname, target=target, tau=float(tau), emit=emit)


def _has_arg(command: CommandDef, name: str) -> bool:
    return any(p.name == name for p in command.params)


def _check_numeric_field(
    where: str, template: str, command: Optional[CommandDef], ctx: _Context
) -> None:
    """Every load-time expansion of *template* must be a numeric field."""
    for concrete in _expansions(template, command) or []:
        if concrete in ctx.fields and not ctx.is_numeric(concrete):
            ctx.error(f"{where}: {concrete} is not a numeric field")


def _check_scalar_for_field(
    where: str, template: str, value, command: Optional[CommandDef], ctx: _Context
) -> None:
    """A set/initial value must fit the field: label for enums, type-compatible."""
    if isinstance(value, dict):
        ctx.error(f"{where}: unexpected table value")
        return
    if isinstance(value, bool):
        ctx.error(f"{where}: boolean values are ambiguous — use 0/1 or an enum label")
        return
    if isinstance(value, float) and not math.isfinite(value):
        ctx.error(f"{where}: value must be finite, got {value!r}")
        return
    for concrete in _expansions(template, command) or []:
        f = ctx.fields.get(concrete)
        if f is not None:
            _check_scalar_against(where, concrete, f, value, ctx)


def _check_scalar_against(where: str, concrete: str, f, value, ctx: _Context) -> None:
    """Type/label fit of one scalar against one concrete field."""
    if isinstance(value, str):
        if f.enumerations is not None:
            if value not in f.enumerations:
                ctx.error(
                    f"{where}: {value!r} is not a label of {concrete} "
                    f"(valid: {sorted(f.enumerations)})"
                )
        elif f.python_type not in ("string", "bytes"):
            ctx.error(f"{where}: string value for numeric field {concrete}")
        return
    if f.python_type in ("string", "bytes"):
        ctx.error(f"{where}: numeric value for string field {concrete}")
    elif f.enumerations is not None and value not in f.enumerations.values():
        ctx.error(
            f"{where}: {value!r} is not a raw value of {concrete} "
            f"(valid: {sorted(f.enumerations.values())} or a label)"
        )


def _check_field_template(
    where: str,
    template: str,
    command: CommandDef,
    ctx: _Context,
    *,
    numeric: bool = False,
) -> None:
    """Validate a (possibly templated) field name against the definition.

    Template arguments must exist on the command. When every templated
    argument has a small finite value set (enumeration or integer range),
    all expansions are checked against the known fields; otherwise the
    field-existence check defers to execution time.
    """
    args = _TEMPLATE_RE.findall(template)
    for arg in args:
        if not _has_arg(command, arg):
            ctx.error(f"{where}: template argument {{{arg}}} — command has no argument {arg!r}")
            return
    expansions = _expansions(template, command)
    if expansions is None:
        return  # unbounded template: checked at execution time
    for concrete in expansions:
        if concrete not in ctx.fields:
            ctx.error(f"{where}: unknown telemetry field {concrete!r}")
        elif numeric and not ctx.is_numeric(concrete):
            ctx.error(f"{where}: {concrete} is not a numeric field")


def _expansions(template: str, command: Optional[CommandDef]) -> Optional[list[str]]:
    """All concrete field names a template can produce, or None if unbounded.

    A plain (untemplated) name expands to itself. Values come from each
    templated argument's enumeration or small integer ValidRange.
    """
    # Dedupe: the same {Arg} used twice in one name is one variable (both
    # occurrences get the same value), not two independent product axes.
    args = list(dict.fromkeys(_TEMPLATE_RE.findall(template)))
    if not args:
        return [template]
    if command is None:
        return None
    value_sets = []
    for arg in args:
        values = _arg_values(command, arg)
        if values is None:
            return None
        value_sets.append(values)
    if _product_size(value_sets) > _MAX_EXPANSIONS:
        return None
    names = []
    for combo in itertools.product(*value_sets):
        name = template
        for arg, val in zip(args, combo):
            name = name.replace("{" + arg + "}", str(val))
        names.append(name)
    return names


def _arg_values(command: CommandDef, arg: str) -> Optional[list[int]]:
    """The finite value set of a command argument, or None if unbounded."""
    p = next((p for p in command.params if p.name == arg), None)
    if p is None:
        return None
    if p.enumerations:
        return sorted(set(p.enumerations.values()))
    if p.valid_min is not None and p.valid_max is not None:
        # ceil/floor so a float range like 1.5..2.7 yields only the integers
        # actually inside it (2), not truncation artifacts at the edges.
        lo, hi = math.ceil(p.valid_min), math.floor(p.valid_max)
        if lo <= hi and (hi - lo) < _MAX_EXPANSIONS:
            return list(range(lo, hi + 1))
    return None


def _product_size(value_sets: list[list[int]]) -> int:
    size = 1
    for values in value_sets:
        size *= max(1, len(values))
    return size


# ---------------------------------------------------------------------------
# runtime engine
# ---------------------------------------------------------------------------

logger = logging.getLogger("xtce_sim.behavior")


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
        self._params = {
            c.name: {p.name: p for p in c.params} for c in simdef.commands
        }
        self.state: dict[str, object] = {}
        for fname, value in spec.initial.items():
            self._store(f"[_initial] {fname}", fname, value)

    # ---- packing side ------------------------------------------------------

    def values_for(self, packet) -> dict:
        """The overlay entries belonging to one packet (merge over synth)."""
        return {
            f.name: self.state[f.name] for f in packet.fields if f.name in self.state
        }

    # ---- command side ------------------------------------------------------

    def apply_command(self, command, args: dict) -> list[str]:
        """Apply a command's effects to the overlay.

        ``args`` is the decoded argument dict (enum arguments arrive as
        labels, exactly as ``codec.decode_command`` produces them). Returns
        human-readable descriptions of the effects applied, for the server
        log. Ramps are counted but not executed yet.
        """
        applied: list[str] = []
        pending_ramps = 0
        for eff in self.spec.commands.get(command.name, []):
            if isinstance(eff, RampEffect):
                pending_ramps += 1
                continue
            desc = self._apply_effect(command, eff, args)
            if desc is not None:
                applied.append(desc)
        if pending_ramps:
            applied.append(f"{pending_ramps} ramp(s) declared (engine pending)")
        return applied

    def _apply_effect(self, command, eff, args: dict) -> Optional[str]:
        where = f"[{command.name}] {eff.field}"
        fname = self._resolve_template(where, eff.field, command, args)
        if fname is None:
            return None
        if isinstance(eff, SetEffect):
            value = eff.value
        elif isinstance(eff, CopyArgEffect):
            if eff.arg not in args:
                logger.warning("%s: argument %s missing from decode; skipped", where, eff.arg)
                return None
            # Same raw-value rule as templates: an enum argument arrives from
            # decode as its label; store its raw value (the destination
            # field's own enum may use different labels entirely).
            value = self._raw_arg(command, args, eff.arg)
        else:  # IncrementEffect
            current = self.state.get(fname, 0)
            value = (current if isinstance(current, (int, float)) else 0) + eff.by
        stored = self._store(where, fname, value)
        return None if stored is None else f"{fname}={stored!r}"

    def _resolve_template(
        self, where: str, template: str, command, args: dict
    ) -> Optional[str]:
        """Fill {Arg} placeholders with raw argument values; None on failure."""
        def sub(match: re.Match) -> str:
            return str(self._raw_arg(command, args, match.group(1)))

        try:
            fname = _TEMPLATE_RE.sub(sub, template)
        except KeyError as exc:
            logger.warning("%s: template argument %s missing; skipped", where, exc)
            return None
        if fname not in self._fields:
            logger.warning("%s: resolved field %r does not exist; skipped", where, fname)
            return None
        return fname

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
        """Coerce a value to wire form and write it into the overlay."""
        field = self._fields[fname]
        wire = _wire_value(field, value)
        if wire is None:
            logger.warning(
                "%s: value %r does not fit field %s (%s); skipped",
                where, value, fname, field.python_type,
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
    if isinstance(value, str):
        if field.enumerations and value in field.enumerations:
            return field.enumerations[value]
        if field.python_type in ("string", "bytes"):
            return value.encode()
        return None
    if isinstance(value, (bytes, bytearray)):
        return value if field.python_type in ("string", "bytes") else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # nan/inf can arrive via a copied float argument
    if field.python_type in ("string", "bytes"):
        return None
    if field.python_type.startswith("float"):
        return float(value)
    return _clamp_int(field, int(round(value)))


def _clamp_int(field, value: int) -> int:
    """Clamp an integer to the field's wire range so packing never overflows."""
    bits = field.size_bits or 8
    if field.python_type.startswith("u"):
        lo, hi = 0, (1 << bits) - 1
    else:
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return max(lo, min(hi, value))
