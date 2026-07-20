"""The ``oscillate`` verb: a continuous wave around a (possibly live) center."""

from __future__ import annotations

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
from xtce_sim.definition import CommandDef

_WAVE_SHAPES = ("sine", "triangle", "sawtooth")


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


@dataclass
class _ActiveOsc(ActiveBehavior):
    """A continuous wave around a (possibly live @FIELD) center."""

    center: float | str
    amplitude: float
    period: float
    shape: str
    phase: float
    elapsed: float = 0.0  # seconds since the behavior started

    def advance(self, engine, fname: str, dt: float) -> None:
        # The clock advances even while an @FIELD center is unresolved, so a
        # late-arriving center doesn't shift this wave's phase against time
        # (or against phase-staggered siblings).
        self.elapsed += dt
        center = engine._live_number(self, self.center)
        if center is None:
            return
        frac = ((self.elapsed + self.phase) / self.period) % 1.0
        value = center + self.amplitude * _wave(self.shape, frac)
        engine._store(f"[oscillate] {fname}", fname, engine._noisy(self, value))


@dataclass
class OscillateEffect(ContinuousEffect):
    center: float | str  # number, or "@FIELD" (possibly templated)
    amplitude: float
    period: float  # seconds (a period, never a frequency)
    shape: str = "sine"  # sine | triangle | sawtooth
    phase: float = 0.0  # seconds of offset into the cycle

    @property
    def reference(self):
        return self.center

    def describe(self) -> str:
        return (
            f"{self.field} oscillates ({self.shape}) around {self.center} "
            f"amplitude {self.amplitude}, period {self.period}s{self._noise_suffix()}"
        )

    def describe_active(self, ref) -> str:
        return f"oscillating ({self.shape}) around {ref}, period {self.period}s"

    def make_active(self, fname: str, ref, rng) -> _ActiveOsc:
        return _ActiveOsc(
            field=fname,
            center=ref,
            amplitude=self.amplitude,
            period=self.period,
            shape=self.shape,
            phase=self.phase,
            noise=self.noise,
            rng=rng,
        )


def _parse(
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


VERB = Verb(
    name="oscillate",
    attrs=frozenset({"amplitude", "period", "shape", "phase", "noise"}),
    continuous=True,
    parse=_parse,
)
