"""The ``ramp_to`` verb: a first-order approach toward a target.

The closed-form step makes the trajectory exact at any tick size; a noisy
ramp degrades into a noisy hold when it lands.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from xtce_sim.behavior.spec import ActiveBehavior, ContinuousEffect, Verb
from xtce_sim.behavior.validate import (
    _check_numeric_field,
    _Context,
    _finite_number,
    _parse_center,
    _parse_noise,
)
from xtce_sim.behavior.verbs.hold import _ActiveHold
from xtce_sim.definition import CommandDef

logger = logging.getLogger("xtce_sim.behavior")

# A ramp is complete when it gets within this distance of its target. Integer
# fields land earlier in practice: the moment the stored (rounded) value
# equals the stored target, the ramp retires.
_RAMP_TOLERANCE = 1e-6


@dataclass
class _ActiveRamp(ActiveBehavior):
    """One running first-order approach on a field (registry entry).

    ``value`` is the ramp's own float trajectory. The overlay stores the
    wire-coerced view (rounded for integer fields); if the ramp advanced
    from that stored value instead, sub-0.5 steps on an integer field would
    round away every tick and the ramp would stall short of its target.
    Retirement is driven by the float trajectory (tolerance or a stalled
    step), not by the rounded overlay view. A noisy ramp emits
    trajectory+noise and degrades into a noisy hold at its target.
    """

    target: float | str  # number, or "@FIELD" (already template-resolved)
    tau: float
    value: Optional[float] = None  # seeded from the overlay on first tick

    def advance(self, engine, fname: str, dt: float) -> None:
        target = engine._live_number(self, self.target)
        if target is None:
            return
        if self.value is None:
            self.value = engine._current_engineering(fname)
        previous = self.value
        step = 1.0 - math.exp(-dt / self.tau)
        self.value += (target - self.value) * step
        engine._store(f"[ramp] {fname}", fname, engine._noisy(self, self.value))
        # Retire on tolerance, or when a step makes no float progress
        # (very large magnitudes stall at ~1 ULP before the tolerance).
        if abs(target - self.value) <= _RAMP_TOLERANCE or self.value == previous:
            # land exactly on target, then retire — a noisy ramp degrades
            # into a noisy hold there (settled but still breathing).
            engine._store(f"[ramp] {fname}", fname, target)
            if self.noise:
                # hold the landed number, not the @FIELD reference — noise
                # must not turn a settled ramp into a live tracker.
                engine._behaviors[fname] = _ActiveHold(
                    field=fname, value=target, noise=self.noise, rng=self.rng
                )
            else:
                del engine._behaviors[fname]
            logger.debug("[ramp] %s reached %s; complete", fname, target)


@dataclass
class RampEffect(ContinuousEffect):
    target: float | str  # number, or "@FIELD" (possibly templated)
    tau: float

    @property
    def reference(self):
        return self.target

    def describe(self) -> str:
        return f"{self.field} ramps to {self.target} (tau={self.tau}s){self._noise_suffix()}"

    def describe_active(self, ref) -> str:
        return f"ramping to {ref} (tau={self.tau}s)"

    def make_active(self, fname: str, ref, rng) -> _ActiveRamp:
        return _ActiveRamp(field=fname, target=ref, tau=self.tau, noise=self.noise, rng=rng)


def _parse(
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


VERB = Verb(name="ramp_to", attrs=frozenset({"tau", "noise"}), continuous=True, parse=_parse)
