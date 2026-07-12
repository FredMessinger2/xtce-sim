"""Declarative command→telemetry behavior, loaded from a TOML sidecar.

The XTCE defines the command/telemetry *interface*; behavior TOML defines
what each command *does* to telemetry. A satellite is a directory: every
``.toml`` beside the XTCE is merged (or ``--behavior`` points at a directory
or single file). Files contain one table per command plus optional
``[_initial]`` start-up values and ``[_signals]`` boot behaviors:

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
tick); ``{ oscillate = C, amplitude = A, period = P }`` runs a continuous
wave around center C (``shape`` = "sine"/"triangle"/"sawtooth", optional
``phase`` seconds); ``{ hold = V }`` keeps re-asserting V. An ``@FIELD``
reference must not name the field itself — feeding a field its own output
turns noise/waves into unbounded drift — so literal self-references are
load errors and a template that resolves to its own field is skipped at
execution. Continuous verbs (ramp_to/oscillate/hold) accept ``noise =
stddev`` — one seeded RNG per field per engine, so separate runs reproduce
each other while a restarted behavior continues its stream — and a
completed noisy ramp degrades into a noisy hold at its target. An optional ``[_signals]`` table starts continuous
behaviors at boot (ambient realism: orbit thermal cycles, bus ripple) with
no command needed; a command's behavior on the same field replaces a
signal, and a direct set cancels it, exactly like ramps. ``{ArgName}`` inside a field name or ``@`` target is filled with the
argument's decoded **raw integer** value at execution time (an enumerated
argument substitutes its raw value, not its label). An instant effect
(set/copy/increment) may carry ``emit = "immediate"``: the packet containing
the field is emitted out-of-cycle the moment the command executes, while the
beacon keeps its own schedule — for a copy that is written
``{ set = "@arg:Name", emit = "immediate" }``. Continuous verbs reject it
(they pace with the beacon by nature). Booleans are rejected as values:
write ``0``/``1`` or an enum label.

Behavior values are ENGINEERING UNITS. A field whose XTCE declares a
calibrator transmits raw counts, but the sidecar speaks the calibrated
meaning — a setpoint of 25.5 means degrees, not counts — and the engine
converts at the wire boundary (inverting on write, calibrating on read for
``@FIELD`` references and increments). A behavior-governed calibrated field
therefore needs an invertible calibrator (affine polynomial or monotonic
spline); anything else is a load error.

Validation is strict and total: every command table, field name, argument
reference, enum label, and verb key is checked against the resolved
SimDefinition, and *all* problems are reported in one BehaviorError.

BehaviorEngine executes a loaded spec at runtime: it keeps an overlay of
field values that wins over the synthetic generator when packets are packed,
applies set/copy/increment effects when commands execute, seeds the
``[_initial]`` values at start, and advances active ramps each beacon tick
(closed-form first-order step, so trajectories are identical at any tick
size; ``@FIELD`` targets are re-read live; a new ramp on a field replaces
the old one).
"""

from __future__ import annotations

import itertools
import logging
import math
import random
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from xtce_sim.definition import CommandDef, SimDefinition
from xtce_sim.dynamics.model import AdcsModel, AdcsModelConfig, parse_model

_TEMPLATE_RE = re.compile(r"\{(\w+)\}")
_EMIT_VALUES = ("interval", "immediate")
# The verb grammar, single-sourced: per-verb attributes, the universal
# attributes every verb accepts, and which verbs are continuous (tick-driven).
# Everything else — the full key set, the signals filter, the dispatch order —
# derives from these three, so a new verb or attribute is added in one place.
_VERB_ATTRS = {
    "set": set(),
    "increment": set(),
    "ramp_to": {"tau", "noise"},
    "oscillate": {"amplitude", "period", "shape", "phase", "noise"},
    "hold": {"noise"},
}
_UNIVERSAL_ATTRS = {"emit"}
_CONTINUOUS_VERBS = ("ramp_to", "oscillate", "hold")
_VERB_KEYS = set(_VERB_ATTRS) | _UNIVERSAL_ATTRS | set().union(*_VERB_ATTRS.values())
_WAVE_SHAPES = ("sine", "triangle", "sawtooth")
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
    noise: float = 0.0  # gaussian stddev added to the emitted value
    emit: str = "interval"


@dataclass
class OscillateEffect:
    field: str
    center: float | str  # number, or "@FIELD" (possibly templated)
    amplitude: float
    period: float  # seconds (a period, never a frequency)
    shape: str = "sine"  # sine | triangle | sawtooth
    phase: float = 0.0  # seconds of offset into the cycle
    noise: float = 0.0
    emit: str = "interval"


@dataclass
class HoldEffect:
    field: str
    value: float | str  # number, or "@FIELD" (tracked live)
    noise: float = 0.0
    emit: str = "interval"


Effect = SetEffect | CopyArgEffect | IncrementEffect | RampEffect | OscillateEffect | HoldEffect
# The effect classes behind _CONTINUOUS_VERBS: registered as active behaviors
# and advanced by tick(), versus the instant set/copy/increment.
_CONTINUOUS_EFFECTS = (RampEffect, OscillateEffect, HoldEffect)


@dataclass
class BehaviorSpec:
    """Validated behavior: initial values, boot signals, command effects."""

    path: Path  # the source as given: a satellite directory or one file
    initial: dict[str, Scalar]
    commands: dict[str, list[Effect]]  # command name -> effects
    # Continuous behaviors started at boot.
    signals: list[Effect] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)  # the merged .toml files
    # Physics models declared under [_models]: each owns its output fields
    # (no other table may write them) and consumes its bound commands.
    models: list[AdcsModelConfig] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        """Human label: 'dir/ (3 files)' for a directory, the path for a file."""
        if len(self.files) > 1 or (self.path and Path(self.path).is_dir()):
            return f"{self.path} ({len(self.files)} file(s))"
        return str(self.path)


def sidecar_path(xtce_paths: list[Path]) -> Optional[Path]:
    """The satellite's behavior source: its directory, if it holds TOML.

    A satellite is a directory — the XTCE and its per-subsystem behavior
    ``.toml`` files live together. Discovery returns the first XTCE's
    directory when at least one ``.toml`` is present, else None.
    """
    if not xtce_paths:
        return None
    directory = Path(xtce_paths[0]).resolve().parent
    return directory if any(directory.glob("*.toml")) else None


def load_behavior(source: Path, simdef: SimDefinition) -> BehaviorSpec:
    """Parse and fully validate behavior TOML against a definition.

    ``source`` is a satellite directory (every ``*.toml`` inside is merged,
    sorted by name) or a single ``.toml`` file. Merging is strict: the same
    field declared for the same table in two files is a conflict naming
    both. All problems from all files are reported in one BehaviorError.
    """
    source = Path(source)
    files = sorted(source.glob("*.toml")) if source.is_dir() else [source]
    if not files:
        raise BehaviorError(f"{source}: no .toml behavior files found")

    ctx = _Context(simdef)
    initial: dict[str, Scalar] = {}
    signals: list[Effect] = []
    commands: dict[str, list[Effect]] = {}
    models: list[AdcsModelConfig] = []
    origins: dict[tuple, str] = {}  # (table, field template) -> file name
    for path in files:
        _load_one_file(
            path,
            simdef,
            ctx,
            initial,
            signals,
            commands,
            origins,
            models,
            tag=len(files) > 1,
        )

    _check_model_ownership(models, initial, signals, commands, ctx)
    if ctx.errors:
        problems = "\n  - ".join(ctx.errors)
        raise BehaviorError(f"{source}: {len(ctx.errors)} problem(s):\n  - {problems}")
    return BehaviorSpec(
        path=source,
        initial=initial,
        commands=commands,
        signals=signals,
        files=files,
        models=models,
    )


def _check_model_ownership(
    models: list[AdcsModelConfig],
    initial: dict,
    signals: list,
    commands: dict,
    ctx: "_Context",
) -> None:
    """A model OWNS its output fields and its commands: any other claimant
    is a conflict.

    Two sources of truth for one field would fight every tick; two models
    consuming one command name would silently shadow each other (dict
    routing: last one wins). Fields are checked by literal name — a
    template that resolves onto a model field at execution time is caught
    by the engine's runtime guard instead (warned and skipped).
    """
    owned: dict[str, str] = {}
    for cfg in models:
        for fname in cfg.outputs:
            if fname in owned:
                ctx.error(f"[_models] {fname}: bound by both {owned[fname]!r} and {cfg.name!r}")
            owned[fname] = cfg.name
    consumed: dict[str, str] = {}
    for cfg in models:
        for cname in cfg.commands.values():
            if cname in consumed:
                ctx.error(
                    f"[_models] command {cname}: consumed by both "
                    f"{consumed[cname]!r} and {cfg.name!r}"
                )
            consumed[cname] = cfg.name
    if not owned:
        return
    for fname in initial:
        if fname in owned:
            ctx.error(f"[_initial] {fname}: owned by model {owned[fname]!r}")
    for eff in signals:
        if eff.field in owned:
            ctx.error(f"[_signals] {eff.field}: owned by model {owned[eff.field]!r}")
    for cname, effects in commands.items():
        for eff in effects:
            if eff.field in owned:
                ctx.error(f"[{cname}] {eff.field}: owned by model {owned[eff.field]!r}")


def _load_one_file(
    path: Path,
    simdef: SimDefinition,
    ctx: _Context,
    initial: dict,
    signals: list,
    commands: dict,
    origins: dict,
    models: list,
    *,
    tag: bool,
) -> None:
    """Parse one file into the shared structures, flagging cross-file dups.

    With ``tag`` set (multi-file source), every error is prefixed with the
    file name so a combined report stays attributable.
    """
    first_error = len(ctx.errors)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        ctx.error(f"not valid TOML: {exc}")
        data = {}

    merge = _Merger(path.name, origins, ctx)
    for table, body in data.items():
        if table == "_initial":
            _merge_initial(body, ctx, initial, merge)
        elif table == "_signals":
            _merge_signals(body, ctx, signals, merge)
        elif table == "_models":
            _merge_models(body, simdef, ctx, models, merge)
        else:
            _merge_command_table(table, body, simdef, ctx, commands, merge)

    if tag:
        for i in range(first_error, len(ctx.errors)):
            ctx.errors[i] = f"{path.name}: {ctx.errors[i]}"


def _merge_initial(body, ctx: _Context, initial: dict, merge: "_Merger") -> None:
    for fname, value in _load_initial(body, ctx).items():
        if merge.claim("_initial", fname):
            initial[fname] = value


def _merge_signals(body, ctx: _Context, signals: list, merge: "_Merger") -> None:
    for eff in _load_signals(body, ctx):
        if merge.claim("_signals", eff.field):
            signals.append(eff)


def _merge_models(
    body, simdef: SimDefinition, ctx: _Context, models: list, merge: "_Merger"
) -> None:
    if not isinstance(body, dict):
        ctx.error("[_models]: must hold one sub-table per model")
        return
    for name, table in body.items():
        if not merge.claim("_models", name):
            continue
        cfg = parse_model(name, table, simdef, ctx.error)
        if cfg is not None:
            models.append(cfg)


class _Merger:
    """Cross-file ownership of (table, field): first file claims, rest error."""

    def __init__(self, filename: str, origins: dict, ctx: _Context) -> None:
        self.filename, self.origins, self.ctx = filename, origins, ctx

    def claim(self, table: str, key: str) -> bool:
        owner = self.origins.get((table, key))
        if owner is not None and owner != self.filename:
            self.ctx.error(f"[{table}] {key}: already declared in {owner}")
            return False
        self.origins[(table, key)] = self.filename
        return True


def _merge_command_table(
    table: str,
    body,
    simdef: SimDefinition,
    ctx: _Context,
    commands: dict,
    merge: "_Merger",
) -> None:
    command = simdef.command_by_name(table)
    if command is None:
        ctx.error(f"[{table}]: unknown command (not in the definition)")
        return
    for eff in _load_command_table(table, body, command, ctx):
        if merge.claim(table, eff.field):
            commands.setdefault(table, []).append(eff)


def describe(spec: BehaviorSpec) -> list[str]:
    """Human-readable narration of a behavior spec, one line per fact."""
    lines = []
    if spec.initial:
        lines.append(f"initial values: {len(spec.initial)} field(s)")
        for fname, value in spec.initial.items():
            lines.append(f"  {fname} = {value!r}")
    if spec.signals:
        lines.append(f"boot signals: {len(spec.signals)}")
        for eff in spec.signals:
            lines.append(f"  {_describe_effect(eff)}")
    for cfg in spec.models:
        lines.extend(cfg.describe())
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
    if isinstance(eff, OscillateEffect):
        noise = f" ±noise({eff.noise})" if eff.noise else ""
        return (
            f"{eff.field} oscillates ({eff.shape}) around {eff.center} "
            f"amplitude {eff.amplitude}, period {eff.period}s{noise}{tail}"
        )
    if isinstance(eff, HoldEffect):
        noise = f" ±noise({eff.noise})" if eff.noise else ""
        return f"{eff.field} holds at {eff.value}{noise}{tail}"
    noise = f" ±noise({eff.noise})" if eff.noise else ""
    return f"{eff.field} ramps to {eff.target} (tau={eff.tau}s){noise}{tail}"


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


def _load_signals(body, ctx: _Context) -> list[Effect]:
    """[_signals]: continuous behaviors started at boot (no command context)."""
    signals: list[Effect] = []
    if not isinstance(body, dict):
        ctx.error("[_signals]: must be a table of FIELD = behavior")
        return signals
    for fname, spec in body.items():
        where = f"[_signals] {fname}"
        if _TEMPLATE_RE.search(fname):
            ctx.error(f"{where}: templates are not allowed here — no command context")
            continue
        if fname not in ctx.fields:
            ctx.error(f"{where}: unknown telemetry field")
            continue
        if not isinstance(spec, dict) or not any(k in spec for k in _CONTINUOUS_VERBS):
            ctx.error(
                f"{where}: signals must be continuous behaviors "
                "(ramp_to/oscillate/hold); use [_initial] for one-shot values"
            )
            continue
        eff = _parse_effect_table(where, fname, spec, None, ctx)
        if eff is not None:
            signals.append(eff)
    return signals


def _load_command_table(table: str, body, command: CommandDef, ctx: _Context) -> list[Effect]:
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
        arg = value[len("@arg:") :]
        if not _has_arg(command, arg):
            ctx.error(f"{where}: @arg:{arg} — command has no argument {arg!r}")
            return None
        for concrete in _expansions(fname, command) or []:
            _check_invertible(where, concrete, ctx)
        return CopyArgEffect(field=fname, arg=arg, emit=emit)
    if isinstance(value, str) and value.startswith("@"):
        ctx.error(
            f'{where}: {value!r} — did you mean "@arg:..."? '
            "(@FIELD references are only valid as ramp_to targets)"
        )
        return None
    _check_scalar_for_field(where, fname, value, command, ctx)
    return SetEffect(field=fname, value=value, emit=emit)


def _parse_effect_table(
    where: str, fname: str, spec: dict, command: CommandDef | None, ctx: _Context
) -> Optional[Effect]:
    unknown = set(spec) - _VERB_KEYS
    if unknown:
        ctx.error(f"{where}: unknown key(s) {sorted(unknown)}; valid: {sorted(_VERB_KEYS)}")
        return None
    emit = spec.get("emit", "interval")
    if emit not in _EMIT_VALUES:
        ctx.error(f"{where}: emit must be one of {_EMIT_VALUES}, got {emit!r}")
        return None
    verbs = [k for k in _VERB_ATTRS if k in spec]
    if len(verbs) != 1:
        ctx.error(f"{where}: exactly one of {'/'.join(_VERB_ATTRS)} required, got {verbs}")
        return None
    if not _attrs_match_verb(where, spec, verbs[0], ctx):
        return None
    if emit == "immediate" and verbs[0] in _CONTINUOUS_VERBS:
        ctx.error(
            f'{where}: emit = "immediate" is not valid with {verbs[0]} — '
            "continuous behaviors pace with the beacon; mark an instant "
            "set/increment on another field instead"
        )
        return None
    if verbs[0] == "set":
        return _scalar_effect(where, fname, spec["set"], command, ctx, emit)
    if verbs[0] == "increment":
        return _parse_increment(where, fname, spec, command, emit, ctx)
    if verbs[0] == "oscillate":
        return _parse_oscillate(where, fname, spec, command, emit, ctx)
    if verbs[0] == "hold":
        return _parse_hold(where, fname, spec, command, emit, ctx)
    return _parse_ramp(where, fname, spec, command, emit, ctx)


def _attrs_match_verb(where: str, spec: dict, verb: str, ctx: _Context) -> bool:
    """Attributes must belong to their verb (tau without ramp_to is a typo)."""
    attrs = set(spec) - {verb} - _UNIVERSAL_ATTRS
    stray = attrs - _VERB_ATTRS[verb]
    if stray:
        ctx.error(f"{where}: {sorted(stray)} not valid with {verb}")
        return False
    return True


def _finite_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _parse_noise(where: str, spec: dict, ctx: _Context) -> Optional[float]:
    """Validated noise stddev (0.0 when absent); None means invalid."""
    noise = spec.get("noise", 0.0)
    if not _finite_number(noise) or noise < 0:
        ctx.error(f"{where}: noise must be a non-negative number, got {noise!r}")
        return None
    return float(noise)


def _parse_center(
    where: str, key: str, fname: str, spec, command: CommandDef | None, ctx: _Context
):
    """A number or @FIELD reference (validated numeric); None on error."""
    value = spec[key]
    if isinstance(value, str):
        if not value.startswith("@"):
            ctx.error(f"{where}: {key} string value must be an @FIELD reference")
            return None
        if value == f"@{fname}":
            # Feeding a field its own output compounds noise/wave offsets
            # into unbounded drift instead of moving around a fixed point.
            ctx.error(f"{where}: {key} must not reference its own field")
            return None
        _check_field_template(where, value[1:], command, ctx, numeric=True)
        return value
    if not _finite_number(value):
        ctx.error(f"{where}: {key} must be a finite number or @FIELD, got {value!r}")
        return None
    return float(value)


def _parse_oscillate(
    where: str, fname: str, spec: dict, command: CommandDef | None, emit: str, ctx: _Context
) -> Optional[OscillateEffect]:
    center = _parse_center(where, "oscillate", fname, spec, command, ctx)
    noise = _parse_noise(where, spec, ctx)
    if center is None or noise is None:
        return None
    if "amplitude" not in spec or "period" not in spec:
        ctx.error(f"{where}: oscillate requires amplitude and period")
        return None
    amplitude, period = spec["amplitude"], spec["period"]
    if not _finite_number(amplitude) or amplitude < 0:
        ctx.error(f"{where}: amplitude must be a non-negative number, got {amplitude!r}")
        return None
    if not _finite_number(period) or period <= 0:
        ctx.error(f"{where}: period must be a positive number of seconds, got {period!r}")
        return None
    shape = spec.get("shape", "sine")
    if shape not in _WAVE_SHAPES:
        ctx.error(f"{where}: shape must be one of {_WAVE_SHAPES}, got {shape!r}")
        return None
    phase = spec.get("phase", 0.0)
    if not _finite_number(phase):
        ctx.error(f"{where}: phase must be a finite number of seconds, got {phase!r}")
        return None
    _check_numeric_field(where, fname, command, ctx)
    return OscillateEffect(
        field=fname,
        center=center,
        amplitude=float(amplitude),
        period=float(period),
        shape=shape,
        phase=float(phase),
        noise=noise,
        emit=emit,
    )


def _parse_hold(
    where: str, fname: str, spec: dict, command: CommandDef | None, emit: str, ctx: _Context
) -> Optional[HoldEffect]:
    value = _parse_center(where, "hold", fname, spec, command, ctx)
    noise = _parse_noise(where, spec, ctx)
    if value is None or noise is None:
        return None
    _check_numeric_field(where, fname, command, ctx)
    return HoldEffect(field=fname, value=value, noise=noise, emit=emit)


def _parse_increment(
    where: str, fname: str, spec: dict, command: CommandDef, emit: str, ctx: _Context
) -> Optional[IncrementEffect]:
    by = spec["increment"]
    if not _finite_number(by):
        ctx.error(f"{where}: increment must be a finite number, got {by!r}")
        return None
    _check_numeric_field(where, fname, command, ctx)
    return IncrementEffect(field=fname, by=by, emit=emit)


def _parse_ramp(
    where: str, fname: str, spec: dict, command: CommandDef | None, emit: str, ctx: _Context
) -> Optional[RampEffect]:
    if "tau" not in spec:
        ctx.error(f"{where}: ramp_to requires tau (time constant in seconds)")
        return None
    tau = spec["tau"]
    if not _finite_number(tau) or tau <= 0:
        ctx.error(f"{where}: tau must be a positive number, got {tau!r}")
        return None
    target = _parse_center(where, "ramp_to", fname, spec, command, ctx)
    noise = _parse_noise(where, spec, ctx)
    if target is None or noise is None:
        return None
    _check_numeric_field(where, fname, command, ctx)
    return RampEffect(field=fname, target=target, tau=float(tau), noise=noise, emit=emit)


def _has_arg(command: CommandDef, name: str) -> bool:
    return any(p.name == name for p in command.params)


def _check_numeric_field(
    where: str, template: str, command: Optional[CommandDef], ctx: _Context
) -> None:
    """Every load-time expansion of *template* must be a numeric field."""
    for concrete in _expansions(template, command) or []:
        if concrete in ctx.fields and not ctx.is_numeric(concrete):
            ctx.error(f"{where}: {concrete} is not a numeric field")
        _check_invertible(where, concrete, ctx)


def _check_invertible(where: str, concrete: str, ctx: _Context) -> None:
    """Behavior values are engineering units; the field's calibrator must
    run backwards to produce wire counts."""
    f = ctx.fields.get(concrete)
    if f is not None and f.calibrator is not None and not f.calibrator.is_invertible:
        ctx.error(
            f"{where}: {concrete} has a non-invertible calibrator — "
            "engineering-unit behavior values cannot be converted to raw counts"
        )


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
            _check_invertible(where, concrete, ctx)


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
    command: CommandDef | None,
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
    if args and command is None:  # [_signals] etc. have no arguments to fill
        ctx.error(f"{where}: templates are not allowed here — no command context")
        return
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

# A ramp is complete when it gets within this distance of its target. Integer
# fields land earlier in practice: the moment the stored (rounded) value
# equals the stored target, the ramp retires.
_RAMP_TOLERANCE = 1e-6


def _field_rng(fname: str) -> random.Random:
    """A per-field RNG with a stable seed: noisy runs are reproducible."""
    return random.Random(f"xtce-sim:{fname}")


@dataclass
class _ActiveRamp:
    """One running first-order approach on a field (registry entry).

    ``value`` is the ramp's own float trajectory. The overlay stores the
    wire-coerced view (rounded for integer fields); if the ramp advanced
    from that stored value instead, sub-0.5 steps on an integer field would
    round away every tick and the ramp would stall short of its target.
    Retirement is driven by the float trajectory (tolerance or a stalled
    step), not by the rounded overlay view. A noisy ramp emits
    trajectory+noise and degrades into a noisy hold at its target.
    """

    field: str
    target: float | str  # number, or "@FIELD" (already template-resolved)
    tau: float
    noise: float = 0.0
    value: Optional[float] = None  # seeded from the overlay on first tick
    warned: bool = False  # missing-target warning already issued (warn once)
    rng: Optional[random.Random] = None


@dataclass
class _ActiveOsc:
    """A continuous wave around a (possibly live @FIELD) center."""

    field: str
    center: float | str
    amplitude: float
    period: float
    shape: str
    phase: float
    noise: float = 0.0
    elapsed: float = 0.0  # seconds since the behavior started
    warned: bool = False
    rng: Optional[random.Random] = None


@dataclass
class _ActiveHold:
    """Keeps re-asserting a value (or tracking @FIELD), optionally noisy."""

    field: str
    value: float | str
    noise: float = 0.0
    warned: bool = False
    rng: Optional[random.Random] = None


_ActiveBehavior = _ActiveRamp | _ActiveOsc | _ActiveHold


def _describe_active(eff, ref) -> str:
    """Short start-log phrasing for a registered behavior."""
    if isinstance(eff, RampEffect):
        return f"ramping to {ref} (tau={eff.tau}s)"
    if isinstance(eff, OscillateEffect):
        return f"oscillating ({eff.shape}) around {ref}, period {eff.period}s"
    return f"holding at {ref}"


def _wave(shape: str, frac: float) -> float:
    """Unit wave in [-1, 1] at cycle fraction *frac* (all start at 0, rising)."""
    if shape == "sine":
        return math.sin(2.0 * math.pi * frac)
    if shape == "triangle":
        if frac < 0.25:
            return 4.0 * frac
        if frac < 0.75:
            return 2.0 - 4.0 * frac
        return 4.0 * frac - 4.0
    # sawtooth: rise 0->1 over the first half, jump to -1, rise back to 0
    return 2.0 * frac if frac < 0.5 else 2.0 * frac - 2.0


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
        self._behaviors: dict[str, _ActiveBehavior] = {}
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
            if isinstance(eff, _CONTINUOUS_EFFECTS):
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
            if isinstance(eff, _CONTINUOUS_EFFECTS):
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
        if isinstance(eff, RampEffect):
            ref = eff.target
        elif isinstance(eff, OscillateEffect):
            ref = eff.center
        else:
            ref = eff.value
        if isinstance(ref, str):  # "@FIELD", possibly templated
            resolved = self._resolve_template(where, ref[1:], command, args)
            if resolved is None:
                return None
            if resolved == fname:  # template args can make @ref land on itself
                logger.warning("%s: reference @%s names its own field; skipped", where, fname)
                return None
            ref = f"@{resolved}"
        rng = self._rngs.setdefault(fname, _field_rng(fname)) if eff.noise else None
        self._behaviors[fname] = self._make_active(eff, fname, ref, rng)
        return f"{fname} {_describe_active(eff, ref)}"

    @staticmethod
    def _make_active(eff, fname: str, ref, rng) -> _ActiveBehavior:
        if isinstance(eff, RampEffect):
            return _ActiveRamp(field=fname, target=ref, tau=eff.tau, noise=eff.noise, rng=rng)
        if isinstance(eff, OscillateEffect):
            return _ActiveOsc(
                field=fname,
                center=ref,
                amplitude=eff.amplitude,
                period=eff.period,
                shape=eff.shape,
                phase=eff.phase,
                noise=eff.noise,
                rng=rng,
            )
        return _ActiveHold(field=fname, value=ref, noise=eff.noise, rng=rng)

    def tick(self, dt: float) -> None:
        """Advance every active ramp by *dt* seconds.

        Uses the closed-form first-order step, so the curve is exact for any
        tick size — a 5-second beacon interval samples the same trajectory a
        0.5-second one does. The ramp advances its own float trajectory (the
        overlay stores the wire-coerced view) and completes when it gets
        within _RAMP_TOLERANCE of the target. An @FIELD target is re-read every tick, so changing a
        setpoint mid-ramp bends the curve.
        """
        if dt <= 0:
            return
        # Iterate a snapshot: completed ramps delete themselves mid-loop.
        snapshot = self._behaviors.copy()
        for fname, beh in snapshot.items():
            if isinstance(beh, _ActiveRamp):
                self._tick_ramp(fname, beh, dt)
            elif isinstance(beh, _ActiveOsc):
                self._tick_osc(fname, beh, dt)
            else:
                self._tick_hold(fname, beh)
        for model in self.models:
            model.advance(dt)
            self._store_model_outputs(model)

    def _store_model_outputs(self, model: AdcsModel) -> None:
        where = f"[_models.{model.config.name}]"
        for fname, value in model.outputs().items():
            self._store(where, fname, value)

    def _tick_ramp(self, fname: str, ramp: _ActiveRamp, dt: float) -> None:
        target = self._live_number(ramp, ramp.target)
        if target is None:
            return
        if ramp.value is None:
            ramp.value = self._ramp_current(fname)
        previous = ramp.value
        step = 1.0 - math.exp(-dt / ramp.tau)
        ramp.value += (target - ramp.value) * step
        self._store(f"[ramp] {fname}", fname, self._noisy(ramp, ramp.value))
        # Retire on tolerance, or when a step makes no float progress
        # (very large magnitudes stall at ~1 ULP before the tolerance).
        if abs(target - ramp.value) <= _RAMP_TOLERANCE or ramp.value == previous:
            # land exactly on target, then retire — a noisy ramp degrades
            # into a noisy hold there (settled but still breathing).
            self._store(f"[ramp] {fname}", fname, target)
            if ramp.noise:
                # hold the landed number, not the @FIELD reference — noise
                # must not turn a settled ramp into a live tracker.
                self._behaviors[fname] = _ActiveHold(
                    field=fname, value=target, noise=ramp.noise, rng=ramp.rng
                )
            else:
                del self._behaviors[fname]
            logger.debug("[ramp] %s reached %s; complete", fname, target)

    def _tick_osc(self, fname: str, osc: _ActiveOsc, dt: float) -> None:
        # The clock advances even while an @FIELD center is unresolved, so a
        # late-arriving center doesn't shift this wave's phase against time
        # (or against phase-staggered siblings).
        osc.elapsed += dt
        center = self._live_number(osc, osc.center)
        if center is None:
            return
        frac = ((osc.elapsed + osc.phase) / osc.period) % 1.0
        value = center + osc.amplitude * _wave(osc.shape, frac)
        self._store(f"[oscillate] {fname}", fname, self._noisy(osc, value))

    def _tick_hold(self, fname: str, hold: _ActiveHold) -> None:
        value = self._live_number(hold, hold.value)
        if value is None:
            return
        self._store(f"[hold] {fname}", fname, self._noisy(hold, value))

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
        else:  # IncrementEffect — arithmetic in engineering units
            current = self.state.get(fname, 0)
            current = self._engineering(fname, current) if isinstance(current, (int, float)) else 0
            value = current + eff.by
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
