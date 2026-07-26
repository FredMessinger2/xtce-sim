"""The [_models] nav model: the onboard GNSS solution as real telemetry.

A spacecraft knows where it is because a GNSS receiver tells it, and it
downlinks that solution as ordinary telemetry — it does NOT downlink a
TLE (two-line elements are a ground product). This model is that
receiver: every tick it reads the shared [_environment] orbit — the same
world the ADCS flies in and the power model draws sun from — and reports
the state vector in ECI axes, kilometers and kilometers per second.

    [_models.nav]
    kind = "nav"

    [_models.nav.outputs]        # model outputs -> XTCE fields
    NAV_TIMESTAMP = "clock_s"
    NAV_POS_X = "pos_x_km"
    NAV_POS_Y = "pos_y_km"
    NAV_POS_Z = "pos_z_km"
    NAV_VEL_X = "vel_x_kms"
    NAV_VEL_Y = "vel_y_kms"
    NAV_VEL_Z = "vel_z_kms"
    NAV_GPS_VALID = "gps_valid"

Documented approximations:

- The solution is EXACT: the environment's true position with no receiver
  noise, no dilution of precision, no outages — gps_valid always reads
  VALID. Fault injection (dropouts, degraded solutions) is future work.
- The frame is the environment's simplified inertial frame (unperturbed
  circular orbit, fixed sun). Downstream consumers label it J2000; at
  this fidelity the distinction has no observable consequence.
- clock_s is seconds since boot — the sim has no onboard calendar; the
  ground side (the viewer bridge) stamps wall-clock epochs at receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from xtce_sim.dynamics.environment import Environment

#: Numeric output sources (all floats; clock_s rounds into integer fields).
_NUMERIC_KEYS = frozenset(
    {"clock_s", "pos_x_km", "pos_y_km", "pos_z_km", "vel_x_kms", "vel_y_kms", "vel_z_kms"}
)
#: Label-emitting sources and the labels they can produce.
_LABEL_KEYS = {"gps_valid": ("VALID",)}


@dataclass
class NavModelConfig:
    """Validated [_models.<name>] table (kind = "nav"), ready to run."""

    name: str
    outputs: dict[str, str]  # XTCE field -> model source key
    commands: dict[str, str]  # none; kept for the model contract

    def describe(self) -> list[str]:
        return [
            f"model {self.name}: GNSS state vector (ECI, from the shared orbit) "
            f"driving {len(self.outputs)} field(s)"
        ]


class NavModel:
    """The runtime: read the shared orbit, report the state vector.

    Deliberately thin — the orbit does all the work. Position and
    velocity come from the environment's Keplerian orbit in meters and
    convert to kilometers at the boundary (fast at perigee, slow at
    apogee, exactly as the ellipse demands).
    """

    def __init__(self, config: NavModelConfig, environment: Environment) -> None:
        self.config = config
        self.environment = environment
        self.t = 0.0

    def advance(self, dt: float) -> None:
        self.t += dt

    def outputs(self) -> dict[str, object]:
        """Engineering-unit values for every bound field."""
        orbit = self.environment.orbit
        pos = orbit.position(self.t)
        vel = orbit.velocity(self.t)
        values: dict[str, object] = {
            "clock_s": self.t,
            "pos_x_km": pos[0] / 1000.0,
            "pos_y_km": pos[1] / 1000.0,
            "pos_z_km": pos[2] / 1000.0,
            "vel_x_kms": vel[0] / 1000.0,
            "vel_y_kms": vel[1] / 1000.0,
            "vel_z_kms": vel[2] / 1000.0,
            "gps_valid": "VALID",
        }
        return {fname: values[source] for fname, source in self.config.outputs.items()}


def parse_nav_model(
    name: str, body: dict, simdef, error: Callable[[str], None]
) -> NavModelConfig | None:
    """Validate one [_models.<name>] table with kind = "nav".

    Same contract as the other model parses: total (every problem
    reported via ``error``), returns None if anything is wrong.
    """
    # Deferred import: model.py's dispatch imports this module, so the
    # helper import must not run at model.py's own import time.
    from xtce_sim.dynamics.model import _ErrorCounter

    where = f"[_models.{name}]"
    problems = _ErrorCounter(error)
    err = problems.error
    for key in sorted(set(body) - {"kind", "outputs"}):
        err(f"{where}: unknown key {key!r}")
    outputs = _parse_nav_outputs(body.get("outputs", {}), simdef, where, err)
    if problems.count:
        return None
    return NavModelConfig(name=name, outputs=outputs, commands={})


def _parse_nav_outputs(table, simdef, where: str, err) -> dict[str, str]:
    if not isinstance(table, dict) or not table:
        err(f"{where}.outputs: at least one field binding is required")
        return {}
    fields = {f.name: f for p in simdef.packets for f in p.fields}
    outputs = {}
    for fname, source in table.items():
        field = fields.get(fname)
        if field is None:
            err(f"{where}.outputs: unknown field {fname!r}")
            continue
        problem = _nav_binding_problem(field, source)
        if problem is not None:
            err(f"{where}.outputs: {fname}: {problem}")
            continue
        outputs[fname] = source
    return outputs


def _nav_binding_problem(field, source) -> str | None:
    """Why this source's values could not survive storage into this field
    (None when the binding is sound) — the ADCS rule, sized for nav."""
    labels = _LABEL_KEYS.get(source)
    if labels is not None:
        if field.python_type in ("string", "bytes"):
            return None
        if not field.enumerations:
            return (
                f"source {source!r} emits labels but the field is "
                f"{field.python_type} with no enumeration"
            )
        missing = [label for label in labels if label not in field.enumerations]
        if missing:
            return (
                f"source {source!r} label(s) {', '.join(missing)} "
                "missing from the field's enumeration"
            )
        return None
    if source not in _NUMERIC_KEYS:
        return f"unknown source {source!r}"
    if field.python_type in ("string", "bytes") or field.enumerations:
        return f"source {source!r} is numeric but the field is not"
    if field.calibrator is not None and not field.calibrator.is_invertible:
        return f"source {source!r} needs an invertible calibrator on the field"
    return None
