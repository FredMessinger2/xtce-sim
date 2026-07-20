"""The ``set`` verb: instant assignment, including ``@arg:`` copies.

A bare scalar in the TOML is sugar for ``{ set = value }``; a value of
``"@arg:Name"`` copies a command argument instead, producing a
CopyArgEffect. Both are instant: applied once when the command executes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from xtce_sim.behavior.spec import _SKIP, Effect, InstantEffect, Scalar, Verb
from xtce_sim.behavior.validate import (
    _check_invertible,
    _check_scalar_for_field,
    _Context,
    _expansions,
    _has_arg,
)
from xtce_sim.definition import CommandDef

logger = logging.getLogger("xtce_sim.behavior")


@dataclass
class SetEffect(InstantEffect):
    value: Scalar  # number/bool, enum label, or string payload

    def describe(self) -> str:
        return f"{self.field} = {self.value!r}"

    def value_for(self, engine, command, args, where, fname):
        return self.value


@dataclass
class CopyArgEffect(InstantEffect):
    arg: str

    def describe(self) -> str:
        return f"{self.field} = @arg:{self.arg}"

    def value_for(self, engine, command, args, where, fname):
        if self.arg not in args:
            logger.warning("%s: argument %s missing from decode; skipped", where, self.arg)
            return _SKIP
        # Same raw-value rule as templates: an enum argument arrives from
        # decode as its label; store its raw value (the destination
        # field's own enum may use different labels entirely).
        return engine._raw_arg(command, args, self.arg)


def scalar_effect(
    where: str, fname: str, value, command: CommandDef, ctx: _Context, emit: str
) -> Effect | None:
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


def _parse(where, fname, spec, command, emit, ctx):
    return scalar_effect(where, fname, spec["set"], command, ctx, emit)


VERB = Verb(name="set", attrs=frozenset(), continuous=False, parse=_parse)
