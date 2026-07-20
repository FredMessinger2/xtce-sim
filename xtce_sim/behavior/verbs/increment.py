"""The ``increment`` verb: instant addition in engineering units."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from xtce_sim.behavior.spec import InstantEffect, Verb
from xtce_sim.behavior.validate import _check_numeric_field, _Context, _finite_number
from xtce_sim.definition import CommandDef


@dataclass
class IncrementEffect(InstantEffect):
    by: float

    def describe(self) -> str:
        return f"{self.field} += {self.by}"

    def value_for(self, engine, command, args, where, fname):
        return engine._current_engineering(fname) + self.by


def _parse(
    where: str, fname: str, spec: dict, command: CommandDef, emit: str, ctx: _Context
) -> Optional[IncrementEffect]:
    by = spec["increment"]
    if not _finite_number(by):
        ctx.error(f"{where}: increment must be a finite number, got {by!r}")
        return None
    _check_numeric_field(where, fname, command, ctx)
    return IncrementEffect(field=fname, by=by, emit=emit)


VERB = Verb(name="increment", attrs=frozenset(), continuous=False, parse=_parse)
