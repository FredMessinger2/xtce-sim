"""Load-time side of behavior: sidecar discovery, parsing, validation.

Runs once at startup: discovers the ``.toml`` files beside the XTCE,
merges them with strict cross-file ownership, and validates everything
against the resolved SimDefinition — every problem from every file in one
BehaviorError. Verb-specific parsing lives with each verb (the ``verbs``
package); this module owns the cross-verb grammar: table shapes, unknown
keys, emit rules, exactly-one-verb. The full DSL documentation is the
package docstring (``xtce_sim/behavior/__init__.py``).
"""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Optional

from xtce_sim.behavior.spec import (
    _TEMPLATE_RE,
    VERBS,
    BehaviorError,
    BehaviorSpec,
    Effect,
    Scalar,
    Verb,
)
from xtce_sim.behavior.validate import _check_field_template, _check_scalar_for_field, _Context
from xtce_sim.behavior.verbs import scalar_effect  # populates VERBS on import
from xtce_sim.definition import CommandDef, SimDefinition
from xtce_sim.dynamics.model import AdcsModelConfig, parse_environment, parse_model

_EMIT_VALUES = ("interval", "immediate")
# Attributes every verb accepts, on top of its own (declared per Verb).
_UNIVERSAL_ATTRS = {"emit"}


def _verb_keys() -> set[str]:
    """The full key vocabulary of an effect table, from the LIVE registry
    (a verb registered after import is honored, not half-visible)."""
    return set(VERBS) | _UNIVERSAL_ATTRS | set().union(*(v.attrs for v in VERBS.values()))


def _continuous_verbs() -> list[str]:
    """The tick-driven verb names, from the live registry."""
    return [name for name, verb in VERBS.items() if verb.continuous]


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
    environments: list = []  # at most one — the shared world
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
            environments,
            tag=len(files) > 1,
        )

    _check_model_ownership(models, initial, signals, commands, ctx)
    if ctx.errors:
        problems = "\n  - ".join(ctx.errors)
        raise BehaviorError(f"{source}: {len(ctx.errors)} problem(s):\n  - {problems}")
    spec = BehaviorSpec(
        path=source,
        initial=initial,
        commands=commands,
        signals=signals,
        files=files,
        models=models,
    )
    if environments:
        spec.environment = environments[0]
    return spec


def _check_model_ownership(
    models: list[AdcsModelConfig],
    initial: dict,
    signals: list,
    commands: dict,
    ctx: _Context,
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


def _claim_unique(pairs, conflict: str, ctx: _Context) -> dict[str, str]:
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
    environments: list,
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
        elif table == "_environment":
            _merge_environment(body, ctx, environments, merge)
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


def _merge_environment(body, ctx: _Context, environments: list, merge: "_Merger") -> None:
    """The [_environment] table: the ONE shared world (orbit, sun).

    Exactly one file may declare it — a second declaration anywhere is a
    conflict, same rule as any duplicated field, because two solar
    systems that could disagree is precisely what this table forbids.
    """
    if not merge.claim("_environment", "world"):
        return
    env = parse_environment(body, ctx.error)
    if env is not None:
        environments.append(env)


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
    if spec.models:
        # The world only matters when something lives in it.
        env = spec.environment
        lines.append(
            f"environment: orbit {env.orbit.altitude / 1e3:.0f} km @ "
            f"{math.degrees(env.orbit.inclination):.1f} deg, "
            f"sun {list(env.sun_direction)} (shared by all models)"
        )
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
    return eff.describe() + tail


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
        if not isinstance(spec, dict) or not any(k in spec for k in _continuous_verbs()):
            ctx.error(
                f"{where}: signals must be continuous behaviors "
                f"({'/'.join(_continuous_verbs())}); use [_initial] for one-shot values"
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
    return scalar_effect(where, fname, spec, command, ctx, "interval")


def _parse_effect_table(
    where: str, fname: str, spec: dict, command: CommandDef | None, ctx: _Context
) -> Optional[Effect]:
    keys = _verb_keys()
    unknown = set(spec) - keys
    if unknown:
        ctx.error(f"{where}: unknown key(s) {sorted(unknown)}; valid: {sorted(keys)}")
        return None
    emit = spec.get("emit", "interval")
    if emit not in _EMIT_VALUES:
        ctx.error(f"{where}: emit must be one of {_EMIT_VALUES}, got {emit!r}")
        return None
    verbs = [k for k in VERBS if k in spec]
    if len(verbs) != 1:
        ctx.error(f"{where}: exactly one of {'/'.join(VERBS)} required, got {verbs}")
        return None
    verb = VERBS[verbs[0]]
    if not _attrs_match_verb(where, spec, verb, ctx):
        return None
    if emit == "immediate" and verb.continuous:
        ctx.error(
            f'{where}: emit = "immediate" is not valid with {verb.name} — '
            "continuous behaviors pace with the beacon; mark an instant "
            "set/increment on another field instead"
        )
        return None
    return verb.parse(where, fname, spec, command, emit, ctx)


def _attrs_match_verb(where: str, spec: dict, verb: Verb, ctx: _Context) -> bool:
    """Attributes must belong to their verb (tau without ramp_to is a typo)."""
    attrs = set(spec) - {verb.name} - _UNIVERSAL_ATTRS
    stray = attrs - verb.attrs
    if stray:
        ctx.error(f"{where}: {sorted(stray)} not valid with {verb.name}")
        return False
    return True
