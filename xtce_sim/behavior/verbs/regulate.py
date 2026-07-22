"""The ``regulate`` verb: bang-bang regulation around a (possibly live) center.

A thermostat generalized: an internal element is either driving the field
toward ``heats_to`` (time constant ``tau_heat``) or letting it relax toward
``cools_to`` (``tau_cool``), flipping at the edges of a hysteresis ``band``
centered on the regulated reference. The result is the real shape — a
sawtooth inside the band, asymmetric rise and fall — not a flat line.

Unlike ``ramp_to``, regulate never retires: arriving at the center is the
start of its job, not the end, so a live ``@FIELD`` center (a setpoint)
is honored forever — change the setpoint an hour later and the loop
follows. Degenerate configurations behave physically rather than erroring:
a ``heats_to`` below the band's top edge settles there with the element on
(an underpowered heater); a ``cools_to`` above the bottom edge settles
there with the element off (regulation not needed against a warm ambient).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from xtce_sim.behavior.spec import _TEMPLATE_RE, ActiveBehavior, ContinuousEffect, Verb
from xtce_sim.behavior.validate import (
    _check_numeric_field,
    _Context,
    _finite_number,
    _parse_center,
    _parse_noise,
)
from xtce_sim.definition import CommandDef

logger = logging.getLogger("xtce_sim.behavior")


@dataclass
class _ActiveRegulate(ActiveBehavior):
    """One running bang-bang loop on a field (registry entry).

    ``value`` is the loop's own float trajectory (same rationale as the
    ramp: integer fields must not stall on sub-0.5 steps). ``element``
    is the loop's memory — which half of the cycle it is in — and flips
    on the trajectory, not the noisy emitted view, so sensor noise never
    chatters the element.
    """

    center: float | str  # number, or "@FIELD" (already template-resolved)
    band: float
    heats_to: float | str
    tau_heat: float
    cools_to: float | str
    tau_cool: float
    value: Optional[float] = None  # seeded from the overlay on first tick
    element: bool = False

    def advance(self, engine, fname: str, dt: float) -> None:
        center = engine._live_number(self, self.center)
        if center is None:
            return
        if self.value is None:
            self.value = engine._current_engineering(fname)
            self.element = self.value < center
        target = engine._live_number(self, self.heats_to if self.element else self.cools_to)
        if target is None:
            return
        tau = self.tau_heat if self.element else self.tau_cool
        step = 1.0 - math.exp(-dt / tau)
        self.value += (target - self.value) * step
        half = self.band / 2.0
        if self.element and self.value >= center + half:
            self.element = False
            logger.debug("[regulate] %s crossed %s; element off", fname, center + half)
        elif not self.element and self.value <= center - half:
            self.element = True
            logger.debug("[regulate] %s crossed %s; element on", fname, center - half)
        engine._store(f"[regulate] {fname}", fname, engine._noisy(self, self.value))


@dataclass
class RegulateEffect(ContinuousEffect):
    center: float | str  # number, or "@FIELD" (possibly templated)
    band: float
    heats_to: float | str
    tau_heat: float
    cools_to: float | str
    tau_cool: float

    @property
    def reference(self):
        return self.center

    def describe(self) -> str:
        return (
            f"{self.field} regulates around {self.center} band {self.band} "
            f"(heats to {self.heats_to} tau={self.tau_heat}s, "
            f"cools to {self.cools_to} tau={self.tau_cool}s){self._noise_suffix()}"
        )

    def describe_active(self, ref) -> str:
        return f"regulating around {ref} band {self.band}"

    def make_active(self, fname: str, ref, rng) -> _ActiveRegulate:
        return _ActiveRegulate(
            field=fname,
            center=ref,
            band=self.band,
            heats_to=self.heats_to,
            tau_heat=self.tau_heat,
            cools_to=self.cools_to,
            tau_cool=self.tau_cool,
            noise=self.noise,
            rng=rng,
        )


def _parse(
    where: str, fname: str, spec: dict, command: CommandDef | None, emit: str, ctx: _Context
) -> Optional[RegulateEffect]:
    required = ("band", "heats_to", "tau_heat", "cools_to", "tau_cool")
    missing = [key for key in required if key not in spec]
    if missing:
        ctx.error(f"{where}: regulate requires {', '.join(required)} (missing {missing})")
        return None
    center = _parse_center(where, "regulate", fname, spec, command, ctx)
    heats_to = _parse_center(where, "heats_to", fname, spec, command, ctx)
    cools_to = _parse_center(where, "cools_to", fname, spec, command, ctx)
    noise = _parse_noise(where, spec, ctx)
    if center is None or heats_to is None or cools_to is None or noise is None:
        return None
    # The engine template-resolves only the center (eff.reference) at
    # execution; a templated side reference would reach the tick loop
    # unresolved and freeze the loop with a warning. Refuse it at load.
    for key, ref in (("heats_to", heats_to), ("cools_to", cools_to)):
        if isinstance(ref, str) and _TEMPLATE_RE.search(ref):
            ctx.error(
                f"{where}: {key} must not use {{templates}} — "
                "only the regulate center is template-resolved at execution"
            )
            return None
    band = spec["band"]
    if not _finite_number(band) or band <= 0:
        ctx.error(f"{where}: band must be a positive number, got {band!r}")
        return None
    for key in ("tau_heat", "tau_cool"):
        tau = spec[key]
        if not _finite_number(tau) or tau <= 0:
            ctx.error(f"{where}: {key} must be a positive number of seconds, got {tau!r}")
            return None
    _check_numeric_field(where, fname, command, ctx)
    return RegulateEffect(
        field=fname,
        center=center,
        band=float(band),
        heats_to=heats_to,
        tau_heat=float(spec["tau_heat"]),
        cools_to=cools_to,
        tau_cool=float(spec["tau_cool"]),
        noise=noise,
        emit=emit,
    )


VERB = Verb(
    name="regulate",
    attrs=frozenset({"band", "heats_to", "tau_heat", "cools_to", "tau_cool", "noise"}),
    continuous=True,
    parse=_parse,
)
