"""Behavior-sidecar loader/validation tests (xtce_sim.behavior + inspect wiring).

Validation is strict and total: every reference is checked against the
resolved SimDefinition and all problems are reported in one BehaviorError.
"""

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim import behavior
from xtce_sim.behavior import (
    BehaviorError,
    CopyArgEffect,
    IncrementEffect,
    RampEffect,
    SetEffect,
    load_behavior,
    sidecar_path,
)
from xtce_sim.cli import main
from xtce_sim.definition import SimDefinition

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"
IMAGING = EXAMPLES / "imaging_sat/imaging_sat.xml"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(IMAGING)


def _load(tmp_path, simdef, text: str):
    p = tmp_path / "t.behavior.toml"
    p.write_text(text)
    return load_behavior(p, simdef)


def _errors(tmp_path, simdef, text: str) -> str:
    with pytest.raises(BehaviorError) as exc:
        _load(tmp_path, simdef, text)
    return str(exc.value)


# ---- the shipped sidecar is the canonical happy path -----------------------


def test_shipped_imaging_sidecar_validates(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    # 6 non-ADCS seeds (incl. the imager's dark boot) + 5 boot power states
    # + the beacon state (the ADCS model owns its fields)
    assert len(spec.initial) == 12
    assert set(spec.commands) == {
        "SET_MODE",
        "SET_POWER",
        "ENABLE_BEACON",
        "HEATER_ON",
        "HEATER_OFF",
        "HEATER_AUTO",
        "SET_HEATER_SETPOINT",
        "IMAGER_ON",
        "IMAGER_OFF",
        "SET_EXPOSURE",
        "TAKE_IMAGE",
    }
    # HEATER_ON is the manual override: open-loop toward element capability.
    ramp = next(e for e in spec.commands["HEATER_ON"] if isinstance(e, RampEffect))
    assert ramp.target == 60.0 and ramp.tau == 30.0
    # HEATER_AUTO is the thermostat: regulate around the live setpoint.
    from xtce_sim.behavior import RegulateEffect

    reg = next(e for e in spec.commands["HEATER_AUTO"] if isinstance(e, RegulateEffect))
    assert reg.center == "@THM_HEATER{HeaterId}_SETPOINT" and reg.band == 2.0
    assert (reg.heats_to, reg.tau_heat, reg.cools_to, reg.tau_cool) == (60.0, 30.0, 20.0, 45.0)
    # Six boot signals (the fake solar/battery voltages became the power
    # model), and only continuous kinds: waves and holds.
    from xtce_sim.behavior import HoldEffect, OscillateEffect

    assert len(spec.signals) == 6
    assert {type(e) for e in spec.signals} == {OscillateEffect, HoldEffect}
    # Two physics models: the ADCS (41 fields, 11 commands) and the EPS
    # (4 analog fields, no commands — SET_POWER stays ordinary behavior).
    by_name = {cfg.name: cfg for cfg in spec.models}
    assert set(by_name) == {"adcs", "power"}
    assert len(by_name["adcs"].outputs) == 41
    assert len(by_name["adcs"].commands) == 11
    assert len(by_name["power"].outputs) == 4
    assert by_name["power"].commands == {}
    assert len(by_name["power"].loads) == 5


def test_sidecar_discovery(tmp_path):
    # A satellite is a directory: any .toml beside the XTCE means behavior
    # (my_vehicle's sidecar is adcs.toml, not named after the vehicle).
    assert sidecar_path([IMAGING]) == EXAMPLES / "imaging_sat"
    assert sidecar_path([DATA / "my_vehicle/my_vehicle.xml"]) == DATA / "my_vehicle"
    # A directory with no .toml beside the XTCE discovers nothing.
    import shutil

    bare = tmp_path / "my_vehicle.xml"
    shutil.copy(DATA / "my_vehicle/my_vehicle.xml", bare)
    assert sidecar_path([bare]) is None
    # ...and no sources at all discover nothing.
    assert sidecar_path([]) is None


# ---- effect parsing ---------------------------------------------------------


def test_effect_kinds_parse(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        """
        [IMAGER_ON]
        IMG_STATE = 1
        IMG_CAPTURE_COUNT = { increment = 1, emit = "immediate" }
        IMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0, tau = 20 }
        [SET_EXPOSURE]
        IMG_EXPOSURE_MS = "@arg:ExposureMs"
        """,
    )
    kinds = {type(e) for e in spec.commands["IMAGER_ON"]}
    assert kinds == {SetEffect, IncrementEffect, RampEffect}
    inc = next(e for e in spec.commands["IMAGER_ON"] if isinstance(e, IncrementEffect))
    assert inc.emit == "immediate"
    assert isinstance(spec.commands["SET_EXPOSURE"][0], CopyArgEffect)


def test_enum_label_set_is_valid(tmp_path, simdef):
    spec = _load(tmp_path, simdef, '[SET_MODE]\nHK_SYSTEM_MODE = "IMAGING"\n')
    assert spec.commands["SET_MODE"][0].value == "IMAGING"


# ---- validation errors ------------------------------------------------------


def test_unknown_command_table(tmp_path, simdef):
    assert "unknown command" in _errors(tmp_path, simdef, "[NO_SUCH_CMD]\nIMG_STATE = 1\n")


def test_unknown_field(tmp_path, simdef):
    assert "unknown telemetry field" in _errors(tmp_path, simdef, "[IMAGER_ON]\nNOT_A_FIELD = 1\n")


def test_unknown_arg_reference(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = "@arg:Nope"\n')
    assert "no argument 'Nope'" in msg


def test_template_arg_must_exist(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, '[IMAGER_ON]\n"IMG_{Unit}_STATE" = 1\n')
    assert "template argument {Unit}" in msg


def test_template_expansion_catches_missing_field(tmp_path, simdef):
    # HeaterId expands to 1..2; THM_HEATER1_BOGUS / THM_HEATER2_BOGUS don't exist.
    msg = _errors(tmp_path, simdef, '[HEATER_ON]\n"THM_HEATER{HeaterId}_BOGUS" = 1\n')
    assert "THM_HEATER1_BOGUS" in msg and "THM_HEATER2_BOGUS" in msg


def test_template_expansion_uses_enum_labels(tmp_path, simdef):
    # An enumerated argument expands to its labels. RESET's SubsystemId
    # enumerates all six subsystems but only four have a PWR_*_STATE field,
    # so load-time expansion names exactly the two that don't exist.
    msg = _errors(tmp_path, simdef, '[RESET]\n"PWR_{SubsystemId}_STATE" = "OFF"\n')
    assert "PWR_EPS_STATE" in msg and "PWR_THERMAL_STATE" in msg
    assert "PWR_COMMS_STATE" not in msg  # the real fields validate fine


def test_bad_enum_label(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, '[SET_MODE]\nHK_SYSTEM_MODE = "WARP_SPEED"\n')
    assert "not a label" in msg and "SAFE" in msg


def test_string_value_for_numeric_field(tmp_path, simdef):
    assert "string value for numeric field" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_EXPOSURE_MS = "fast"\n'
    )


def test_ramp_requires_tau_and_numeric_target(tmp_path, simdef):
    assert "requires tau" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0 }\n"
    )
    assert "tau must be a positive number" in _errors(
        tmp_path,
        simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0, tau = -1 }\n",
    )
    # TOML permits inf; the same finiteness check refuses it.
    assert "tau must be a positive number" in _errors(
        tmp_path,
        simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0, tau = inf }\n",
    )
    assert "@FIELD reference" in _errors(
        tmp_path,
        simdef,
        '[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = "warm", tau = 5 }\n',
    )


def test_unknown_verb_key(tmp_path, simdef):
    msg = _errors(
        tmp_path,
        simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_too = 35.0, tau = 5 }\n",
    )
    assert "unknown key(s) ['ramp_too']" in msg


def test_bad_emit_value(tmp_path, simdef):
    assert "emit must be one of" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = { set = 1, emit = "now" }\n'
    )


def test_initial_unknown_field_and_bad_label(tmp_path, simdef):
    msg = _errors(
        tmp_path,
        simdef,
        '[_initial]\nNOT_A_FIELD = 1\nHK_SYSTEM_MODE = "WARP_SPEED"\n',
    )
    assert "unknown telemetry field" in msg and "not a label" in msg


def test_all_errors_collected_in_one_raise(tmp_path, simdef):
    msg = _errors(
        tmp_path,
        simdef,
        "[NO_SUCH_CMD]\nX = 1\n[IMAGER_ON]\nNOT_A_FIELD = 1\nIMG_STATE = { bogus = 1 }\n",
    )
    assert "3 problem(s)" in msg


def test_invalid_toml_is_behavior_error(tmp_path, simdef):
    p = tmp_path / "bad.behavior.toml"
    p.write_text("[NOT CLOSED\n")
    with pytest.raises(BehaviorError, match="not valid TOML"):
        load_behavior(p, simdef)


# ---- review-driven cases ----------------------------------------------------


def test_templated_field_values_are_still_validated(tmp_path, simdef):
    # Value checks must expand templates just like existence checks do —
    # a bad label on a templated field is not allowed to slip through, and
    # EVERY expansion is checked (both heaters here).
    msg = _errors(tmp_path, simdef, '[HEATER_ON]\n"THM_HEATER{HeaterId}_STATE" = "BANANA"\n')
    assert "not a label of THM_HEATER1_STATE" in msg
    assert "not a label of THM_HEATER2_STATE" in msg


def test_table_form_set_accepts_arg_copy_with_emit(tmp_path, simdef):
    # '@arg:' means copy in the table form too — that's how a copy gets
    # emit="immediate" — and it must not parse as a literal string set.
    spec = _load(
        tmp_path,
        simdef,
        '[SET_EXPOSURE]\nIMG_EXPOSURE_MS = { set = "@arg:ExposureMs", emit = "immediate" }\n',
    )
    eff = spec.commands["SET_EXPOSURE"][0]
    assert isinstance(eff, CopyArgEffect) and eff.emit == "immediate"


def test_stray_tau_rejected_outside_ramp(tmp_path, simdef):
    assert "['tau'] not valid with set" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_STATE = { set = 1, tau = 5 }\n"
    )


def test_boolean_values_rejected(tmp_path, simdef):
    assert "boolean values are ambiguous" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_STATE = true\n"
    )


def test_raw_int_for_enum_field_must_be_a_real_value(tmp_path, simdef):
    # 3 is IMAGING — fine as a raw escape; 99 maps to nothing and is a typo.
    spec = _load(tmp_path, simdef, "[SET_MODE]\nHK_SYSTEM_MODE = 3\n")
    assert spec.commands["SET_MODE"][0].value == 3
    assert "not a raw value" in _errors(tmp_path, simdef, "[SET_MODE]\nHK_SYSTEM_MODE = 99\n")


def test_bare_at_field_set_value_gets_a_hint(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = "@IMG_EXPOSURE_MS"\n')
    assert 'did you mean "@arg:' in msg


def test_initial_rejects_templates_with_hint(tmp_path, simdef):
    assert "templates are not allowed here" in _errors(
        tmp_path, simdef, '[_initial]\n"THM_HEATER{HeaterId}_TEMP" = 20.0\n'
    )


# ---- inspect wiring ---------------------------------------------------------


def test_inspect_narrates_behavior_sidecar():
    result = CliRunner().invoke(main, ["inspect", str(IMAGING)])
    assert result.exit_code == 0, result.output
    assert "Behavior (" in result.output
    assert "THM_HEATER{HeaterId}_TEMP ramps to" in result.output
    assert "IMG_EXPOSURE_MS = @arg:ExposureMs" in result.output


def test_inspect_behavior_validation_failure_is_fatal(tmp_path):
    bad = tmp_path / "bad.behavior.toml"
    bad.write_text("[NO_SUCH_CMD]\nX = 1\n")
    result = CliRunner().invoke(main, ["inspect", str(IMAGING), "--behavior", str(bad)])
    assert result.exit_code != 0
    assert "unknown command" in result.output


def test_inspect_without_sidecar_unchanged(tmp_path):
    import shutil

    bare = tmp_path / "my_vehicle.xml"
    shutil.copy(DATA / "my_vehicle/my_vehicle.xml", bare)
    result = CliRunner().invoke(main, ["inspect", str(bare)])
    assert result.exit_code == 0, result.output
    assert "Behavior (" not in result.output


def test_inspect_my_vehicle_narrates_its_model():
    result = CliRunner().invoke(main, ["inspect", str(DATA / "my_vehicle/my_vehicle.xml")])
    assert result.exit_code == 0, result.output
    assert "model adcs: rigid-body ADCS (3 wheels" in result.output


def test_describe_lines(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    lines = behavior.describe(spec)
    assert any(line.startswith("HEATER_ON:") for line in lines)
    assert any("tau=30.0s" in line for line in lines)


# ---- runtime engine ---------------------------------------------------------


@pytest.fixture()
def engine(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    return behavior.BehaviorEngine(spec, simdef)


@pytest.fixture()
async def imaging_server(simdef):
    """A started, engine-backed imaging_sat server on an ephemeral port.

    One scaffold for every wire test (fast 0.05s beacon); teardown stops
    the server so no test leaks it on failure.
    """
    from xtce_sim.server import SimServer

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    server = SimServer(
        simdef,
        host="127.0.0.1",
        port=0,
        beacon_interval=0.05,
        behavior_engine=behavior.BehaviorEngine(spec, simdef),
    )
    await server.start()
    yield server
    await server.stop()


@pytest.fixture()
def simdef_quadratic(tmp_path_factory):
    """imaging_sat with IMG temperature calibration made quadratic (x^2) —
    a legal XTCE calibrator with no unique inverse."""
    xml = (
        (EXAMPLES / "imaging_sat/imaging_sat.xml")
        .read_text()
        .replace(
            '<xtce:Term coefficient="0.01" exponent="1" />',
            '<xtce:Term coefficient="0.01" exponent="2" />',
        )
    )
    path = tmp_path_factory.mktemp("quad") / "quad.xml"
    path.write_text(xml)
    return SimDefinition.from_xtce(path)


def _errors_for(tmp_path, sd, text: str) -> str:
    path = tmp_path / "t.behavior.toml"
    path.write_text(text)
    with pytest.raises(behavior.BehaviorError) as exc:
        load_behavior(path, sd)
    return str(exc.value)


def _eu(engine, fname):
    """A stored value in engineering units (states hold wire counts)."""
    return engine._engineering(fname, engine.state[fname])


def _cmd(simdef, name):
    return simdef.command_by_name(name)


def test_engine_seeds_initial_values(engine, simdef):
    thermal = simdef.packet_by_name("THERMAL_STATUS")
    values = engine.values_for(thermal)
    # values_for is the WIRE view: 0.01 degC/count, so 20.0 degC = 2000 counts.
    assert values["THM_HEATER1_TEMP"] == 2000
    assert values["THM_HEATER1_SETPOINT"] == 4000
    # values_for filters per packet: imager fields don't leak into thermal.
    assert "IMG_FOCAL_PLANE_TEMP" not in values
    # The bus boots with platform loads on and the payload off (power.toml
    # [_initial]); held states, not synthetic wobble, from the first beacon.
    power = engine.values_for(simdef.packet_by_name("POWER_STATUS"))
    assert power["PWR_CDH_STATE"] == 1
    assert power["PWR_COMMS_STATE"] == 1
    assert power["PWR_ADCS_STATE"] == 1
    assert power["PWR_IMAGER_STATE"] == 0
    # ...and the comms card boots with its beacon enabled (comms.toml).
    comms = engine.values_for(simdef.packet_by_name("COMMS_STATUS"))
    assert comms["COMM_BEACON_STATE"] == 1  # ENABLE


def test_engine_set_resolves_enum_label(engine, simdef):
    applied = engine.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    assert engine.state["IMG_STATE"] == 1  # "IDLE" label -> raw 1
    assert any("IMG_STATE=1" in a for a in applied)
    assert any("ramping to 35.0" in a for a in applied)  # ramp registered, live


def test_engine_copy_arg_resolves_labels_to_raw(engine, simdef):
    # decode_command hands enum args over as labels; the overlay stores raw.
    engine.apply_command(_cmd(simdef, "SET_MODE"), {"Mode": "IMAGING"})
    assert engine.state["HK_SYSTEM_MODE"] == 3


def test_engine_template_isolates_instances(engine, simdef):
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 2})
    assert engine.state["THM_HEATER2_STATE"] == 1  # "ON" -> raw 1
    assert "THM_HEATER1_STATE" not in engine.state  # heater 1 untouched


def test_engine_template_substitutes_enum_label(engine, simdef):
    # SET_POWER's SubsystemId is enumerated: the label names the field, so
    # COMMS lands in PWR_COMMS_STATE — no numbering convention involved.
    applied = engine.apply_command(
        _cmd(simdef, "SET_POWER"), {"SubsystemId": "COMMS", "PowerState": "OFF"}
    )
    assert engine.state["PWR_COMMS_STATE"] == 0  # "OFF" -> raw 0
    assert applied == ["PWR_COMMS_STATE=0"]
    # power.toml marks SET_POWER emit = "immediate": POWER_STATUS goes out
    # the moment the command lands, not on the next beacon.
    assert simdef.packet_by_name("POWER_STATUS").apid in engine.pop_immediate_apids()


def test_engine_template_enum_raw_value_resolves_via_label(engine, simdef):
    # A raw wire value that has a declared label names the same field the
    # label would (4 = IMAGER on PowerLoadIdType).
    engine.apply_command(_cmd(simdef, "SET_POWER"), {"SubsystemId": 4, "PowerState": "ON"})
    assert engine.state["PWR_IMAGER_STATE"] == 1


def test_engine_template_unlabeled_raw_value_refused(engine, simdef, caplog):
    # EPS (raw 1) is not a switchable load — PowerLoadIdType declares no
    # label for it, so the effect refuses to resolve rather than invent a
    # field name, and the refusal is logged.
    with caplog.at_level(logging.WARNING, logger="xtce_sim.behavior"):
        applied = engine.apply_command(
            _cmd(simdef, "SET_POWER"), {"SubsystemId": 1, "PowerState": "ON"}
        )
    assert applied == []
    assert any("has no label" in r.getMessage() for r in caplog.records)


def test_engine_template_undeclared_label_string_refused(engine, simdef, caplog):
    # The sharp case: "HEATER" would resolve to PWR_HEATER_STATE — a real
    # field this command cannot legally address over the wire. A string that
    # is not a declared PowerLoadIdType label is refused at the label check,
    # never by luck of field existence.
    with caplog.at_level(logging.WARNING, logger="xtce_sim.behavior"):
        applied = engine.apply_command(
            _cmd(simdef, "SET_POWER"), {"SubsystemId": "HEATER", "PowerState": "ON"}
        )
    assert applied == []
    assert engine.state["PWR_HEATER_STATE"] == 0  # still the boot seed, not ON
    assert any("is not a declared label" in r.getMessage() for r in caplog.records)


def test_engine_template_integral_float_substitutes_like_the_integer(engine, simdef):
    # Load-time expansion writes integer text ('2'); a float 2.0 from a
    # direct caller must name the same field, not 'THM_HEATER2.0_STATE'.
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 2.0})
    assert engine.state["THM_HEATER2_STATE"] == 1


def test_heater_commands_drive_the_power_card_channel(engine, simdef):
    # One power channel for two heaters: it tracks the last heater command
    # (the honest approximation documented in thermal.toml).
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    assert engine.state["PWR_HEATER_STATE"] == 1
    engine.apply_command(_cmd(simdef, "HEATER_OFF"), {"HeaterId": 1})
    assert engine.state["PWR_HEATER_STATE"] == 0


def test_imager_commands_track_the_power_card(engine, simdef):
    # One switch, one story: IMAGER_ON/OFF keep PWR_IMAGER_STATE coherent
    # with IMG_STATE instead of leaving the power card telling a stale tale.
    engine.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    assert engine.state["PWR_IMAGER_STATE"] == 1
    engine.apply_command(_cmd(simdef, "IMAGER_OFF"), {})
    assert engine.state["PWR_IMAGER_STATE"] == 0


def test_engine_increment_accumulates(tmp_path, simdef):
    spec = _load(tmp_path, simdef, "[TAKE_IMAGE]\nIMG_CAPTURE_COUNT = { increment = 1 }\n")
    eng = behavior.BehaviorEngine(spec, simdef)
    cmd = _cmd(simdef, "TAKE_IMAGE")
    eng.apply_command(cmd, {"ImageCount": 1})
    eng.apply_command(cmd, {"ImageCount": 1})
    assert eng.state["IMG_CAPTURE_COUNT"] == 2


def test_engine_bad_copy_value_warns_and_skips(engine, simdef, caplog):
    # A string argument copied into a numeric field can't be packed — the
    # effect is skipped with a warning, never crashing the dispatch path.
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="xtce_sim.behavior"):
        engine.apply_command(_cmd(simdef, "SET_EXPOSURE"), {"ExposureMs": "fast", "GainLevel": 1})
    assert "IMG_EXPOSURE_MS" not in engine.state
    assert engine.state["IMG_GAIN"] == 1  # the valid effect still applied
    assert any("does not fit" in r.getMessage() for r in caplog.records)


def test_engine_clamps_int_to_wire_width(tmp_path, simdef):
    spec = _load(tmp_path, simdef, '[SET_EXPOSURE]\nIMG_GAIN = "@arg:GainLevel"\n')
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "SET_EXPOSURE"), {"GainLevel": 99999})
    field = next(f for p in simdef.packets for f in p.fields if f.name == "IMG_GAIN")
    assert eng.state["IMG_GAIN"] == (1 << field.size_bits) - 1  # clamped, no overflow


async def test_server_end_to_end_command_sets_and_ramps_telemetry(imaging_server, simdef):
    # The full loop, both effect families on one wire packet: command in ->
    # overlay mutated -> beacon carries the set, AND the beacon loop ticks
    # the ramp, so the temperature has strictly left its seed.
    import asyncio

    from xtce_sim import ccsds, client, codec

    server = imaging_server
    cmd = simdef.command_by_name("HEATER_ON")
    await asyncio.to_thread(
        client.send_command, "127.0.0.1", server.bound_port, cmd, {"HeaterId": "1"}
    )
    await asyncio.sleep(1.0)  # ~20 beacon ticks against tau=30

    thermal = simdef.packet_by_name("THERMAL_STATUS")

    def read_one():
        for pkt in client.stream_packets("127.0.0.1", server.bound_port, timeout=2.0):
            header = ccsds.CCSDSHeader.unpack(pkt[:6])
            if header.apid == thermal.apid:
                return codec.unpack_telemetry(thermal, pkt[6:])
        return None

    values = await asyncio.to_thread(read_one)
    assert values is not None
    assert values["THM_HEATER1_STATE"] == 1  # ON, set by the command
    # The wire carries counts (0.01 degC/count): seeded at 20 degC (2000)
    # and ramping toward the 40 degC setpoint. The shipped HEATER_ON ramp
    # has no noise, so STRICTLY above the seed proves the beacon loop
    # actually ticked the ramp — equality here would mean nothing moved.
    assert 2000 < values["THM_HEATER1_TEMP"] < 4000


# ---- ENABLE_BEACON: the beacon gate -----------------------------------------


def test_enable_beacon_mirror_emits_final_comms_status(engine, simdef):
    # The sidecar mirror runs before the server's gate flips, so DISABLE's
    # immediate COMMS_STATUS — carrying the new state — is the link's last
    # autonomous packet.
    applied = engine.apply_command(
        _cmd(simdef, "ENABLE_BEACON"), {"BeaconState": "DISABLE"}
    )
    assert applied == ["COMM_BEACON_STATE=0"]
    assert simdef.packet_by_name("COMMS_STATUS").apid in engine.pop_immediate_apids()


def test_beacon_gate_is_label_driven(simdef):
    # The gate resolves BeaconState through the command's OWN enumeration
    # (label_for), so raw wire values follow whatever the ICD declares —
    # no server-side assumption about which number means what.
    from xtce_sim.server import SimServer

    server = SimServer(simdef, port=0)
    cmd = simdef.command_by_name("ENABLE_BEACON")
    server._set_beacon_enabled(cmd, {"BeaconState": 0})  # raw DISABLE
    assert server.beacon_enabled is False
    server._set_beacon_enabled(cmd, {"BeaconState": "ENABLE"})  # decoded label
    assert server.beacon_enabled is True


def test_beacon_gate_ignores_malformed_state(simdef, caplog):
    # A beacon command without a usable BeaconState leaves the gate alone —
    # guessing about RF silence is worse than ignoring a malformed command.
    # True == 1 in Python, so a leaked boolean must also be refused.
    from xtce_sim.server import SimServer

    server = SimServer(simdef, port=0)
    cmd = simdef.command_by_name("ENABLE_BEACON")
    assert server.beacon_enabled
    with caplog.at_level(logging.WARNING, logger="xtce_sim"):
        server._set_beacon_enabled(cmd, {})
        server._set_beacon_enabled(cmd, {"BeaconState": "MAYBE"})
        server._set_beacon_enabled(cmd, {"BeaconState": False})
        server._set_beacon_enabled(cmd, {"BeaconState": 2})  # no label
    assert server.beacon_enabled
    assert sum("ignored" in r.getMessage() for r in caplog.records) == 4


async def test_enable_beacon_gates_the_periodic_beacon(imaging_server, simdef):
    # The full story on the wire: beacons flow, DISABLE silences them (echo
    # and immediate emissions excepted), ENABLE brings them back.
    import asyncio
    import socket
    import time

    from xtce_sim import client

    server = imaging_server
    cmd = simdef.command_by_name("ENABLE_BEACON")

    def saw_packet(window: float) -> bool:
        # One packet is proof, so flowing phases return in ~one beacon
        # interval; the timeout is the failure bound and, for the quiet
        # phase, the whole point. A fresh connection receives only what
        # is enqueued after it connects, so no stale packets leak in.
        try:
            for _ in client.stream_packets(
                "127.0.0.1", server.bound_port, timeout=window
            ):
                return True
        except (TimeoutError, socket.timeout):
            pass
        return False

    async def command_beacon(state: str, want: bool) -> None:
        # Poll the gate instead of a fixed sleep: fast normally, and a
        # generous cap instead of a flake under CI load.
        await asyncio.to_thread(
            client.send_command,
            "127.0.0.1",
            server.bound_port,
            cmd,
            {"BeaconState": state},
        )
        deadline = time.monotonic() + 2.0
        while server.beacon_enabled is not want and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert server.beacon_enabled is want

    assert await asyncio.to_thread(saw_packet, 2.0)  # boot: beacons flow
    await command_beacon("DISABLE", want=False)
    assert not await asyncio.to_thread(saw_packet, 0.4)  # autonomous silence
    await command_beacon("ENABLE", want=True)
    assert await asyncio.to_thread(saw_packet, 2.0)  # beacons resumed


async def test_get_status_snapshots_a_quiet_vehicle(imaging_server, simdef, tmp_path):
    # GET_STATUS is the operator's downlink path to a beacon-disabled
    # vehicle: one commanded pass over every PERIODIC packet, on demand,
    # deliberately independent of the beacon gate. With a file service
    # wired (as the real `run` always has), the event-only FILE_RECEIPT
    # must NOT ride the snapshot — re-broadcasting a stale transfer
    # verdict on every poll is exactly what event-only forbids.
    import asyncio
    import contextlib
    import socket
    import time

    from xtce_sim import ccsds, client, fileservice

    server = imaging_server
    store = fileservice.FileStore(tmp_path / "files")
    server.file_service = fileservice.FileService(store, simdef)
    server.beacon_enabled = False  # commanded quiet (the gate is tested above)

    event_only = fileservice.event_only_apids(simdef)
    want = {p.apid for p in simdef.packets} - event_only
    seen: set[int] = set()

    def collect() -> None:
        # Reader window (5s) is deliberately larger than the registration
        # poll (2s) plus the send, so its socket timeout cannot expire
        # before the snapshot is even commanded.
        with contextlib.suppress(TimeoutError, socket.timeout):
            for pkt in client.stream_packets(
                "127.0.0.1", server.bound_port, timeout=5.0
            ):
                seen.add(ccsds.CCSDSHeader.unpack(pkt[:6]).apid)
                if want <= seen:
                    return

    reader = asyncio.create_task(asyncio.to_thread(collect))
    try:
        # The snapshot broadcasts to clients connected at emission time, so
        # wait for the reader's connection to register before commanding.
        deadline = time.monotonic() + 2.0
        while not server._clients and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert server._clients

        await asyncio.to_thread(
            client.send_command,
            "127.0.0.1",
            server.bound_port,
            simdef.command_by_name("GET_STATUS"),
            {},
        )
    finally:
        # Always collect the reader — an assertion above must not leak the
        # thread (bounded by its socket timeout) or its exception.
        with contextlib.suppress(Exception):
            await reader

    assert want <= seen  # every periodic packet arrived, beacon still off
    assert not (seen & event_only)  # and no event-only packet rode along
    assert server.beacon_enabled is False


async def test_beacon_paces_each_packet_on_its_declared_period(imaging_server, simdef):
    # ADCS_ATTITUDE declares 0.5 s, POWER_STATUS 2 s: over one window the
    # fast packet must arrive strictly more often than the slow one.
    # Generous margins — the pin is the ORDERING, not exact counts.
    import asyncio
    import socket
    import time

    from xtce_sim import ccsds, client

    server = imaging_server
    fast = simdef.packet_by_name("ADCS_ATTITUDE").apid
    slow = simdef.packet_by_name("POWER_STATUS").apid
    counts = {fast: 0, slow: 0}

    def collect(window: float) -> None:
        deadline = time.monotonic() + window
        try:
            for pkt in client.stream_packets(
                "127.0.0.1", server.bound_port, timeout=window
            ):
                apid = ccsds.CCSDSHeader.unpack(pkt[:6]).apid
                if apid in counts:
                    counts[apid] += 1
                if time.monotonic() >= deadline:
                    return
        except (TimeoutError, socket.timeout):
            pass

    await asyncio.to_thread(collect, 2.6)
    assert counts[fast] >= 3  # ~5 expected at 0.5 s
    assert counts[slow] <= 2  # ~1-2 expected at 2 s
    assert counts[fast] > counts[slow]


async def test_set_tlm_rate_retimes_one_packet(imaging_server, simdef):
    # The in-flight modifier of the declared periods: label names the
    # packet, PeriodMs is a duration, everything else keeps its schedule.
    server = imaging_server
    cmd = simdef.command_by_name("SET_TLM_RATE")
    attitude = simdef.packet_by_name("ADCS_ATTITUDE").apid
    power = simdef.packet_by_name("POWER_STATUS").apid

    await server._apply_command(cmd, {"Packet": "ADCS_ATTITUDE", "PeriodMs": 2000})
    assert server._tlm_periods[attitude] == 2.0
    assert server._tlm_periods[power] == 2.0  # untouched (its declared period)

    # A raw enum value (the APID) resolves like its label would.
    await server._apply_command(cmd, {"Packet": power, "PeriodMs": 250})
    assert server._tlm_periods[power] == 0.25


async def test_set_tlm_rate_refuses_malformed_arguments(imaging_server, simdef, caplog):
    # Missing args, undeclared labels, and leaked booleans all leave every
    # schedule alone — same refusal posture as the beacon gate.
    server = imaging_server
    cmd = simdef.command_by_name("SET_TLM_RATE")
    before = dict(server._tlm_periods)
    with caplog.at_level(logging.WARNING, logger="xtce_sim"):
        await server._apply_command(cmd, {})
        await server._apply_command(cmd, {"Packet": "EVENT_LOG", "PeriodMs": 1000})
        await server._apply_command(cmd, {"Packet": "POWER_STATUS", "PeriodMs": True})
        await server._apply_command(cmd, {"Packet": True, "PeriodMs": 1000})
        # Below the flood floor: legal only for a vehicle whose ICD forgot a
        # ValidRange, refused by the server regardless.
        await server._apply_command(cmd, {"Packet": "POWER_STATUS", "PeriodMs": 10})
    assert server._tlm_periods == before
    assert sum("ignored" in r.getMessage() for r in caplog.records) == 5


async def test_set_tlm_rate_refuses_event_only_packets(
    imaging_server, simdef, tmp_path, caplog
):
    # On a vehicle whose Packet enum lists an event-only packet, accepting
    # would log success for a packet the scheduler never sends — refuse.
    from xtce_sim import fileservice
    from xtce_sim.definition import CommandDef, ParamInfo

    server = imaging_server
    store = fileservice.FileStore(tmp_path / "files")
    server.file_service = fileservice.FileService(store, simdef)
    receipt = simdef.packet_by_name("FILE_RECEIPT")
    cmd = CommandDef(
        name="SET_TLM_RATE",
        opcode=0x11,
        params=[
            ParamInfo("Packet", 8, "uint8", enumerations={"FILE_RECEIPT": receipt.apid}),
            ParamInfo("PeriodMs", 16, "uint16"),
        ],
    )
    before = dict(server._tlm_periods)
    with caplog.at_level(logging.WARNING, logger="xtce_sim"):
        server._set_tlm_period(cmd, {"Packet": "FILE_RECEIPT", "PeriodMs": 1000})
    assert server._tlm_periods == before
    assert any("event-only" in r.getMessage() for r in caplog.records)


async def test_commands_only_definition_serves_cleanly():
    # A commands-only ICD (uplink half of a split pair) has nothing to
    # schedule; the beacon loop must idle as a physics heartbeat instead of
    # dying on an empty schedule (regression: min() of an empty dict).
    import asyncio

    from xtce_sim.server import SimServer

    d = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle_commands.xml")
    assert d.packets == []
    server = SimServer(d, host="127.0.0.1", port=0, beacon_interval=0.05)
    await server.start()
    await asyncio.sleep(0.15)  # a few loop passes
    await server.stop()  # used to re-raise ValueError from the dead task


def test_undeclared_rates_fall_back_to_the_global_interval(simdef):
    # my_vehicle declares no DefaultRateInStream anywhere: every packet
    # paces on --interval, exactly the pre-feature behavior.
    from xtce_sim.server import SimServer

    my_vehicle = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    server = SimServer(my_vehicle, port=0, beacon_interval=0.7)
    assert set(server._tlm_periods.values()) == {0.7}


async def test_get_status_leaves_the_beacon_gate_alone(imaging_server, simdef):
    # 'Independent of the gate' cuts both ways: a snapshot must neither
    # need the beacon nor touch its state, from either side of the switch.
    server = imaging_server
    cmd = simdef.command_by_name("GET_STATUS")
    await server._apply_command(cmd, {})
    assert server.beacon_enabled is True
    server.beacon_enabled = False
    await server._apply_command(cmd, {})
    assert server.beacon_enabled is False


# ---- review-driven runtime cases --------------------------------------------


def test_nonfinite_values_rejected_at_load(tmp_path, simdef):
    # TOML permits nan/inf literals; they must never reach the engine.
    assert "must be finite" in _errors(tmp_path, simdef, "[_initial]\nIMG_GAIN = nan\n")
    assert "must be finite" in _errors(tmp_path, simdef, "[IMAGER_ON]\nIMG_GAIN = inf\n")


def test_nonfinite_copied_argument_skipped_at_runtime(tmp_path, simdef):
    # A float argument can decode to nan off the wire — skip, never crash.
    spec = _load(
        tmp_path, simdef, '[SET_HEATER_SETPOINT]\nTHM_HEATER1_SETPOINT = "@arg:Setpoint"\n'
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(
        _cmd(simdef, "SET_HEATER_SETPOINT"), {"Setpoint": float("nan"), "HeaterId": 1}
    )
    assert "THM_HEATER1_SETPOINT" not in eng.state


def test_copy_of_enum_arg_stores_raw_value(tmp_path, simdef):
    # An enum argument decodes as its label; copying it into a field with
    # DIFFERENT (or no) labels must store the raw value, same as templates.
    spec = _load(tmp_path, simdef, '[SET_MODE]\nIMG_GAIN = "@arg:Mode"\n')
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "SET_MODE"), {"Mode": "IMAGING"})
    assert eng.state["IMG_GAIN"] == 3  # raw value of IMAGING, not the label


def test_overlay_wins_over_telemetry_source(engine, simdef):
    # The behavior overlay must beat the synthetic layer at pack time.
    from xtce_sim.server import SimServer

    server = SimServer(
        simdef,
        host="127.0.0.1",
        port=1,  # never started; just merging
        behavior_engine=engine,
        telemetry_source=lambda pkt: {f.name: 7 for f in pkt.fields},
    )
    thermal = simdef.packet_by_name("THERMAL_STATUS")
    values = server._packet_values(thermal)
    assert values["THM_HEATER1_TEMP"] == 2000  # overlay wins (20 degC in counts)
    assert values["THM_PANEL_TEMP_PX"] == 7 if "THM_PANEL_TEMP_PX" in values else True
    # a field the overlay doesn't hold comes from the source:
    non_overlay = [f.name for f in thermal.fields if f.name not in engine.state]
    assert values[non_overlay[0]] == 7


def test_ramp_registration_moves_nothing_until_tick(engine, simdef):
    before = dict(engine.state)
    applied = engine.apply_command(_cmd(simdef, "HEATER_OFF"), {"HeaterId": 1})
    # the set applied; the ramp is registered but only tick() moves values
    assert engine.state["THM_HEATER1_STATE"] == 0
    assert engine.state["THM_HEATER1_TEMP"] == before["THM_HEATER1_TEMP"]
    assert any("ramping to 20.0" in a for a in applied)


def test_increment_saturates_at_wire_max(tmp_path, simdef):
    # IMG_GAIN is uint8: 200 + 200 must saturate at 255, not wrap.
    spec = _load(tmp_path, simdef, "[SET_EXPOSURE]\nIMG_GAIN = { increment = 200 }\n")
    eng = behavior.BehaviorEngine(spec, simdef)
    cmd = _cmd(simdef, "SET_EXPOSURE")
    eng.apply_command(cmd, {"ExposureMs": 1, "GainLevel": 1})
    eng.apply_command(cmd, {"ExposureMs": 1, "GainLevel": 1})
    assert eng.state["IMG_GAIN"] == 255  # saturated, not wrapped


# ---- coverage batch: the defensive branches, each pinned --------------------


def test_describe_increment_line(tmp_path, simdef):
    spec = _load(tmp_path, simdef, "[TAKE_IMAGE]\nIMG_CAPTURE_COUNT = { increment = 2 }\n")
    assert any("IMG_CAPTURE_COUNT += 2" in line for line in behavior.describe(spec))


def test_non_table_bodies_rejected(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, "_initial = 5\nIMAGER_ON = 7\n")
    assert "[_initial]: must be a table" in msg
    assert "[IMAGER_ON]: must be a table" in msg


def test_verb_count_must_be_exactly_one(tmp_path, simdef):
    assert "exactly one of set/increment/ramp_to" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = { emit = "interval" }\n'
    )
    assert "exactly one of set/increment/ramp_to" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_STATE = { set = 1, increment = 1 }\n"
    )


def test_increment_amount_must_be_number(tmp_path, simdef):
    assert "increment must be a finite number" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = { increment = "lots" }\n'
    )
    assert "increment must be a finite number" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_GAIN = { increment = inf }\n"
    )


def test_ramp_target_bool_rejected(tmp_path, simdef):
    assert "must be a finite number or @FIELD" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = true, tau = 5 }\n"
    )
    # inf is a legal TOML float; the same target check refuses it at load.
    assert "must be a finite number or @FIELD" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = inf, tau = 5 }\n"
    )


def test_ramp_on_string_field_rejected(tmp_path, simdef):
    # both the ramped field and an @target must be numeric
    assert "not a numeric field" in _errors(
        tmp_path, simdef, "[FILE_DELETE]\nFR_FILENAME = { ramp_to = 1, tau = 5 }\n"
    )
    assert "not a numeric field" in _errors(
        tmp_path,
        simdef,
        '[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = "@FR_FILENAME", tau = 5 }\n',
    )


def test_nested_table_set_value_rejected(tmp_path, simdef):
    assert "unexpected table value" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_STATE = { set = { deep = 1 } }\n"
    )


def test_numeric_value_for_string_field_rejected(tmp_path, simdef):
    assert "numeric value for string field" in _errors(
        tmp_path, simdef, "[FILE_DELETE]\nFR_FILENAME = 5\n"
    )


def test_unbounded_template_defers_to_runtime(tmp_path, simdef):
    # ExposureMs spans 1..10000 (> the expansion cap), so the field-existence
    # check defers — the file loads even though the name can't be verified.
    spec = _load(tmp_path, simdef, '[SET_EXPOSURE]\n"IMG_{ExposureMs}_X" = 1\n')
    assert spec.commands["SET_EXPOSURE"]
    # ...and at runtime an unresolvable expansion warns and skips:
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "SET_EXPOSURE"), {"ExposureMs": 7, "GainLevel": 1})
    assert "IMG_7_X" not in eng.state


def test_enum_arg_template_expands_labels(tmp_path, simdef):
    # Mode is enumerated: the template expands over its labels (the same
    # text runtime substitution uses), and the nonexistent expansions are
    # all named in the error.
    msg = _errors(tmp_path, simdef, '[SET_MODE]\n"X_{Mode}_Y" = 1\n')
    assert "X_SAFE_Y" in msg and "X_DOWNLINK_Y" in msg


def test_engine_skips_when_template_arg_missing(engine, simdef, caplog):
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="xtce_sim.behavior"):
        engine.apply_command(_cmd(simdef, "HEATER_ON"), {})  # no HeaterId
    assert any("template argument" in r.getMessage() for r in caplog.records)


def test_engine_skips_when_copy_arg_missing(tmp_path, simdef, caplog):
    import logging as _logging

    spec = _load(tmp_path, simdef, '[SET_MODE]\nHK_SYSTEM_MODE = "@arg:Mode"\n')
    eng = behavior.BehaviorEngine(spec, simdef)
    with caplog.at_level(_logging.WARNING, logger="xtce_sim.behavior"):
        eng.apply_command(_cmd(simdef, "SET_MODE"), {})
    assert "HK_SYSTEM_MODE" not in eng.state
    assert any("missing from decode" in r.getMessage() for r in caplog.records)


def test_engine_string_set_encodes_for_string_field(tmp_path, simdef):
    spec = _load(tmp_path, simdef, '[FILE_DELETE]\nFR_FILENAME = "gone.bin"\n')
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "FILE_DELETE"), {"Filename": b"x"})
    assert eng.state["FR_FILENAME"] == b"gone.bin"


def test_engine_copies_bytes_arg_into_string_field_only(tmp_path, simdef):
    # decode hands string args over as bytes: fine into a string field,
    # skipped (not crashed) into a numeric one.
    spec = _load(
        tmp_path,
        simdef,
        '[FILE_DELETE]\nFR_FILENAME = "@arg:Filename"\nFR_FILE_SIZE = "@arg:Filename"\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "FILE_DELETE"), {"Filename": b"a.bin"})
    assert eng.state["FR_FILENAME"] == b"a.bin"
    assert "FR_FILE_SIZE" not in eng.state


def test_engine_bool_value_skipped_at_runtime(tmp_path, simdef):
    spec = _load(tmp_path, simdef, '[SET_EXPOSURE]\nIMG_GAIN = "@arg:GainLevel"\n')
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "SET_EXPOSURE"), {"GainLevel": True})
    assert "IMG_GAIN" not in eng.state


def test_engine_float_field_stores_float(tmp_path, simdef):
    spec = _load(tmp_path, simdef, "[_initial]\nHK_ISSUED_TIMESTAMP = 1735689600.5\n")
    eng = behavior.BehaviorEngine(spec, simdef)
    assert eng.state["HK_ISSUED_TIMESTAMP"] == 1735689600.5  # float64, not rounded


# ---- ramp tick engine (unit 4a) ---------------------------------------------


def test_ramp_advances_toward_target_and_completes(engine, simdef):
    import math

    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    # manual override: tau=30 open-loop toward the element's 60.0 capability
    # from 20.0; one 30s tick covers 1-1/e.
    engine.tick(30.0)
    expected = 20.0 + (60.0 - 20.0) * (1.0 - math.exp(-1.0))
    # counts quantize at 0.01 degC, so the EU view is exact to a centidegree
    assert abs(_eu(engine, "THM_HEATER1_TEMP") - expected) < 0.01
    # after many time constants the ramp lands exactly and retires
    for _ in range(20):
        engine.tick(30.0)
    assert _eu(engine, "THM_HEATER1_TEMP") == 60
    assert "THM_HEATER1_TEMP" not in engine._behaviors


def test_ramp_trajectory_is_tick_size_independent(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nHK_ISSUED_TIMESTAMP = 0.0\n"
        "[IMAGER_ON]\nHK_ISSUED_TIMESTAMP = { ramp_to = 100.0, tau = 10 }\n",
    )
    coarse = behavior.BehaviorEngine(spec, simdef)
    fine = behavior.BehaviorEngine(spec, simdef)
    cmd = _cmd(simdef, "IMAGER_ON")
    coarse.apply_command(cmd, {})
    fine.apply_command(cmd, {})
    coarse.tick(10.0)  # one 10s step
    for _ in range(100):
        fine.tick(0.1)  # a hundred 0.1s steps
    assert abs(coarse.state["HK_ISSUED_TIMESTAMP"] - fine.state["HK_ISSUED_TIMESTAMP"]) < 1e-6


def test_ramp_integer_field_does_not_stall_on_small_steps(engine, simdef):
    # int16 field, tiny dt/tau steps: the float trajectory must keep moving
    # even while the stored (rounded) value holds still.
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    for _ in range(4000):  # 0.1s steps against tau=30
        engine.tick(0.1)
    assert _eu(engine, "THM_HEATER1_TEMP") == 60  # reached, not stalled at 20


def test_ramp_target_reread_live_each_tick(tmp_path, simdef):
    # HEATER_ON no longer references the setpoint (it is the open-loop manual
    # override), so pin ramp's live @FIELD re-read with an explicit sidecar.
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nTHM_HEATER1_SETPOINT = 40.0\nHK_ISSUED_TIMESTAMP = 20.0\n"
        '[IMAGER_ON]\nHK_ISSUED_TIMESTAMP = { ramp_to = "@THM_HEATER1_SETPOINT", tau = 30 }\n'
        '[SET_HEATER_SETPOINT]\n"THM_HEATER{HeaterId}_SETPOINT" = "@arg:Setpoint"\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    eng.tick(30.0)
    part_way = eng.state["HK_ISSUED_TIMESTAMP"]
    # raise the setpoint mid-ramp: the curve bends toward the new target
    eng.apply_command(_cmd(simdef, "SET_HEATER_SETPOINT"), {"HeaterId": 1, "Setpoint": 55})
    for _ in range(20):
        eng.tick(30.0)
    assert part_way < 40 < eng.state["HK_ISSUED_TIMESTAMP"] == 55


def test_new_ramp_replaces_old_per_field(engine, simdef):
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    for _ in range(10):
        engine.tick(30.0)  # warm up toward 60
    engine.apply_command(_cmd(simdef, "HEATER_OFF"), {"HeaterId": 1})  # cooling replaces
    for _ in range(30):
        engine.tick(30.0)
    assert _eu(engine, "THM_HEATER1_TEMP") == 20  # cooled back to ambient
    # heater 2 was never involved
    assert "THM_HEATER2_TEMP" not in engine._behaviors


# ---- regulate: the bang-bang thermostat loop --------------------------------


def test_regulate_validation(tmp_path, simdef):
    line = '"THM_HEATER1_TEMP" = {{ regulate = 40.0{rest} }}'

    def errs(rest: str) -> str:
        return _errors(tmp_path, simdef, "[HEATER_AUTO]\n" + line.format(rest=rest) + "\n")

    assert "regulate requires band, heats_to, tau_heat, cools_to, tau_cool" in errs("")
    full = ", heats_to = 60.0, tau_heat = 30.0, cools_to = 20.0, tau_cool = 45.0"
    assert "band must be a positive number" in errs(", band = 0" + full)
    assert "tau_heat must be a positive number of seconds" in errs(
        ", band = 2.0, heats_to = 60.0, tau_heat = -1, cools_to = 20.0, tau_cool = 45.0"
    )
    assert "heats_to string value must be an @FIELD reference" in errs(
        ', band = 2.0, heats_to = "hot", tau_heat = 30.0, cools_to = 20.0, tau_cool = 45.0'
    )
    assert "noise must be a non-negative number" in errs(", band = 2.0" + full + ", noise = -1")
    # templated side references are refused at load: the engine resolves
    # templates only for the center, so these could never work at runtime
    assert "cools_to must not use {templates}" in _errors(
        tmp_path,
        simdef,
        '[HEATER_AUTO]\n"THM_HEATER1_TEMP" = { regulate = 40.0, band = 2.0, '
        'heats_to = 60.0, tau_heat = 30.0, cools_to = "@THM_HEATER{HeaterId}_SETPOINT", '
        "tau_cool = 45.0 }\n",
    )


def test_regulate_sawtooths_inside_band_and_never_retires(engine, simdef):
    engine.apply_command(_cmd(simdef, "HEATER_AUTO"), {"HeaterId": 1})
    series = []
    for _ in range(3000):  # 1500 s at 0.5 s ticks
        engine.tick(0.5)
        series.append(_eu(engine, "THM_HEATER1_TEMP"))
    # After the initial climb from 20, the loop lives inside the hysteresis
    # band around the 40.0 setpoint (39..41 plus at most one integration step
    # of overshoot at either edge).
    first_peak = next(i for i, v in enumerate(series) if v >= 41.0)
    tail = series[first_peak:]
    assert min(tail) > 38.5 and max(tail) < 41.5
    # It sawtooths: the element cycles many times (direction reversals),
    # rather than settling to a flat line.
    reversals = sum(
        1 for a, b, c in zip(tail, tail[1:], tail[2:]) if (b - a) * (c - b) < 0
    )
    assert reversals > 20
    # And it NEVER retires — arrival is the start of its job, not the end.
    assert "THM_HEATER1_TEMP" in engine._behaviors


def test_regulate_follows_setpoint_change_after_settling(engine, simdef):
    # THE regression for the old asymmetry: a settled loop must still honor
    # a later SET_HEATER_SETPOINT (the retired ramp never did).
    engine.apply_command(_cmd(simdef, "HEATER_AUTO"), {"HeaterId": 1})
    for _ in range(1200):
        engine.tick(0.5)  # settle into the 39..41 band
    assert 38.5 < _eu(engine, "THM_HEATER1_TEMP") < 41.5
    engine.apply_command(_cmd(simdef, "SET_HEATER_SETPOINT"), {"HeaterId": 1, "Setpoint": 55})
    for _ in range(1200):
        engine.tick(0.5)
    # regulating around the new setpoint now (54..56 plus edge overshoot)
    assert 53.5 < _eu(engine, "THM_HEATER1_TEMP") < 56.5
    assert "THM_HEATER1_TEMP" in engine._behaviors


def test_regulate_underpowered_element_settles_at_capability(tmp_path, simdef):
    # heats_to below the band: the element can never reach the loop's bottom
    # edge, so it stays on and the value settles at what the element can do —
    # an underpowered heater behaves physically instead of erroring.
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nHK_ISSUED_TIMESTAMP = 0.0\n"
        "[IMAGER_ON]\nHK_ISSUED_TIMESTAMP = { regulate = 40.0, band = 2.0, "
        "heats_to = 30.0, tau_heat = 5.0, cools_to = 0.0, tau_cool = 5.0 }\n",
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    for _ in range(400):
        eng.tick(0.5)
    assert abs(eng.state["HK_ISSUED_TIMESTAMP"] - 30.0) < 0.1
    assert "HK_ISSUED_TIMESTAMP" in eng._behaviors


def test_regulate_describe_lines(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    assert any(
        "regulates around @THM_HEATER{HeaterId}_SETPOINT band 2.0" in line
        for line in behavior.describe(spec)
    )


def test_tick_ignores_nonpositive_dt(engine, simdef):
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    engine.tick(0.0)
    engine.tick(-5.0)
    assert _eu(engine, "THM_HEATER1_TEMP") == 20  # unmoved


def test_float_field_ramp_lands_exactly_and_retires(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nHK_ISSUED_TIMESTAMP = 0.0\n"
        "[IMAGER_ON]\nHK_ISSUED_TIMESTAMP = { ramp_to = 100.0, tau = 5 }\n",
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    for _ in range(40):  # 40 * 5s = 40 time constants
        eng.tick(5.0)
    assert eng.state["HK_ISSUED_TIMESTAMP"] == 100.0  # exact landing
    assert "HK_ISSUED_TIMESTAMP" not in eng._behaviors  # retired


def test_direct_write_cancels_active_ramp(engine, simdef):
    # Last command wins: an explicit set on a ramped field cancels the ramp,
    # so the next tick cannot silently revert the operator's value.
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    engine.tick(30.0)
    assert "THM_HEATER1_TEMP" in engine._behaviors
    # a direct copy onto the ramped field (via setpoint command redirected)...
    spec_over = engine.spec.commands.setdefault("NOOP", [])
    from xtce_sim.behavior import SetEffect

    spec_over.append(SetEffect(field="THM_HEATER1_TEMP", value=33))
    engine.apply_command(_cmd(simdef, "NOOP"), {})
    assert _eu(engine, "THM_HEATER1_TEMP") == 33
    assert "THM_HEATER1_TEMP" not in engine._behaviors  # ramp cancelled
    engine.tick(30.0)
    assert _eu(engine, "THM_HEATER1_TEMP") == 33  # value survives ticks


def test_missing_ramp_target_warns_once_not_per_tick(tmp_path, simdef, caplog):
    import logging as _logging

    spec = _load(
        tmp_path,
        simdef,
        '[HEATER_ON]\n"THM_HEATER{HeaterId}_TEMP" = { ramp_to = "@THM_HEATER{HeaterId}_SETPOINT", tau = 30.0 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)  # setpoint never seeded
    eng.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    with caplog.at_level(_logging.WARNING, logger="xtce_sim.behavior"):
        for _ in range(10):
            eng.tick(1.0)
    warnings = [r for r in caplog.records if "no numeric value yet" in r.getMessage()]
    assert len(warnings) == 1  # once per ramp, not once per beacon tick
    assert "THM_HEATER1_TEMP" not in eng.state  # held: nothing written yet
    # and the ramp recovers when the target appears (seed it directly —
    # this minimal spec has no SET_HEATER_SETPOINT table):
    eng.state["THM_HEATER1_SETPOINT"] = 40
    eng.tick(30.0)
    assert _eu(eng, "THM_HEATER1_TEMP") > 0  # moving now


# ---- oscillate / hold / noise / signals (unit 4b) ---------------------------


def test_oscillate_validation(tmp_path, simdef):
    assert "requires amplitude and period" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { oscillate = 10 }\n"
    )
    assert "period must be a positive number" in _errors(
        tmp_path,
        simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { oscillate = 10, amplitude = 5, period = 0 }\n",
    )
    assert "shape must be one of" in _errors(
        tmp_path,
        simdef,
        '[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { oscillate = 10, amplitude = 5, period = 60, shape = "square" }\n',
    )
    assert "noise must be a non-negative number" in _errors(
        tmp_path,
        simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { oscillate = 10, amplitude = 5, period = 60, noise = -1 }\n",
    )


def test_signals_validation(tmp_path, simdef):
    msg = _errors(
        tmp_path,
        simdef,
        "[_signals]\nIMG_STATE = 1\nNOT_A_FIELD = { hold = 1 }\n"
        '"THM_HEATER{HeaterId}_TEMP" = { hold = 1 }\n',
    )
    assert "signals must be continuous behaviors" in msg
    assert "unknown telemetry field" in msg
    assert "templates are not allowed here" in msg


def test_oscillate_wave_math_no_noise(tmp_path, simdef):
    # sine: quarter period -> center + amplitude, exactly (no noise)
    spec = _load(
        tmp_path,
        simdef,
        "[_signals]\nTHM_PANEL_PLUS_X = { oscillate = 10.0, amplitude = 20.0, period = 100 }\n",
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.tick(25.0)  # t = period/4
    assert _eu(eng, "THM_PANEL_PLUS_X") == 30  # 10 + 20*sin(pi/2)
    eng.tick(25.0)  # t = period/2
    assert _eu(eng, "THM_PANEL_PLUS_X") == 10  # back through center
    eng.tick(25.0)  # t = 3/4 period
    assert _eu(eng, "THM_PANEL_PLUS_X") == -10  # trough


def test_triangle_and_sawtooth_shapes():
    from xtce_sim.behavior.verbs.oscillate import _wave

    assert _wave("triangle", 0.25) == 1.0 and _wave("triangle", 0.75) == -1.0
    assert _wave("triangle", 0.0) == 0.0 and _wave("triangle", 0.5) == 0.0
    assert _wave("sawtooth", 0.25) == 0.5 and _wave("sawtooth", 0.75) == -0.5


def test_boot_signals_run_without_commands(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.tick(1350.0)  # quarter orbit
    assert _eu(eng, "THM_PANEL_PLUS_X") > 25  # near peak (35 ± noise)
    assert _eu(eng, "THM_RADIATOR") != 0  # holding around -5


def test_noisy_ramp_degrades_into_noisy_hold(tmp_path, simdef):
    from xtce_sim.behavior.verbs.hold import _ActiveHold

    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nHK_ISSUED_TIMESTAMP = 0.0\n"
        "[IMAGER_ON]\nHK_ISSUED_TIMESTAMP = { ramp_to = 100.0, tau = 2, noise = 0.5 }\n",
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    for _ in range(30):
        eng.tick(2.0)
    beh = eng._behaviors.get("HK_ISSUED_TIMESTAMP")
    assert isinstance(beh, _ActiveHold)  # settled but still breathing
    assert beh.noise == 0.5
    eng.tick(2.0)
    assert abs(eng.state["HK_ISSUED_TIMESTAMP"] - 100.0) < 3.0  # jitter near target


# ---- 4b defensive branches ---------------------------------------------------


def test_spec_signals_default_to_empty_list(tmp_path):
    spec = behavior.BehaviorSpec(path=tmp_path, initial={}, commands={})
    assert spec.signals == []


def test_signals_body_must_be_table(tmp_path, simdef):
    assert "must be a table" in _errors(tmp_path, simdef, "_signals = 5\n")


def test_signal_parse_error_reported_not_registered(tmp_path, simdef):
    # a bad verb body inside [_signals] errors without crashing the loader
    assert "not valid with hold" in _errors(
        tmp_path, simdef, "[_signals]\nTHM_RADIATOR = { hold = 1, tau = 2 }\n"
    )


def test_center_and_hold_value_validation(tmp_path, simdef):
    assert "must be an @FIELD reference" in _errors(
        tmp_path,
        simdef,
        '[_signals]\nTHM_RADIATOR = { oscillate = "FOO", amplitude = 1, period = 60 }\n',
    )
    assert "must be a finite number or @FIELD" in _errors(
        tmp_path,
        simdef,
        "[_signals]\nTHM_RADIATOR = { oscillate = true, amplitude = 1, period = 60 }\n",
    )
    assert "amplitude must be a non-negative number" in _errors(
        tmp_path,
        simdef,
        "[_signals]\nTHM_RADIATOR = { oscillate = 1, amplitude = -1, period = 60 }\n",
    )
    assert "phase must be a finite number" in _errors(
        tmp_path,
        simdef,
        "[_signals]\nTHM_RADIATOR = { oscillate = 1, amplitude = 1, period = 60, phase = nan }\n",
    )
    assert "noise must be a non-negative number" in _errors(
        tmp_path, simdef, "[_signals]\nTHM_RADIATOR = { hold = 1, noise = -1 }\n"
    )
    assert "noise must be a non-negative number" in _errors(
        tmp_path,
        simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 5, tau = 1, noise = -1 }\n",
    )


def test_oscillate_center_can_be_live_reference(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nTHM_HEATER1_SETPOINT = 20\n"
        '[_signals]\nTHM_RADIATOR = { oscillate = "@THM_HEATER1_SETPOINT", amplitude = 4, period = 8 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.tick(2.0)  # quarter period: 20 + 4
    assert _eu(eng, "THM_RADIATOR") == 24


def test_hold_with_unresolvable_template_ref_skips(simdef, caplog, tmp_path):
    from xtce_sim.behavior import HoldEffect

    spec = behavior.BehaviorSpec(path=tmp_path, initial={}, commands={})
    spec.commands["NOOP"] = [
        HoldEffect(field="THM_RADIATOR", value="@THM_HEATER{HeaterId}_SETPOINT")
    ]
    eng = behavior.BehaviorEngine(spec, simdef)
    with caplog.at_level("WARNING"):
        eng.apply_command(_cmd(simdef, "NOOP"), {})  # HeaterId not supplied
    assert "THM_RADIATOR" not in eng._behaviors
    assert "skipped" in caplog.text


def test_tick_skips_behavior_with_missing_live_ref(simdef, caplog, tmp_path):
    from xtce_sim.behavior.verbs.hold import _ActiveHold
    from xtce_sim.behavior.verbs.oscillate import _ActiveOsc

    spec = behavior.BehaviorSpec(path=tmp_path, initial={}, commands={})
    eng = behavior.BehaviorEngine(spec, simdef)
    eng._behaviors["THM_RADIATOR"] = _ActiveHold(field="THM_RADIATOR", value="@GHOST")
    eng._behaviors["THM_PANEL_PLUS_X"] = _ActiveOsc(
        field="THM_PANEL_PLUS_X",
        center="@GHOST",
        amplitude=1.0,
        period=10.0,
        shape="sine",
        phase=0.0,
    )
    with caplog.at_level("WARNING"):
        eng.tick(1.0)
        eng.tick(1.0)
    assert "THM_RADIATOR" not in eng.state  # skipped, not zeroed
    assert "THM_PANEL_PLUS_X" not in eng.state
    assert caplog.text.count("@GHOST") <= 2  # warn-once per behavior


def test_signal_with_templated_reference_is_load_error(tmp_path, simdef):
    # review defect: this used to crash the loader with AttributeError
    assert "templates are not allowed here" in _errors(
        tmp_path,
        simdef,
        '[_signals]\nTHM_RADIATOR = { hold = "@THM_HEATER{HeaterId}_SETPOINT" }\n',
    )


def test_noisy_ramp_settles_on_landed_value_not_live_target(tmp_path, simdef):
    # review defect: the degraded hold used to keep tracking @FIELD, so a
    # later setpoint change teleported the value instead of freezing it.
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nTHM_HEATER1_SETPOINT = 40\n"
        '[HEATER_ON]\nTHM_HEATER1_TEMP = { ramp_to = "@THM_HEATER1_SETPOINT", tau = 2, noise = 0.1 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    for _ in range(40):
        eng.tick(2.0)
    eng.state["THM_HEATER1_SETPOINT"] = 100  # operator moves the setpoint
    eng.tick(2.0)
    assert abs(_eu(eng, "THM_HEATER1_TEMP") - 40) < 3  # stayed put (± noise)


# ---- review fixes: self-reference, finiteness, non-numeric guard, ----------
# ---- oscillator clock, per-engine rng                            ----------


def test_self_reference_rejected_at_load(tmp_path, simdef):
    msg = _errors(
        tmp_path,
        simdef,
        '[_signals]\nTHM_RADIATOR = { hold = "@THM_RADIATOR", noise = 0.3 }\n'
        'PWR_BATTERY_TEMP = { oscillate = "@PWR_BATTERY_TEMP", amplitude = 1, period = 60 }\n'
        '[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = "@IMG_FOCAL_PLANE_TEMP", tau = 5 }\n',
    )
    assert msg.count("must not reference its own field") == 3


def test_templated_self_reference_skipped_at_runtime(tmp_path, simdef, caplog):
    # A concrete field with a templated reference is not equal at load
    # (identical templates ARE caught there) but can resolve to itself.
    spec = _load(
        tmp_path,
        simdef,
        '[HEATER_ON]\nTHM_HEATER1_TEMP = { hold = "@THM_HEATER{HeaterId}_TEMP", noise = 0.5 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    with caplog.at_level("WARNING"):
        eng.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    assert "names its own field" in caplog.text
    assert "THM_HEATER1_TEMP" not in eng._behaviors


def test_continuous_behavior_on_string_field_refused_once(simdef, caplog):
    from xtce_sim.behavior import HoldEffect

    spec = behavior.BehaviorSpec(path=None, initial={}, commands={})
    spec.commands["NOOP"] = [HoldEffect(field="EVT_MESSAGE", value=5.0)]
    eng = behavior.BehaviorEngine(spec, simdef)
    with caplog.at_level("WARNING"):
        eng.apply_command(_cmd(simdef, "NOOP"), {})
        for _ in range(5):
            eng.tick(1.0)
    assert "EVT_MESSAGE" not in eng._behaviors  # refused at start, not per tick
    assert caplog.text.count("not a numeric field") == 1


def test_oscillator_clock_advances_while_center_unresolved(tmp_path, simdef):
    # Center resolves only after 25s (quarter period); the wave must resume
    # at its true phase, not restart from zero.
    spec = _load(
        tmp_path,
        simdef,
        "[_signals]\nTHM_RADIATOR = "
        '{ oscillate = "@THM_HEATER1_SETPOINT", amplitude = 20, period = 100 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    for _ in range(25):
        eng.tick(1.0)  # unresolved: no writes, but the clock runs
    assert "THM_RADIATOR" not in eng.state
    eng.state["THM_HEATER1_SETPOINT"] = 1000  # wire counts: 10.0 degC
    eng.tick(25.0)  # now at t=50 = half period: sine crosses center
    assert _eu(eng, "THM_RADIATOR") == 10


def test_restarted_behavior_continues_noise_stream(tmp_path, simdef):
    # Re-issuing a noisy behavior must NOT replay the same gauss draws.
    text = "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { hold = 30.0, noise = 2.0 }\n"
    eng = behavior.BehaviorEngine(_load(tmp_path, simdef, text), simdef)
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    first = [eng.tick(1.0) or eng.state["IMG_FOCAL_PLANE_TEMP"] for _ in range(4)]
    assert len(set(first)) > 1  # the hold actually jitters, not a flat line
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})  # restart the behavior
    second = [eng.tick(1.0) or eng.state["IMG_FOCAL_PLANE_TEMP"] for _ in range(4)]
    assert first != second  # stream continues, no replay
    # ...while two fresh engines still reproduce each other exactly.
    eng_a = behavior.BehaviorEngine(_load(tmp_path, simdef, text), simdef)
    eng_b = behavior.BehaviorEngine(_load(tmp_path, simdef, text), simdef)
    for e in (eng_a, eng_b):
        e.apply_command(_cmd(simdef, "IMAGER_ON"), {})
        e.tick(1.0)
    assert eng_a.state["IMG_FOCAL_PLANE_TEMP"] == eng_b.state["IMG_FOCAL_PLANE_TEMP"]


# ---- unit 5: immediate emission ---------------------------------------------


def test_immediate_rejected_on_continuous_verbs(tmp_path, simdef):
    msg = _errors(
        tmp_path,
        simdef,
        "[IMAGER_ON]\n"
        'IMG_FOCAL_PLANE_TEMP = { ramp_to = 35, tau = 5, emit = "immediate" }\n'
        "[_signals]\n"
        'THM_RADIATOR = { hold = -5, emit = "immediate" }\n'
        'PWR_SOLAR_VOLTAGE = { oscillate = 16, amplitude = 2, period = 60, emit = "immediate" }\n',
    )
    assert msg.count('emit = "immediate" is not valid with') == 3


def test_immediate_apids_collected_deduped_and_cleared(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        "[IMAGER_ON]\n"
        'IMG_STATE = { set = "IDLE", emit = "immediate" }\n'
        'IMG_GAIN = { set = 2, emit = "immediate" }\n'  # same packet
        'THM_HEATER1_STATE = { set = "ON", emit = "immediate" }\n'  # other packet
        "IMG_EXPOSURE_MS = 5\n",  # interval-paced
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    imager = simdef.packet_by_name("IMAGER_STATUS").apid
    thermal = simdef.packet_by_name("THERMAL_STATUS").apid
    assert eng.pop_immediate_apids() == {imager, thermal}  # deduped per packet
    assert eng.pop_immediate_apids() == set()  # cleared on read


def test_immediate_skipped_effect_emits_nothing(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        '[SET_EXPOSURE]\nIMG_EXPOSURE_MS = { set = "@arg:ExposureMs", emit = "immediate" }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "SET_EXPOSURE"), {})  # arg missing: warn-skip
    assert eng.pop_immediate_apids() == set()


async def test_immediate_emission_end_to_end(tmp_path, simdef):
    # With a 5s beacon, the only way IMAGER_STATUS arrives within 2s of the
    # command is the immediate path.
    import asyncio

    from xtce_sim import ccsds, client, codec
    from xtce_sim.server import SimServer

    spec = _load(
        tmp_path,
        simdef,
        '[TAKE_IMAGE]\nIMG_STATE = { set = "CAPTURING", emit = "immediate" }\n',
    )
    engine = behavior.BehaviorEngine(spec, simdef)
    server = SimServer(
        simdef,
        host="127.0.0.1",
        port=0,
        beacon_interval=5.0,
        behavior_engine=engine,
    )
    await server.start()
    try:
        imager = simdef.packet_by_name("IMAGER_STATUS")

        def read_one():
            for pkt in client.stream_packets("127.0.0.1", server.bound_port, timeout=2.0):
                header = ccsds.CCSDSHeader.unpack(pkt[:6])
                if header.apid == imager.apid:
                    return codec.unpack_telemetry(imager, pkt[6:])
            return None

        reader = asyncio.create_task(asyncio.to_thread(read_one))
        for _ in range(100):  # wait until the monitor is registered
            if server.client_count >= 1:
                break
            await asyncio.sleep(0.05)
        assert server.client_count >= 1
        cmd = simdef.command_by_name("TAKE_IMAGE")
        await asyncio.to_thread(
            client.send_command, "127.0.0.1", server.bound_port, cmd, {"ImageCount": "1"}
        )
        values = await reader
        assert values is not None  # arrived out-of-cycle, not on the 5s beacon
        assert values["IMG_STATE"] == 2  # CAPTURING
    finally:
        await server.stop()


async def test_failing_immediate_send_skips_but_continues(tmp_path, simdef):
    # One bad packet must not drop the other immediate emissions or the
    # command handler (same per-packet guard the beacon has).
    import asyncio

    from xtce_sim import client
    from xtce_sim.server import SimServer

    spec = _load(
        tmp_path,
        simdef,
        "[TAKE_IMAGE]\n"
        'IMG_STATE = { set = "CAPTURING", emit = "immediate" }\n'
        'EVT_EVENT_ID = { set = 11, emit = "immediate" }\n',
    )
    engine = behavior.BehaviorEngine(spec, simdef)
    handled = []

    async def handler(server, command, args):
        handled.append(command.name)

    server = SimServer(
        simdef,
        host="127.0.0.1",
        port=0,
        beacon_interval=60.0,
        behavior_engine=engine,
        command_handler=handler,
    )
    imager_apid = simdef.packet_by_name("IMAGER_STATUS").apid
    sent = []
    real_send = server.send_packet

    def flaky_send(apid, **kwargs):
        if apid == imager_apid:
            raise RuntimeError("boom")
        sent.append(apid)
        return real_send(apid, **kwargs)

    server.send_packet = flaky_send
    await server.start()
    try:
        cmd = simdef.command_by_name("TAKE_IMAGE")
        await asyncio.to_thread(
            client.send_command, "127.0.0.1", server.bound_port, cmd, {"ImageCount": "1"}
        )
        await asyncio.sleep(0.3)
        event_apid = simdef.packet_by_name("EVENT_LOG").apid
        assert sent == [event_apid]  # the other packet still went out
        assert handled == ["TAKE_IMAGE"]  # handler still ran
    finally:
        await server.stop()


# ---- engineering-unit rule: sidecar speaks EU, the wire speaks counts --------


def test_behavior_values_are_engineering_units(tmp_path, simdef):
    # 25.5 degC seeds as 2550 counts (0.01 degC/count); increment adds degrees.
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nTHM_RADIATOR = -5.0\n[IMAGER_ON]\nTHM_RADIATOR = { increment = 1.5 }\n",
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    assert eng.state["THM_RADIATOR"] == -500  # counts on the wire
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    assert eng.state["THM_RADIATOR"] == -350  # -5.0 + 1.5 degC = -350 counts


def test_eu_quantization_round_trip(tmp_path, simdef):
    # A value between count boundaries lands on the nearest count — the
    # readback shows real quantization, like actual telemetry.
    spec = _load(tmp_path, simdef, "[_initial]\nTHM_RADIATOR = 25.304\n")
    eng = behavior.BehaviorEngine(spec, simdef)
    assert eng.state["THM_RADIATOR"] == 2530  # nearest count
    assert eng._engineering("THM_RADIATOR", eng.state["THM_RADIATOR"]) == 25.30


def test_live_reference_across_different_scales(tmp_path, simdef):
    # A ramp on a temperature (0.01 degC/count) tracking a setpoint field:
    # both sides of the reference resolve in engineering units, so the ramp
    # lands at the setpoint's EU value regardless of count scales.
    spec = _load(
        tmp_path,
        simdef,
        "[_initial]\nTHM_HEATER1_SETPOINT = 33.0\n"
        '[HEATER_ON]\n"THM_HEATER{HeaterId}_TEMP" = '
        '{ ramp_to = "@THM_HEATER1_SETPOINT", tau = 2 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    for _ in range(40):
        eng.tick(2.0)
    assert _eu(eng, "THM_HEATER1_TEMP") == 33.0


def test_non_invertible_calibrator_rejected_at_load(tmp_path, simdef_quadratic):
    msg = _errors_for(
        tmp_path,
        simdef_quadratic,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 30, tau = 5 }\n"
        "[_initial]\nIMG_FOCAL_PLANE_TEMP = 20.0\n",
    )
    assert msg.count("non-invertible calibrator") == 2  # both uses flagged


def test_copy_to_non_invertible_field_rejected_at_load(tmp_path, simdef_quadratic):
    msg = _errors_for(
        tmp_path,
        simdef_quadratic,
        '[SET_EXPOSURE]\nIMG_FOCAL_PLANE_TEMP = "@arg:ExposureMs"\n',
    )
    assert "non-invertible calibrator" in msg


def test_deferred_template_to_non_invertible_field_refused_once(tmp_path, simdef_quadratic, caplog):
    # An unbounded template escapes load validation; the runtime refusal
    # happens once at start with the real reason, not per tick.
    spec = _load(
        tmp_path,
        simdef_quadratic,
        '[SET_EXPOSURE]\n"IMG_FOCAL_PLANE_{ExposureMs}" = { hold = 25.0 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef_quadratic)
    cmd = _cmd(simdef_quadratic, "SET_EXPOSURE")
    with caplog.at_level("WARNING"):
        eng.apply_command(cmd, {"ExposureMs": "TEMP", "GainLevel": 1})
        for _ in range(5):
            eng.tick(1.0)
    assert "IMG_FOCAL_PLANE_TEMP" not in eng._behaviors
    assert caplog.text.count("non-invertible calibrator") == 1


def test_effects_log_confirms_in_engineering_units(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        '[SET_HEATER_SETPOINT]\n"THM_HEATER{HeaterId}_SETPOINT" = "@arg:Setpoint"\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    applied = eng.apply_command(
        _cmd(simdef, "SET_HEATER_SETPOINT"), {"HeaterId": 1, "Setpoint": 55}
    )
    assert applied == ["THM_HEATER1_SETPOINT=55.0 (5500 counts)"]


# ---- satellite-as-directory: multi-file behavior ----------------------------


def test_directory_source_merges_files(simdef):
    # Content counts are pinned by test_shipped_imaging_sidecar_validates;
    # this test pins the MERGE: all six files, commands from different files.
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    assert len(spec.files) == 6  # adcs, comms, imager, power, system, thermal
    assert "HEATER_ON" in spec.commands and "TAKE_IMAGE" in spec.commands
    assert "(6 file(s))" in spec.source_label


def test_cross_file_conflict_is_load_error(tmp_path, simdef):
    (tmp_path / "a.toml").write_text("[_initial]\nTHM_RADIATOR = 1.0\n")
    (tmp_path / "b.toml").write_text("[_initial]\nTHM_RADIATOR = 2.0\n")
    with pytest.raises(behavior.BehaviorError) as exc:
        load_behavior(tmp_path, simdef)
    assert "already declared in a.toml" in str(exc.value)
    assert "b.toml:" in str(exc.value)  # error attributed to its file


def test_same_command_merges_across_files(tmp_path, simdef):
    # Different fields of one command in two files is legitimate ownership.
    (tmp_path / "imager.toml").write_text('[IMAGER_ON]\nIMG_STATE = "IDLE"\n')
    (tmp_path / "events.toml").write_text(
        '[IMAGER_ON]\nEVT_EVENT_ID = { set = 10, emit = "immediate" }\n'
    )
    spec = load_behavior(tmp_path, simdef)
    assert len(spec.commands["IMAGER_ON"]) == 2


def test_bad_toml_in_one_file_reported_with_others(tmp_path, simdef):
    (tmp_path / "a.toml").write_text("not [valid toml\n")
    (tmp_path / "b.toml").write_text("[_initial]\nNOT_A_FIELD = 1\n")
    with pytest.raises(behavior.BehaviorError) as exc:
        load_behavior(tmp_path, simdef)
    msg = str(exc.value)
    assert "a.toml: not valid TOML" in msg and "b.toml:" in msg


# ---- ADCS dynamics model (shipped adcs.toml [_models.adcs]) -----------------
#
# Commands are INPUTS to a physics model now: a slew CONVERGES over ticks
# through the wheel motors instead of teleporting the quaternion, and every
# ADCS field is a model output refreshed each tick.


@pytest.fixture()
def adcs_engine(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    return behavior.BehaviorEngine(spec, simdef)


def _adcs_apids(simdef):
    return {
        simdef.packet_by_name(n).apid
        for n in ("ADCS_STATUS", "ADCS_ATTITUDE", "ADCS_WHEELS", "ADCS_SENSORS")
    }


def _mode_raw(simdef, label):
    status = simdef.packet_by_name("ADCS_STATUS")
    field = next(f for f in status.fields if f.name == "ADCS_MODE")
    return field.enumerations[label]


def test_shipped_model_seeds_a_live_attitude_at_boot(adcs_engine, simdef):
    # Before any beacon tick, the overlay already holds a real solution —
    # an all-zeros quaternion is not an attitude.
    assert _eu(adcs_engine, "ADCS_ATT_QUAT_Q4") == pytest.approx(1.0, abs=0.01)
    assert adcs_engine.state["ADCS_MODE"] == _mode_raw(simdef, "STANDBY")
    assert _eu(adcs_engine, "ADCS_MAG_Z") != 0.0  # dipole field is live


def test_slew_to_quaternion_really_rotates_the_body(adcs_engine, simdef):
    applied = adcs_engine.apply_command(
        _cmd(simdef, "ADCS_SLEW_TO_QUATERNION"),
        {"Q1": 0.0, "Q2": 0.0, "Q3": 0.7071, "Q4": 0.7071},
    )
    assert any("slew" in line for line in applied)
    # The command flips to INERTIAL_POINT and the vehicle is NOT there yet.
    assert adcs_engine.state["ADCS_MODE"] == _mode_raw(simdef, "INERTIAL_POINT")
    assert _eu(adcs_engine, "ADCS_ATT_QUAT_Q3") == pytest.approx(0.0, abs=0.01)
    # Every ADCS packet shows the mode change immediately.
    assert adcs_engine.pop_immediate_apids() == _adcs_apids(simdef)
    # Ten beacon intervals later the 90-degree slew has converged.
    for _ in range(24):
        adcs_engine.tick(5.0)
    assert _eu(adcs_engine, "ADCS_ATT_QUAT_Q3") == pytest.approx(0.7071, abs=0.005)
    assert _eu(adcs_engine, "ADCS_ATT_QUAT_Q4") == pytest.approx(0.7071, abs=0.005)
    assert _eu(adcs_engine, "ADCS_POINTING_ERR") < 0.2


def test_slew_to_angles_converges_on_the_commanded_euler(adcs_engine, simdef):
    adcs_engine.apply_command(
        _cmd(simdef, "ADCS_SLEW_TO_ANGLES"),
        {"Roll": 10.5, "Pitch": -15.25, "Yaw": 45.0},
    )
    for _ in range(24):
        adcs_engine.tick(5.0)
    assert _eu(adcs_engine, "ADCS_ATT_ROLL") == pytest.approx(10.5, abs=0.1)
    assert _eu(adcs_engine, "ADCS_ATT_PITCH") == pytest.approx(-15.25, abs=0.1)
    assert _eu(adcs_engine, "ADCS_ATT_YAW") == pytest.approx(45.0, abs=0.1)


def test_wheel_set_speed_spins_up_through_the_motor(adcs_engine, simdef):
    # STANDBY (boot mode): the wheel obeys its speed servo, which rides the
    # torque limit — the speed is a spin-up curve, not a step.
    adcs_engine.apply_command(
        _cmd(simdef, "ADCS_WHEEL_SET_SPEED"), {"WheelId": 2, "Speed": -2200.0}
    )
    assert _eu(adcs_engine, "ADCS_WHEEL2_SPEED") == pytest.approx(0.0, abs=1.0)
    adcs_engine.tick(30.0)
    partway = _eu(adcs_engine, "ADCS_WHEEL2_SPEED")
    assert -2200.0 < partway < -400.0  # moving, not arrived
    for _ in range(4):
        adcs_engine.tick(30.0)
    assert _eu(adcs_engine, "ADCS_WHEEL2_SPEED") == pytest.approx(-2200.0, rel=0.02)
    # The others stayed parked, and their telemetry says so.
    assert _eu(adcs_engine, "ADCS_WHEEL1_SPEED") == pytest.approx(0.0, abs=1.0)
    # Spinning draws more than idle current.
    assert _eu(adcs_engine, "ADCS_WHEEL2_CURRENT") >= 0.05


def test_mtq_enable_reflects_state_enum(adcs_engine, simdef):
    adcs_engine.apply_command(_cmd(simdef, "ADCS_MTQ_ENABLE"), {"State": "OFF"})
    status = simdef.packet_by_name("ADCS_STATUS")
    field = next(f for f in status.fields if f.name == "ADCS_MTQ_STATE")
    assert adcs_engine.state["ADCS_MTQ_STATE"] == field.enumerations["OFF"]
    assert adcs_engine.pop_immediate_apids() == _adcs_apids(simdef)


def test_track_target_flips_mode_to_target_track(adcs_engine, simdef):
    adcs_engine.apply_command(
        _cmd(simdef, "ADCS_TRACK_TARGET"), {"Latitude": 34.05, "Longitude": -118.24}
    )
    assert adcs_engine.state["ADCS_MODE"] == _mode_raw(simdef, "TARGET_TRACK")
    assert adcs_engine.pop_immediate_apids() == _adcs_apids(simdef)


def test_set_mode_and_estimator_state_are_telemetered(adcs_engine, simdef):
    adcs_engine.apply_command(_cmd(simdef, "ADCS_SET_MODE"), {"Mode": "NADIR"})
    assert adcs_engine.state["ADCS_MODE"] == _mode_raw(simdef, "NADIR")
    status = simdef.packet_by_name("ADCS_STATUS")
    est = next(f for f in status.fields if f.name == "ADCS_EST_STATE")
    assert adcs_engine.state["ADCS_EST_STATE"] in set(est.enumerations.values())


def test_bad_wheel_id_warns_and_applies_nothing(adcs_engine, simdef, caplog):
    with caplog.at_level(logging.WARNING, logger="xtce_sim.dynamics"):
        applied = adcs_engine.apply_command(_cmd(simdef, "ADCS_WHEEL_ENABLE"), {"WheelId": 9})
    assert applied == []
    assert "WheelId 9 out of range" in caplog.text
    # A rejected command emits NOTHING out of cycle: an unscheduled ADCS
    # burst on the wire must always mean "the command took effect".
    assert adcs_engine.pop_immediate_apids() == set()


def test_model_field_cannot_be_written_by_another_table(tmp_path, simdef):
    # Ownership: adcs.toml binds ADCS_MODE to the model; a command table
    # writing it anywhere in the satellite directory is a load error.
    import shutil

    for toml in (EXAMPLES / "imaging_sat").glob("*.toml"):
        shutil.copy(toml, tmp_path)
    shutil.copy(EXAMPLES / "imaging_sat" / "imaging_sat.xml", tmp_path)
    (tmp_path / "zz_extra.toml").write_text('[ADCS_SET_MODE]\nADCS_MODE = { set = "@arg:Mode" }\n')
    with pytest.raises(behavior.BehaviorError) as exc:
        load_behavior(tmp_path, simdef)
    assert "owned by model 'adcs'" in str(exc.value)


def test_two_models_cannot_consume_the_same_command(tmp_path, simdef):
    # Disjoint outputs pass field ownership, but both models default-bind
    # ADCS_SET_MODE — dict routing would silently steer only the last one.
    wheel = (
        "[[_models.%s.wheels]]\n"
        "axis = [0.6, 0.0, 0.8]\n"
        "inertia = 0.02\n"
        "max_torque = 0.05\n"
        "max_speed = 600.0\n"
    )
    text = ""
    for name, binding in (("a", 'ADCS_MODE = "mode"'), ("b", 'ADCS_EST_STATE = "est_state"')):
        text += (
            f"[_models.{name}]\n"
            f"[_models.{name}.body]\ninertia = [12.0, 14.0, 9.0]\n"
            + wheel % name
            + f"[_models.{name}.outputs]\n{binding}\n"
        )
    (tmp_path / "two.behavior.toml").write_text(text)
    with pytest.raises(behavior.BehaviorError) as exc:
        load_behavior(tmp_path, simdef)
    assert "command ADCS_SET_MODE: consumed by both 'a' and 'b'" in str(exc.value)


# ---- [_environment]: the one shared world -----------------------------------


def test_environment_table_builds_the_spec_world(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        "[_environment.orbit]\naltitude_km = 700.0\ninclination_deg = 98.0\n",
    )
    assert spec.environment.orbit.altitude == pytest.approx(700e3)
    # absent table -> the documented default world
    bare = _load(tmp_path, simdef, "[_initial]\nIMG_GAIN = 1\n")
    assert bare.environment.orbit.altitude == pytest.approx(500e3)


def test_environment_declared_twice_is_a_conflict(tmp_path, simdef):
    (tmp_path / "a.behavior.toml").write_text("[_environment.orbit]\naltitude_km = 500.0\n")
    (tmp_path / "b.behavior.toml").write_text("[_environment.orbit]\naltitude_km = 700.0\n")
    with pytest.raises(behavior.BehaviorError) as exc:
        load_behavior(tmp_path, simdef)
    assert "[_environment] world: already declared in" in str(exc.value)


def test_every_model_shares_the_one_spec_environment(engine, simdef):
    # The unit's whole point: models are tenants of ONE world object —
    # identity, not equality, so two suns can never disagree.
    assert engine.models, "shipped example must have a model"
    for model in engine.models:
        assert model.environment is engine.spec.environment


def test_environment_is_narrated(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    assert any(
        line.startswith("environment: orbit 500 km @ 51.6 deg") for line in behavior.describe(spec)
    )


def test_templated_effect_cannot_teleport_a_model_wheel_speed(tmp_path, simdef, caplog):
    # A templated command effect escapes the load-time literal-name
    # ownership check; the engine's runtime guard must refuse it, or the
    # immediately-emitted packet would report the setpoint-teleport this
    # unit exists to eliminate.
    import shutil

    for toml in (EXAMPLES / "imaging_sat").glob("*.toml"):
        shutil.copy(toml, tmp_path)
    (tmp_path / "zz_teleport.toml").write_text(
        '[ADCS_WHEEL_SET_SPEED]\n"ADCS_WHEEL{WheelId}_SPEED" = "@arg:Speed"\n'
    )
    spec = load_behavior(tmp_path, simdef)  # loads: the template hides the name
    engine = behavior.BehaviorEngine(spec, simdef)
    with caplog.at_level(logging.WARNING):
        engine.apply_command(_cmd(simdef, "ADCS_WHEEL_SET_SPEED"), {"WheelId": 1, "Speed": -2200.0})
    assert "owned by a model; skipped" in caplog.text
    # The wheel is spinning up through its motor, not teleported.
    assert abs(_eu(engine, "ADCS_WHEEL1_SPEED")) < 1.0


# ---- my_vehicle 3-wheel model (tests/data/my_vehicle/adcs.toml) --------------
#
# The same physics stack, configured for a subset ICD: three orthogonal
# wheels, six of the eleven command roles, and a mode enum that
# legitimately omits TARGET_TRACK (no track command) with STANDBY at a
# different raw value than the ImagingSat's.
#
# This vehicle is a FIXTURE, not an example (see tests/data/README.md): it is
# the only thing proving the model is driven by the XTCE rather than built
# around imaging_sat. If it ever stops earning that, delete it — but do not
# quietly let it rot.


@pytest.fixture(scope="module")
def mv_simdef() -> SimDefinition:
    return SimDefinition.from_xtce(
        [
            DATA / "my_vehicle/my_vehicle_commands.xml",
            DATA / "my_vehicle/my_vehicle_telemetry.xml",
        ]
    )


@pytest.fixture()
def mv_engine(mv_simdef):
    spec = load_behavior(DATA / "my_vehicle", mv_simdef)
    return behavior.BehaviorEngine(spec, mv_simdef)


def test_my_vehicle_boots_with_its_own_mode_encoding(mv_engine, mv_simdef):
    # Labels map through THIS vehicle's enum: STANDBY is raw 4 here (the
    # ImagingSat's is 5) — a wire-value copy would be wrong on one of them.
    status = mv_simdef.packet_by_name("ADCS_STATUS")
    mode = next(f for f in status.fields if f.name == "ADCS_MODE")
    assert mode.enumerations["STANDBY"] == 4
    assert "TARGET_TRACK" not in mode.enumerations  # the subset is the point
    assert mv_engine.state["ADCS_MODE"] == 4
    assert _eu(mv_engine, "ADCS_ATT_QUAT_Q4") == pytest.approx(1.0, abs=0.01)


def test_my_vehicle_slew_converges_on_three_wheels(mv_engine, mv_simdef):
    applied = mv_engine.apply_command(
        _cmd(mv_simdef, "ADCS_SLEW_TO_QUATERNION"),
        {"Q1": 0.0, "Q2": 0.0, "Q3": 0.7071, "Q4": 0.7071},
    )
    assert any("slew" in line for line in applied)
    for _ in range(24):
        mv_engine.tick(5.0)
    assert _eu(mv_engine, "ADCS_ATT_QUAT_Q3") == pytest.approx(0.7071, abs=0.005)
    assert _eu(mv_engine, "ADCS_POINTING_ERROR") < 0.2


def test_my_vehicle_unwired_roles_are_simply_absent(mv_engine):
    model = mv_engine.models[0]
    assert not model.handles("ADCS_TRACK_TARGET")
    assert not model.handles("ADCS_SET_GYRO_BIAS")
    assert sorted(model.config.commands) == [
        "desaturate",
        "reset_estimator",
        "set_mode",
        "slew_to_quaternion",
        "wheel_disable",
        "wheel_enable",
    ]


# ---- verb-registry refactor pins ---------------------------------------------


def test_duck_typed_signal_is_skipped_not_fatal(simdef, caplog):
    class NotAnEffect:
        field = "THM_RADIATOR"

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    spec.signals.append(NotAnEffect())
    spec.signals.append(object())  # even field-less junk must not crash boot
    with caplog.at_level(logging.WARNING, logger="xtce_sim.behavior"):
        eng = behavior.BehaviorEngine(spec, simdef)  # must not raise
    assert "not a continuous behavior; skipped" in caplog.text
    assert eng.state  # the engine came up anyway


def test_instance_shadow_cannot_misroute_continuity(simdef):
    from xtce_sim.behavior import HoldEffect

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    eng = behavior.BehaviorEngine(spec, simdef)
    h = HoldEffect(field="HK_ISSUED_TIMESTAMP", value=1.0)
    h.continuous = False  # an instance shadow must not beat the class flag
    eng.spec.commands.setdefault("NOOP", []).append(h)
    eng.apply_command(_cmd(simdef, "NOOP"), {})
    assert "HK_ISSUED_TIMESTAMP" in eng._behaviors  # routed continuous


def test_verbs_package_reload_is_idempotent():
    import importlib

    import xtce_sim.behavior.verbs as verbs_pkg
    from xtce_sim.behavior.spec import VERBS

    importlib.reload(verbs_pkg)
    assert list(VERBS) == ["set", "increment", "ramp_to", "oscillate", "hold", "regulate"]


def test_copyarg_skips_are_loud_never_silent(simdef, caplog):
    from xtce_sim.behavior import CopyArgEffect

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.spec.commands.setdefault("NOOP", []).append(
        CopyArgEffect(field="HK_ISSUED_TIMESTAMP", arg="Ghost")
    )
    with caplog.at_level(logging.WARNING, logger="xtce_sim.behavior"):
        eng.apply_command(_cmd(simdef, "NOOP"), {"Ghost": None})
    assert "does not fit field" in caplog.text  # a None value reaches _store's warning


def test_late_registered_verb_is_fully_honored(tmp_path, simdef):
    from xtce_sim.behavior import HoldEffect
    from xtce_sim.behavior.spec import VERBS, Verb, register_verb

    def parse(where, fname, spec, command, emit, ctx):
        return HoldEffect(field=fname, value=float(spec["pulse"]), emit=emit)

    register_verb(Verb(name="pulse", attrs=frozenset({"width"}), continuous=True, parse=parse))
    try:
        spec = _load(
            tmp_path,
            simdef,
            "[_signals]\nHK_ISSUED_TIMESTAMP = { pulse = 5.0, width = 1.0 }\n",
        )
        [eff] = spec.signals
        assert isinstance(eff, HoldEffect)
        assert eff.value == 5.0
    finally:
        del VERBS["pulse"]


def test_register_verb_refuses_duplicate_names():
    from xtce_sim.behavior.spec import VERBS, register_verb

    with pytest.raises(ValueError, match="already registered"):
        register_verb(VERBS["hold"])


def test_verb_and_effect_continuity_agree():
    from xtce_sim.behavior import (
        CopyArgEffect,
        HoldEffect,
        IncrementEffect,
        OscillateEffect,
        RampEffect,
        RegulateEffect,
        SetEffect,
    )
    from xtce_sim.behavior.spec import VERBS

    assert {name: verb.continuous for name, verb in VERBS.items()} == {
        "set": False,
        "increment": False,
        "ramp_to": True,
        "oscillate": True,
        "hold": True,
        "regulate": True,
    }
    assert not SetEffect.continuous
    assert not CopyArgEffect.continuous
    assert not IncrementEffect.continuous
    assert RampEffect.continuous
    assert OscillateEffect.continuous
    assert HoldEffect.continuous
    assert RegulateEffect.continuous


def test_behavior_registry_key_governs_store_and_retirement(simdef):
    from xtce_sim.behavior.verbs.ramp import _ActiveRamp

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.state["HK_ISSUED_TIMESTAMP"] = 100
    other_before = eng.state.get("HK_RECEIVED_TIMESTAMP")
    # Hand-seeded entry whose .field disagrees with its key: the KEY is
    # authoritative for stores and retirement, and completion must not
    # raise KeyError out of tick() (which would kill the beacon loop).
    eng._behaviors["HK_ISSUED_TIMESTAMP"] = _ActiveRamp(
        field="HK_RECEIVED_TIMESTAMP", target=100.0, tau=1.0
    )
    eng.tick(1.0)
    assert "HK_ISSUED_TIMESTAMP" not in eng._behaviors  # retired under its key
    assert eng.state["HK_ISSUED_TIMESTAMP"] == 100
    assert eng.state.get("HK_RECEIVED_TIMESTAMP") == other_before  # never written
