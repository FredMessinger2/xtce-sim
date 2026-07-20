"""Shared load-time validation services for behavior parsing.

The error-collecting context and the field/argument/template checks used
by the loader core and by every verb module's parser. All functions are
moved verbatim from the pre-package behavior module; error text is part
of the contract (tests pin it).
"""

from __future__ import annotations

import itertools
import math
from typing import Optional

from xtce_sim.behavior.spec import _MAX_EXPANSIONS, _TEMPLATE_RE, Scalar
from xtce_sim.definition import CommandDef, SimDefinition


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


__all__ = [
    "Scalar",
    "_Context",
    "_arg_values",
    "_check_field_template",
    "_check_invertible",
    "_check_numeric_field",
    "_check_scalar_against",
    "_check_scalar_for_field",
    "_expansions",
    "_finite_number",
    "_has_arg",
    "_parse_center",
    "_parse_noise",
    "_product_size",
]
