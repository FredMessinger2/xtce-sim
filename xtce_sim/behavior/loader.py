"""Load-time side of behavior: sidecar discovery, parsing, validation.

Runs once at startup: discovers the ``.toml`` files beside the XTCE,
merges them with strict cross-file ownership, and validates everything
against the resolved SimDefinition — every problem from every file in one
BehaviorError. The full DSL documentation is the package docstring
(``xtce_sim/behavior/__init__.py``).
"""

from __future__ import annotations

import itertools
import math
import tomllib
from pathlib import Path
from typing import Optional

from xtce_sim.behavior.spec import (
    _CONTINUOUS_VERBS,
    _EMIT_VALUES,
    _MAX_EXPANSIONS,
    _TEMPLATE_RE,
    _UNIVERSAL_ATTRS,
    _VERB_ATTRS,
    _VERB_KEYS,
    _WAVE_SHAPES,
    BehaviorError,
    BehaviorSpec,
    CopyArgEffect,
    Effect,
    HoldEffect,
    IncrementEffect,
    OscillateEffect,
    RampEffect,
    Scalar,
    SetEffect,
)
from xtce_sim.definition import CommandDef, SimDefinition
from xtce_sim.dynamics.model import AdcsModelConfig, parse_model


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
    owned = _claim_unique(
        ((fname, cfg.name) for cfg in models for fname in cfg.outputs),
        "[_models] {key}: bound by both {first!r} and {second!r}",
        ctx,
    )
    _claim_unique(
        ((cname, cfg.name) for cfg in models for cname in cfg.commands.values()),
        "[_models] command {key}: consumed by both {first!r} and {second!r}",
        ctx,
    )
    writers = [(f"[_initial] {fname}", fname) for fname in initial]
    writers += [(f"[_signals] {eff.field}", eff.field) for eff in signals]
    writers += [
        (f"[{cname}] {eff.field}", eff.field)
        for cname, effects in commands.items()
        for eff in effects
    ]
    for where, fname in writers:
        if fname in owned:
            ctx.error(f"{where}: owned by model {owned[fname]!r}")


def _claim_unique(pairs, conflict: str, ctx: "_Context") -> dict[str, str]:
    """Each key claimed by at most one model; a second claimant errors."""
    claims: dict[str, str] = {}
    for key, claimant in pairs:
        if key in claims:
            ctx.error(conflict.format(key=key, first=claims[key], second=claimant))
        claims[key] = claimant
    return claims


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


def _arg_values(command: CommandDef, arg: str) -> Optional[list[str]]:
    """The finite substitution set of a command argument, or None if unbounded.

    What each value substitutes as must mirror the runtime rule in
    ``BehaviorEngine._template_arg``: an enumerated argument contributes its
    labels, an integer argument with a small ValidRange each raw value.
    """
    p = next((p for p in command.params if p.name == arg), None)
    if p is None:
        return None
    if p.enumerations:
        return sorted(p.enumerations)
    if p.valid_min is not None and p.valid_max is not None:
        # ceil/floor so a float range like 1.5..2.7 yields only the integers
        # actually inside it (2), not truncation artifacts at the edges.
        lo, hi = math.ceil(p.valid_min), math.floor(p.valid_max)
        if lo <= hi and (hi - lo) < _MAX_EXPANSIONS:
            return [str(v) for v in range(lo, hi + 1)]
    return None


def _product_size(value_sets: list[list[str]]) -> int:
    size = 1
    for values in value_sets:
        size *= max(1, len(values))
    return size
