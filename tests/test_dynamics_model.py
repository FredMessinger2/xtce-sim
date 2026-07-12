"""The [_models] construct: parsing, validation, units, and the runtime."""

import logging
import math
from pathlib import Path

import pytest

from xtce_sim.definition import SimDefinition
from xtce_sim.dynamics.model import (
    AdcsModel,
    parse_model,
)
from xtce_sim.dynamics.modes import AdcsMode

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
IMAGING = EXAMPLES / "imaging_sat/imaging_sat.xml"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(IMAGING)


def _minimal_table(**overrides):
    table = {
        "body": {"inertia": [12.0, 14.0, 9.0]},
        "wheels": [
            {"axis": [0.6, 0.0, 0.8], "inertia": 0.02, "max_torque": 0.05, "max_speed": 600.0},
            {"axis": [-0.6, 0.0, 0.8], "inertia": 0.02, "max_torque": 0.05, "max_speed": 600.0},
            {"axis": [0.0, 0.6, 0.8], "inertia": 0.02, "max_torque": 0.05, "max_speed": 600.0},
            {"axis": [0.0, -0.6, 0.8], "inertia": 0.02, "max_torque": 0.05, "max_speed": 600.0},
        ],
        "orbit": {"altitude_km": 500.0},
        "outputs": {"ADCS_MODE": "mode", "ADCS_WHEEL1_SPEED": "wheel1_speed_rpm"},
    }
    table.update(overrides)
    return table


def _parse(simdef, table):
    errors = []
    cfg = parse_model("adcs", table, simdef, errors.append)
    return cfg, errors


def test_minimal_model_parses(simdef):
    cfg, errors = _parse(simdef, _minimal_table())
    assert errors == []
    assert cfg is not None
    assert len(cfg.wheels) == 4
    assert cfg.orbit.altitude == pytest.approx(500e3)
    # Conventional command names resolve without a [commands] table.
    assert cfg.commands["set_mode"] == "ADCS_SET_MODE"
    assert len(cfg.commands) == 11
    assert cfg.describe()[0].startswith("model adcs: rigid-body ADCS (4 wheels")


def test_parse_reports_every_problem_at_once(simdef):
    table = _minimal_table(
        outputs={"NOT_A_FIELD": "mode", "ADCS_MODE": "not_a_source"},
        commands={"bogus_role": "ADCS_SET_MODE", "set_mode": "NO_SUCH_COMMAND"},
        substep=-1.0,
    )
    table["unknown_top_key"] = 1
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    text = "\n".join(errors)
    assert "unknown field 'NOT_A_FIELD'" in text
    assert "unknown source 'not_a_source'" in text
    assert "unknown role 'bogus_role'" in text
    assert "unknown command 'NO_SUCH_COMMAND'" in text
    assert "substep: must be a positive number" in text
    assert "unknown key 'unknown_top_key'" in text


def test_parse_rejects_bad_physics(simdef):
    cfg, errors = _parse(simdef, _minimal_table(body={"inertia": [1.0, -2.0, 3.0]}))
    assert cfg is None
    assert any("moments must be positive" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(wheels=[]))
    assert cfg is None
    assert any("at least one" in e for e in errors)
    bad_wheel = [{"axis": [0, 0, 0], "inertia": 0.02, "max_torque": 0.05, "max_speed": 600.0}]
    cfg, errors = _parse(simdef, _minimal_table(wheels=bad_wheel))
    assert cfg is None
    assert any("cannot be the zero vector" in e for e in errors)


def test_parse_rejects_unknown_kind_and_wheel_indexed_sources(simdef):
    cfg, errors = _parse(simdef, _minimal_table(kind="thermal"))
    assert cfg is None
    assert any("unknown model kind" in e for e in errors)
    # A wheel5 source with only 4 wheels is not a real output.
    table = _minimal_table()
    table["outputs"]["ADCS_WHEEL4_SPEED"] = "wheel5_speed_rpm"
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    assert any("unknown source 'wheel5_speed_rpm'" in e for e in errors)


def test_satellite_without_adcs_commands_leaves_roles_unwired(simdef):
    # A satellite that binds fields but declares none of the conventional
    # command names simply gets an uncommandable model — no error, the
    # outputs still stream.
    from types import SimpleNamespace

    field = SimpleNamespace(
        name="F1",
        python_type="uint8",
        enumerations={m.name: m.value for m in AdcsMode},
        calibrator=None,
    )
    stub = SimpleNamespace(
        packets=[SimpleNamespace(fields=[field])],
        command_by_name=lambda name: None,
    )
    table = _minimal_table(outputs={"F1": "mode"})
    errors = []
    cfg = parse_model("adcs", table, stub, errors.append)
    assert errors == []
    assert cfg is not None
    assert cfg.commands == {}


def test_command_role_argument_check(simdef):
    # Binding a role to a command that lacks the role's arguments is an
    # error, not a runtime surprise.
    cfg, errors = _parse(simdef, _minimal_table(commands={"wheel_set_speed": "ADCS_DESATURATE"}))
    assert cfg is None
    assert any("lacks argument(s) WheelId, Speed" in e for e in errors)


def test_parse_rejects_malformed_tables(simdef):
    cfg, errors = _parse(simdef, "not a table")
    assert cfg is None and errors == ["[_models.adcs]: must be a table"]
    cfg, errors = _parse(simdef, _minimal_table(body={"inertia": "wrong"}))
    assert any("must be [Ixx, Iyy, Izz]" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(orbit="wrong"))
    assert any("orbit: must be a table" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(orbit={"altitude_km": -1.0}))
    assert any("altitude_km: must be a positive number" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(outputs={}))
    assert any("at least one field binding" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(commands="wrong"))
    assert any("commands: must be a table" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(sensors={"seed": "x"}))
    assert any("seed: must be an integer" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(wheels=["not a table"]))
    assert any("wheels[1]: must be a table" in e for e in errors)
    bad_axis = [{"axis": "not a vector", "inertia": 0.02, "max_torque": 0.05, "max_speed": 600.0}]
    cfg, errors = _parse(simdef, _minimal_table(wheels=bad_axis))
    assert any("3-element number array" in e for e in errors)


def test_parse_reports_non_table_subtables_instead_of_crashing(simdef):
    # A user writing `sun = 5` must get the courteous error, not a traceback.
    cfg, errors = _parse(
        simdef, _minimal_table(sun=5, controller="x", mtq=[1], sensors=3.5, body="oops")
    )
    assert cfg is None
    text = "\n".join(errors)
    for key in ("sun", "controller", "mtq", "sensors", "body"):
        assert f"{key}: must be a table" in text


def test_parse_reports_non_numeric_orbit_angles_instead_of_crashing(simdef):
    # TOML makes `inclination_deg = "51.6"` a one-keystroke mistake.
    table = _minimal_table(
        orbit={"altitude_km": 500.0, "inclination_deg": "51.6", "raan_deg": True}
    )
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    assert any("inclination_deg: must be a finite number" in e for e in errors)
    assert any("raan_deg: must be a finite number" in e for e in errors)


def test_parse_rejects_non_finite_numbers(simdef):
    # inf and nan are valid TOML floats; unchecked they crash model
    # construction (inf response_time), crash the init tick (inf orbit
    # angles via math.cos), or NaN-flood every wheel field per tick.
    cfg, errors = _parse(simdef, _minimal_table(controller={"response_time": math.inf}))
    assert cfg is None
    assert any("response_time: must be a positive number" in e for e in errors)
    table = _minimal_table(orbit={"altitude_km": 500.0, "inclination_deg": math.inf})
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    assert any("inclination_deg: must be a finite number" in e for e in errors)
    wheels = _minimal_table()["wheels"]
    wheels[0] = dict(wheels[0], max_torque=math.nan)
    cfg, errors = _parse(simdef, _minimal_table(wheels=wheels))
    assert cfg is None
    assert any("max_torque: must be a positive number" in e for e in errors)
    wheels[0] = dict(wheels[0], max_torque=0.05, friction=math.inf)
    cfg, errors = _parse(simdef, _minimal_table(wheels=wheels))
    assert cfg is None
    assert any("friction: must be a non-negative number" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(body={"inertia": [math.nan, 14.0, 9.0]}))
    assert cfg is None
    assert any("moments must be positive" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(sun={"direction": [math.inf, 0.0, 0.0]}))
    assert cfg is None
    assert any("3-element number array" in e for e in errors)


def test_parse_rejects_bad_friction(simdef):
    # Negative friction previously parsed clean, then crashed the engine
    # inside AdcsModel.__init__ — the validator must catch what the plant
    # constructor would reject.
    wheels = _minimal_table()["wheels"]
    wheels[0] = dict(wheels[0], friction=-0.5)
    cfg, errors = _parse(simdef, _minimal_table(wheels=wheels))
    assert cfg is None
    assert any("friction: must be a non-negative number" in e for e in errors)
    wheels[0] = dict(wheels[0], friction="sticky")
    cfg, errors = _parse(simdef, _minimal_table(wheels=wheels))
    assert cfg is None
    assert any("friction: must be a non-negative number" in e for e in errors)


def test_parse_rejects_unknown_subtable_keys(simdef):
    # A typo'd knob must not silently fall back to its default physics.
    table = _minimal_table(
        body={"inertia": [12.0, 14.0, 9.0], "intertia": 5},
        controller={"respons_time": 3.0},
        orbit={"altitud_km": 400.0},
        mtq={"max_dipol": 2.0},
    )
    table["wheels"][0]["frction"] = 0.1
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    text = "\n".join(errors)
    assert "body: unknown key 'intertia'" in text
    assert "controller: unknown key 'respons_time'" in text
    assert "orbit: unknown key 'altitud_km'" in text
    assert "mtq: unknown key 'max_dipol'" in text
    assert "wheels[1]: unknown key 'frction'" in text


def test_parse_rejects_boolean_seed(simdef):
    cfg, errors = _parse(simdef, _minimal_table(sensors={"seed": True}))
    assert cfg is None
    assert any("seed: must be an integer" in e for e in errors)


def test_parse_bounds_substep(simdef):
    # Too coarse: RK4 through the wheel servos goes silently wrong (25% off
    # at substep=30) long before it goes unstable. Too fine: millions of
    # steps per beacon tick. Both are load errors, not runtime surprises.
    cfg, errors = _parse(simdef, _minimal_table(substep=30.0))
    assert cfg is None
    assert any("between 0.001 and 1.0 seconds" in e for e in errors)
    cfg, errors = _parse(simdef, _minimal_table(substep=1e-6))
    assert cfg is None
    assert any("between 0.001 and 1.0 seconds" in e for e in errors)
    # Sampled-loop stability: the controller acts once per substep, so the
    # step must be well inside the commanded response time.
    cfg, errors = _parse(simdef, _minimal_table(substep=0.5, controller={"response_time": 1.0}))
    assert cfg is None
    assert any("response_time/5" in e for e in errors)


def test_output_binding_type_mismatch_is_load_error(simdef):
    # A label source on a float field would warn on every tick forever
    # while the synth generator invents values for a "model-owned" field.
    table = _minimal_table()
    table["outputs"]["ADCS_WHEEL1_SPEED"] = "mode"
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    assert any("ADCS_WHEEL1_SPEED" in e and "emits labels" in e for e in errors)
    # A numeric source on an enum field is equally nonsense.
    table = _minimal_table()
    table["outputs"]["ADCS_MODE"] = "wheel1_speed_rpm"
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    assert any("ADCS_MODE" in e and "is numeric but the field is not" in e for e in errors)
    # Right shape, wrong labels: a mode source cannot live in an OK/FAULT enum.
    table = _minimal_table()
    del table["outputs"]["ADCS_MODE"]
    table["outputs"]["ADCS_ST_HEALTH"] = "mode"
    cfg, errors = _parse(simdef, table)
    assert cfg is None
    assert any(
        "ADCS_ST_HEALTH" in e and "missing from the field's enumeration" in e for e in errors
    )


def test_output_binding_edge_field_types():
    # A label source may land on a raw string field; a numeric source
    # cannot land behind a non-invertible calibrator (the engineering
    # value could never be stored as counts).
    from types import SimpleNamespace

    text = SimpleNamespace(name="TXT", python_type="string", enumerations=None, calibrator=None)
    squashed = SimpleNamespace(
        name="SQ",
        python_type="uint16",
        enumerations=None,
        calibrator=SimpleNamespace(is_invertible=False),
    )
    stub = SimpleNamespace(
        packets=[SimpleNamespace(fields=[text, squashed])],
        command_by_name=lambda name: None,
    )
    errors = []
    cfg = parse_model("adcs", _minimal_table(outputs={"TXT": "mode"}), stub, errors.append)
    assert errors == []
    assert cfg is not None
    errors = []
    cfg = parse_model("adcs", _minimal_table(outputs={"SQ": "momentum_total"}), stub, errors.append)
    assert cfg is None
    assert any("invertible calibrator" in e for e in errors)


# ---------------------------------------------------------------------------
# Runtime


def _model(simdef, **overrides):
    cfg, errors = _parse(simdef, _minimal_table(**overrides))
    assert errors == []
    return AdcsModel(cfg)


def test_advance_uses_fixed_substeps_without_drift(simdef):
    model = _model(simdef)
    for _ in range(3):
        model.advance(0.033)  # each smaller than the 0.1 s substep
    assert model.t == pytest.approx(0.0)  # not enough for one substep yet
    model.advance(0.033)
    assert model.t == pytest.approx(0.1)  # one whole substep fired
    model.advance(5.0)
    # Total simulated time only ever differs from wall input by < 1 substep.
    total_in = 4 * 0.033 + 5.0
    assert total_in - model.t < 0.1


def test_wheel_speed_telemetry_is_rpm(simdef):
    model = _model(simdef)
    model.machine.plant.command_speed(0, 100.0)  # rad/s, internal units
    model.advance(120.0)
    rpm = model.outputs()["ADCS_WHEEL1_SPEED"]
    # Pinned to the literal, NOT to RAD_S_TO_RPM: the command path divides
    # by the constant and the telemetry path multiplies, so a wrong
    # constant cancels in any round-trip test. 100 rad/s IS 954.93 RPM.
    assert rpm == pytest.approx(954.93, rel=0.01)


def test_outputs_only_contain_bound_fields(simdef):
    model = _model(simdef)
    assert set(model.outputs()) == {"ADCS_MODE", "ADCS_WHEEL1_SPEED"}
    assert model.outputs()["ADCS_MODE"] == "STANDBY"


def test_mag_field_reported_in_microtesla(simdef):
    table = _minimal_table()
    table["outputs"]["ADCS_MAG_X"] = "mag_x_ut"
    cfg, errors = _parse(simdef, table)
    assert errors == []
    model = AdcsModel(cfg)
    model.advance(1.0)
    # LEO dipole magnitude is tens of µT; Tesla would be ~1e-5.
    mag = model.machine.mag_body[0] * 1e6
    assert model.outputs()["ADCS_MAG_X"] == pytest.approx(mag)


def test_same_config_same_telemetry(simdef):
    a, b = _model(simdef), _model(simdef)
    for _ in range(20):
        a.advance(1.0)
        b.advance(1.0)
    assert a.outputs() == b.outputs()


def test_slew_command_switches_mode_and_normalizes(simdef):
    model = _model(simdef)
    applied = model.apply_command(
        "ADCS_SLEW_TO_QUATERNION", {"Q1": 0.0, "Q2": 0.0, "Q3": 0.0, "Q4": 2.0}
    )
    assert applied and "slew" in applied[0]
    assert model.machine.mode is AdcsMode.INERTIAL_POINT
    assert model.machine.inertial_target == pytest.approx((0.0, 0.0, 0.0, 1.0))


def test_bad_command_values_warn_and_apply_nothing(simdef, caplog):
    model = _model(simdef)
    with caplog.at_level(logging.WARNING, logger="xtce_sim.dynamics"):
        zero_quat = model.apply_command(
            "ADCS_SLEW_TO_QUATERNION",
            {"Q1": 0.0, "Q2": 0.0, "Q3": 0.0, "Q4": 0.0},
        )
        track_without = model.apply_command("ADCS_SET_MODE", {"Mode": "TARGET_TRACK"})
    assert zero_quat == []
    assert track_without == []
    assert "zero-length quaternion" in caplog.text
    assert "no ground target" in caplog.text
    assert model.machine.mode is AdcsMode.STANDBY  # nothing changed


def test_gyro_bias_command_converts_degrees(simdef):
    model = _model(simdef)
    model.apply_command("ADCS_SET_GYRO_BIAS", {"BiasX": 0.5, "BiasY": 0.0, "BiasZ": -0.25})
    bias = model.machine.estimator.bias_estimate
    assert bias[0] == pytest.approx(math.radians(0.5))
    assert bias[2] == pytest.approx(math.radians(-0.25))


def test_wheel_enable_disable_route_through(simdef):
    model = _model(simdef)
    assert model.handles("ADCS_WHEEL_DISABLE")
    assert not model.handles("HEATER_ON")
    assert model.apply_command("ADCS_WHEEL_DISABLE", {"WheelId": 3}) == ["wheel 3 disabled"]
    assert not model.machine.plant.commands[2].enabled
    assert model.apply_command("ADCS_WHEEL_ENABLE", {"WheelId": 3}) == ["wheel 3 enabled"]
    assert model.machine.plant.commands[2].enabled


def test_desaturate_and_reset_route_through(simdef):
    model = _model(simdef)
    assert model.apply_command("ADCS_DESATURATE", {}) == ["momentum dump engaged"]
    assert model.machine.desaturating
    model.advance(60.0)
    applied = model.apply_command("ADCS_RESET_ESTIMATOR", {})
    assert applied == ["estimator reset (reconverging)"]
    assert model.machine.estimator.state.value == "CONVERGING"


def test_pointing_error_telemetry_gates_on_attitude_hold(simdef):
    # ADCS_POINTING_ERR means "angle to the commanded attitude"; without a
    # hold in force there is no commanded attitude, and a stale target must
    # not masquerade as a live error.
    table = _minimal_table()
    table["outputs"]["ADCS_POINTING_ERR"] = "pointing_err_deg"
    cfg, errors = _parse(simdef, table)
    assert errors == []
    model = AdcsModel(cfg)
    assert model.outputs()["ADCS_POINTING_ERR"] == 0.0  # STANDBY at boot
    model.apply_command(
        "ADCS_SLEW_TO_QUATERNION", {"Q1": 0.0, "Q2": 0.0, "Q3": 0.7071, "Q4": 0.7071}
    )
    model.advance(1.0)
    assert model.outputs()["ADCS_POINTING_ERR"] > 10.0  # mid-slew, real error
    model.apply_command("ADCS_SET_MODE", {"Mode": "STANDBY"})
    model.advance(1.0)
    assert model.outputs()["ADCS_POINTING_ERR"] == 0.0  # hold released


def test_momentum_flag_trips_near_wheel_speed_limit(simdef):
    table = _minimal_table()
    table["outputs"]["ADCS_MOMENTUM_FLAG"] = "momentum_flag"
    cfg, errors = _parse(simdef, table)
    assert errors == []
    model = AdcsModel(cfg)
    assert model.outputs()["ADCS_MOMENTUM_FLAG"] == "OK"
    for i in range(4):
        model.machine.plant.command_speed(i, 590.0)  # 98% of the 600 rail
    model.advance(260.0)  # torque-limited spin-up to past the 80% threshold
    assert model.outputs()["ADCS_MOMENTUM_FLAG"] == "NEAR_SATURATION"


def test_wheel_current_and_temp_reflect_torque_magnitude(simdef):
    table = _minimal_table()
    table["outputs"]["ADCS_WHEEL1_CURRENT"] = "wheel1_current_a"
    table["outputs"]["ADCS_WHEEL1_TEMP"] = "wheel1_temp_c"
    cfg, errors = _parse(simdef, table)
    assert errors == []
    model = AdcsModel(cfg)
    # Spin DOWN so delivered torque is negative: current is |torque|-based
    # and must rise above idle, not fall below it.
    model.machine.plant.command_speed(0, -300.0)
    model.advance(0.5)  # mid-transient, servo railed at -max_torque
    out = model.outputs()
    assert out["ADCS_WHEEL1_CURRENT"] == pytest.approx(2.05)  # 0.05 + 0.05/0.025
    assert out["ADCS_WHEEL1_TEMP"] == pytest.approx(45.0)  # 20 + 25 at full current
