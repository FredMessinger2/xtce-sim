"""The power model: parsing, validation, and the electrical physics."""

import math
from pathlib import Path

import pytest

from xtce_sim.definition import SimDefinition
from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.environment import CircularOrbit, Environment
from xtce_sim.dynamics.model import parse_model
from xtce_sim.dynamics.power import PowerModel, parse_power_model

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
IMAGING = EXAMPLES / "imaging_sat/imaging_sat.xml"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(IMAGING)


def _minimal_table(**overrides):
    table = {
        "kind": "power",
        "outputs": {
            "PWR_SOLAR_CURRENT": "solar_current",
            "PWR_BATTERY_VOLTAGE": "battery_voltage",
            "PWR_BATTERY_CURRENT": "battery_current",
        },
    }
    table.update(overrides)
    return table


def _parse(simdef, table):
    errors = []
    cfg = parse_power_model("power", table, simdef, errors.append)
    return cfg, errors


def _sunlit_env() -> Environment:
    # phase0 = 0 puts the vehicle on the sunward side of the terminator.
    return Environment(orbit=CircularOrbit(altitude=500e3), sun_direction=(1.0, 0.0, 0.0))


def _eclipsed_env() -> Environment:
    # phase0 = pi puts the vehicle squarely inside the shadow cylinder.
    return Environment(
        orbit=CircularOrbit(altitude=500e3, inclination=0.0, phase0=math.pi),
        sun_direction=(1.0, 0.0, 0.0),
    )


def _model(simdef, env, states=None, attitude=None, **overrides):
    cfg, errors = _parse(simdef, _minimal_table(**overrides))
    assert errors == [], errors
    reader = (states or {}).get
    return PowerModel(cfg, env, reader, attitude)


# ---- parsing ----------------------------------------------------------------


def test_kind_dispatch_builds_a_power_config(simdef):
    errors = []
    cfg = parse_model("power", _minimal_table(), simdef, errors.append)
    assert errors == [] and cfg is not None
    assert cfg.describe()[0].startswith("model power: EPS (2x60 W wings")
    assert cfg.commands == {}


def test_unknown_kind_names_both_kinds(simdef):
    errors = []
    assert parse_model("x", {"kind": "warp"}, simdef, errors.append) is None
    assert errors == ["[_models.x]: unknown model kind 'warp' (one of 'adcs', 'power')"]


def test_parse_rejects_bad_tables(simdef):
    cfg, errors = _parse(simdef, _minimal_table(array={"wing_power_w": -5}))
    assert cfg is None and any("wing_power_w: must be a positive number" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(array={"wingz": 2}))
    assert cfg is None and any("array: unknown key 'wingz'" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(battery={"initial_soc": 1.5}))
    assert cfg is None and any("initial_soc: must be a number between 0 and 1" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(array={"mppt_efficiency": 1.2}))
    assert cfg is None and any("mppt_efficiency: cannot exceed 1.0" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(outputs={}))
    assert cfg is None and any("at least one field binding" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(outputs={"PWR_SOLAR_CURRENT": "warp_flux"}))
    assert cfg is None and any("unknown source 'warp_flux'" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(outputs={"NOT_A_FIELD": "solar_current"}))
    assert cfg is None and any("unknown field 'NOT_A_FIELD'" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(warp={}))
    assert cfg is None and any("unknown key 'warp'" in e for e in errors)


def test_parse_checks_loads_against_the_icd(simdef):
    cfg, errors = _parse(simdef, _minimal_table(loads={"WARP": 1.0}))
    assert cfg is None and any("no field 'PWR_WARP_STATE'" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(loads={"CDH": -0.3}))
    assert cfg is None and any("loads.CDH: must be a positive number" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(loads={"CDH": 0.3}))
    assert errors == []
    assert cfg.loads[0].state_field == "PWR_CDH_STATE" and cfg.loads[0].on_raw == 1


# ---- physics ----------------------------------------------------------------


def test_sunlit_no_loads_charges_at_the_controller_limit(simdef):
    m = _model(simdef, _sunlit_env())
    # 2 wings x 60 W at Vmp 28 V: array current ~4.29 A; with no loads the
    # battery takes the controller max (taper starts above 90% charge).
    assert m.outputs()["PWR_SOLAR_CURRENT"] == pytest.approx(120.0 / 28.0)
    assert m.outputs()["PWR_BATTERY_CURRENT"] == pytest.approx(2.0)


def test_eclipse_discharges_through_the_loads(simdef):
    states = {"PWR_CDH_STATE": 1, "PWR_ADCS_STATE": 1}
    m = _model(
        simdef, _eclipsed_env(), states=states, loads={"CDH": 0.3, "ADCS": 0.5}
    )
    out = m.outputs()
    assert out["PWR_SOLAR_CURRENT"] == 0.0
    assert out["PWR_BATTERY_CURRENT"] == pytest.approx(-0.8)
    # discharge sags the terminal voltage below open-circuit
    ocv = m._open_circuit_voltage()
    assert out["PWR_BATTERY_VOLTAGE"] == pytest.approx(ocv - 0.8 * 0.15)


def test_charge_state_integrates_and_clamps(simdef):
    # A near-dead array (must be positive) so the orbit re-entering sunlight
    # mid-hour cannot recharge anything: this measures pure integration.
    states = {"PWR_CDH_STATE": 1}
    m = _model(
        simdef,
        _eclipsed_env(),
        states=states,
        loads={"CDH": 1.0},
        array={"wing_power_w": 1e-6},
    )
    soc0 = m.soc
    m.advance(3600.0)  # one hour at 1 A out of 10 Ah
    assert m.soc == pytest.approx(soc0 - 0.1, abs=1e-4)
    # and it clamps at empty rather than going negative
    for _ in range(20):
        m.advance(3600.0)
    assert m.soc == 0.0


def test_charging_tapers_near_full_and_shunts_at_full(simdef):
    m = _model(simdef, _sunlit_env(), battery={"initial_soc": 0.97})
    # headroom (1 - 0.97)/0.10 = 0.3 of the 2 A limit
    assert m.outputs()["PWR_BATTERY_CURRENT"] == pytest.approx(0.6)
    full = _model(simdef, _sunlit_env(), battery={"initial_soc": 1.0})
    assert full.outputs()["PWR_BATTERY_CURRENT"] == 0.0  # surplus shunted


def test_single_axis_tracking_costs_the_along_axis_component(simdef):
    # Identity attitude: sun (ECI +X) is perpendicular to the body-Y wing
    # axis, so the wings can face it squarely.
    m = _model(simdef, _sunlit_env(), attitude=lambda: al.QUAT_IDENTITY)
    assert m.outputs()["PWR_SOLAR_CURRENT"] == pytest.approx(120.0 / 28.0)
    # Rotate the body 90 deg about Z: the sun now lies ALONG the wing
    # axis, where no wing rotation can recover it — generation dies.
    q = al.quat_from_axis_angle((0.0, 0.0, 1.0), math.pi / 2.0)
    edge_on = _model(simdef, _sunlit_env(), attitude=lambda: q)
    assert edge_on.outputs()["PWR_SOLAR_CURRENT"] == pytest.approx(0.0, abs=1e-9)
    # And 45 deg costs exactly cos(45): the honest cosine law.
    q45 = al.quat_from_axis_angle((0.0, 0.0, 1.0), math.pi / 4.0)
    slewed = _model(simdef, _sunlit_env(), attitude=lambda: q45)
    assert slewed.outputs()["PWR_SOLAR_CURRENT"] == pytest.approx(
        (120.0 / 28.0) * math.cos(math.pi / 4.0)
    )


def test_solar_voltage_reads_vmp_in_sun_and_zero_in_shadow(simdef):
    table = {"outputs": {"PWR_SOLAR_VOLTAGE": "solar_voltage"}}
    lit = _model(simdef, _sunlit_env(), **table)
    dark = _model(simdef, _eclipsed_env(), **table)
    assert lit.outputs()["PWR_SOLAR_VOLTAGE"] == pytest.approx(28.0)
    assert dark.outputs()["PWR_SOLAR_VOLTAGE"] == 0.0


def test_switch_states_gate_the_draws(simdef):
    states = {"PWR_CDH_STATE": 1, "PWR_IMAGER_STATE": 0}
    m = _model(
        simdef, _eclipsed_env(), states=states, loads={"CDH": 0.3, "IMAGER": 0.2}
    )
    assert m.outputs()["PWR_BATTERY_CURRENT"] == pytest.approx(-0.3)
    states["PWR_IMAGER_STATE"] = 1  # SET_POWER IMAGER ON, as the engine would
    m.advance(1.0)
    assert m.outputs()["PWR_BATTERY_CURRENT"] == pytest.approx(-0.5)
    states["PWR_IMAGER_STATE"] = 2  # STANDBY draws nothing in this bank
    m.advance(1.0)
    assert m.outputs()["PWR_BATTERY_CURRENT"] == pytest.approx(-0.3)


def test_without_an_attitude_source_wings_are_sun_pointed(simdef):
    m = _model(simdef, _sunlit_env(), attitude=None)
    assert m.outputs()["PWR_SOLAR_CURRENT"] == pytest.approx(120.0 / 28.0)
