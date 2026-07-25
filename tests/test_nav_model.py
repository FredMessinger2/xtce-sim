"""The nav model: parsing, the state-vector math, and engine integration."""

import math
from pathlib import Path

import pytest

from xtce_sim.definition import SimDefinition
from xtce_sim.dynamics.environment import CircularOrbit, Environment
from xtce_sim.dynamics.model import parse_model
from xtce_sim.dynamics.nav import NavModel, parse_nav_model

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
IMAGING = EXAMPLES / "imaging_sat/imaging_sat.xml"

_FULL_OUTPUTS = {
    "NAV_TIMESTAMP": "clock_s",
    "NAV_POS_X": "pos_x_km",
    "NAV_POS_Y": "pos_y_km",
    "NAV_POS_Z": "pos_z_km",
    "NAV_VEL_X": "vel_x_kms",
    "NAV_VEL_Y": "vel_y_kms",
    "NAV_VEL_Z": "vel_z_kms",
    "NAV_GPS_VALID": "gps_valid",
}


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(IMAGING)


def _parse(simdef, table):
    errors = []
    cfg = parse_nav_model("nav", table, simdef, errors.append)
    return cfg, errors


def _model(simdef, env=None, outputs=_FULL_OUTPUTS):
    cfg, errors = _parse(simdef, {"kind": "nav", "outputs": outputs})
    assert errors == [], errors
    return NavModel(cfg, env or Environment(orbit=CircularOrbit(altitude=500e3)))


# ---- parsing ----------------------------------------------------------------


def test_kind_dispatch_builds_a_nav_config(simdef):
    errors = []
    cfg = parse_model("nav", {"kind": "nav", "outputs": _FULL_OUTPUTS}, simdef, errors.append)
    assert errors == [] and cfg is not None
    assert cfg.describe() == [
        "model nav: GNSS state vector (ECI, from the shared orbit) driving 8 field(s)"
    ]
    assert cfg.commands == {}


def test_parse_rejects_bad_tables(simdef):
    cfg, errors = _parse(simdef, {"kind": "nav", "warp": {}})
    assert cfg is None and any("unknown key 'warp'" in e for e in errors)
    cfg, errors = _parse(simdef, {"kind": "nav", "outputs": {}})
    assert cfg is None and any("at least one field binding" in e for e in errors)
    cfg, errors = _parse(simdef, {"kind": "nav", "outputs": {"WARP_X": "pos_x_km"}})
    assert cfg is None and any("unknown field 'WARP_X'" in e for e in errors)
    cfg, errors = _parse(simdef, {"kind": "nav", "outputs": {"NAV_POS_X": "warp_flux"}})
    assert cfg is None and any("unknown source 'warp_flux'" in e for e in errors)
    # Label source into a plain float field: refused at load, not warned forever.
    cfg, errors = _parse(simdef, {"kind": "nav", "outputs": {"NAV_POS_X": "gps_valid"}})
    assert cfg is None and any("emits labels" in e for e in errors)
    # Numeric source into an enum field: same rule, other direction.
    cfg, errors = _parse(simdef, {"kind": "nav", "outputs": {"NAV_GPS_VALID": "pos_x_km"}})
    assert cfg is None and any("is numeric but the field is not" in e for e in errors)


# ---- the state vector -------------------------------------------------------


def test_state_vector_matches_the_shared_orbit(simdef):
    orbit = CircularOrbit(altitude=500e3)
    m = _model(simdef, Environment(orbit=orbit))
    m.advance(600.0)
    out = m.outputs()
    pos = (out["NAV_POS_X"], out["NAV_POS_Y"], out["NAV_POS_Z"])
    vel = (out["NAV_VEL_X"], out["NAV_VEL_Y"], out["NAV_VEL_Z"])
    expected = orbit.position(600.0)
    assert pos == pytest.approx(tuple(c / 1000.0 for c in expected))
    # Circular-orbit invariants: |r| = radius, |v| = sqrt(mu/r), v ⊥ r.
    assert math.hypot(*pos) == pytest.approx(orbit.radius / 1000.0)
    assert math.hypot(*vel) == pytest.approx(orbit.rate * orbit.radius / 1000.0)
    dot = sum(p * v for p, v in zip(pos, vel))
    assert dot == pytest.approx(0.0, abs=1e-9)
    assert out["NAV_TIMESTAMP"] == 600.0
    assert out["NAV_GPS_VALID"] == "VALID"


def test_position_actually_moves_along_the_orbit(simdef):
    m = _model(simdef)
    first = m.outputs()
    m.advance(60.0)
    second = m.outputs()
    moved = math.hypot(
        second["NAV_POS_X"] - first["NAV_POS_X"],
        second["NAV_POS_Y"] - first["NAV_POS_Y"],
        second["NAV_POS_Z"] - first["NAV_POS_Z"],
    )
    # ~7.6 km/s for one minute is ~456 km of arc.
    assert moved == pytest.approx(60.0 * m.environment.orbit.rate * m.environment.orbit.radius / 1000.0, rel=1e-3)


def test_engine_carries_a_live_state_vector_from_the_first_beacon(simdef):
    from xtce_sim.behavior.engine import BehaviorEngine
    from xtce_sim.behavior.loader import load_behavior

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    engine = BehaviorEngine(spec, simdef)
    # Model outputs are seeded at construction: the first packet is real.
    pos = (engine.state["NAV_POS_X"], engine.state["NAV_POS_Y"], engine.state["NAV_POS_Z"])
    assert math.hypot(*pos) == pytest.approx(spec.environment.orbit.radius / 1000.0, rel=1e-6)
    assert engine.state["NAV_GPS_VALID"] == 1  # the VALID label's raw value
    engine.tick(60.0)
    moved = (engine.state["NAV_POS_X"], engine.state["NAV_POS_Y"], engine.state["NAV_POS_Z"])
    assert moved != pos
