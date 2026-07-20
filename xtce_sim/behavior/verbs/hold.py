"""The ``hold`` verb: keep re-asserting a value (or track an @FIELD)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from xtce_sim.behavior.spec import ActiveBehavior, ContinuousEffect, Verb
from xtce_sim.behavior.validate import _check_numeric_field, _Context, _parse_center, _parse_noise
from xtce_sim.definition import CommandDef


@dataclass
class _ActiveHold(ActiveBehavior):
    """Keeps re-asserting a value (or tracking @FIELD), optionally noisy."""

    value: float | str

    def advance(self, engine, fname: str, dt: float) -> None:
        value = engine._live_number(self, self.value)
        if value is None:
            return
        engine._store(f"[hold] {fname}", fname, engine._noisy(self, value))


@dataclass
class HoldEffect(ContinuousEffect):
    value: float | str  # number, or "@FIELD" (tracked live)

    @property
    def reference(self):
        return self.value

    def describe(self) -> str:
        return f"{self.field} holds at {self.value}{self._noise_suffix()}"

    def describe_active(self, ref) -> str:
        return f"holding at {ref}"

    def make_active(self, fname: str, ref, rng) -> _ActiveHold:
        return _ActiveHold(field=fname, value=ref, noise=self.noise, rng=rng)


def _parse(
    where: str, fname: str, spec: dict, command: CommandDef | None, emit: str, ctx: _Context
) -> Optional[HoldEffect]:
    value = _parse_center(where, "hold", fname, spec, command, ctx)
    noise = _parse_noise(where, spec, ctx)
    if value is None or noise is None:
        return None
    _check_numeric_field(where, fname, command, ctx)
    return HoldEffect(field=fname, value=value, noise=noise, emit=emit)


VERB = Verb(name="hold", attrs=frozenset({"noise"}), continuous=True, parse=_parse)
