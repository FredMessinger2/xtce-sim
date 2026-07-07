"""Behavior-sidecar loader/validation tests (xtce_sim.behavior + inspect wiring).

Validation is strict and total: every reference is checked against the
resolved SimDefinition and all problems are reported in one BehaviorError.
"""

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
IMAGING = EXAMPLES / "imaging_sat.xml"


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
    spec = load_behavior(EXAMPLES / "imaging_sat.behavior.toml", simdef)
    assert len(spec.initial) == 5
    assert set(spec.commands) == {
        "SET_MODE", "HEATER_ON", "HEATER_OFF", "SET_HEATER_SETPOINT",
        "IMAGER_ON", "IMAGER_OFF", "SET_EXPOSURE", "TAKE_IMAGE", "SET_ATTITUDE",
    }
    ramp = next(
        e for e in spec.commands["HEATER_ON"] if isinstance(e, RampEffect)
    )
    assert ramp.target == "@THM_HEATER{HeaterId}_SETPOINT" and ramp.tau == 30.0


def test_sidecar_discovery():
    assert sidecar_path([IMAGING]) == EXAMPLES / "imaging_sat.behavior.toml"
    assert sidecar_path([EXAMPLES / "my_vehicle.xml"]) is None  # none exists


# ---- effect parsing ---------------------------------------------------------


def test_effect_kinds_parse(tmp_path, simdef):
    spec = _load(
        tmp_path,
        simdef,
        """
        [IMAGER_ON]
        IMG_STATE = 1
        IMG_CAPTURE_COUNT = { increment = 1 }
        IMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0, tau = 20, emit = "immediate" }
        [SET_EXPOSURE]
        IMG_EXPOSURE_MS = "@arg:ExposureMs"
        """,
    )
    kinds = {type(e) for e in spec.commands["IMAGER_ON"]}
    assert kinds == {SetEffect, IncrementEffect, RampEffect}
    ramp = next(e for e in spec.commands["IMAGER_ON"] if isinstance(e, RampEffect))
    assert ramp.emit == "immediate"
    assert isinstance(spec.commands["SET_EXPOSURE"][0], CopyArgEffect)


def test_enum_label_set_is_valid(tmp_path, simdef):
    spec = _load(tmp_path, simdef, '[SET_MODE]\nHK_SYSTEM_MODE = "IMAGING"\n')
    assert spec.commands["SET_MODE"][0].value == "IMAGING"


# ---- validation errors ------------------------------------------------------


def test_unknown_command_table(tmp_path, simdef):
    assert "unknown command" in _errors(tmp_path, simdef, "[NO_SUCH_CMD]\nIMG_STATE = 1\n")


def test_unknown_field(tmp_path, simdef):
    assert "unknown telemetry field" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nNOT_A_FIELD = 1\n"
    )


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
        tmp_path, simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = 35.0, tau = -1 }\n",
    )
    assert "@FIELD reference" in _errors(
        tmp_path, simdef,
        '[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = "warm", tau = 5 }\n',
    )


def test_unknown_verb_key(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_too = 35.0, tau = 5 }\n",
    )
    assert "unknown key(s) ['ramp_too']" in msg


def test_bad_emit_value(tmp_path, simdef):
    assert "emit must be one of" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = { set = 1, emit = "now" }\n'
    )


def test_initial_unknown_field_and_bad_label(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef,
        '[_initial]\nNOT_A_FIELD = 1\nHK_SYSTEM_MODE = "WARP_SPEED"\n',
    )
    assert "unknown telemetry field" in msg and "not a label" in msg


def test_all_errors_collected_in_one_raise(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef,
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
    msg = _errors(
        tmp_path, simdef, '[HEATER_ON]\n"THM_HEATER{HeaterId}_STATE" = "BANANA"\n'
    )
    assert "not a label of THM_HEATER1_STATE" in msg
    assert "not a label of THM_HEATER2_STATE" in msg


def test_sidecar_path_keeps_dotted_stems(tmp_path):
    # v1.2.xml must map to v1.2.behavior.toml, not v1.behavior.toml.
    xtce = tmp_path / "v1.2.xml"
    xtce.write_text("<x/>")
    good = tmp_path / "v1.2.behavior.toml"
    good.write_text("")
    assert sidecar_path([xtce]) == good


def test_table_form_set_accepts_arg_copy_with_emit(tmp_path, simdef):
    # '@arg:' means copy in the table form too — that's how a copy gets
    # emit="immediate" — and it must not parse as a literal string set.
    spec = _load(
        tmp_path, simdef,
        '[SET_EXPOSURE]\nIMG_EXPOSURE_MS = { set = "@arg:ExposureMs", emit = "immediate" }\n',
    )
    eff = spec.commands["SET_EXPOSURE"][0]
    assert isinstance(eff, CopyArgEffect) and eff.emit == "immediate"


def test_stray_tau_rejected_outside_ramp(tmp_path, simdef):
    assert "tau is only valid with ramp_to" in _errors(
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
    assert "not a raw value" in _errors(
        tmp_path, simdef, "[SET_MODE]\nHK_SYSTEM_MODE = 99\n"
    )


def test_bare_at_field_set_value_gets_a_hint(tmp_path, simdef):
    msg = _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = "@IMG_EXPOSURE_MS"\n'
    )
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
    result = CliRunner().invoke(
        main, ["inspect", str(IMAGING), "--behavior", str(bad)]
    )
    assert result.exit_code != 0
    assert "unknown command" in result.output


def test_inspect_without_sidecar_unchanged():
    result = CliRunner().invoke(main, ["inspect", str(EXAMPLES / "my_vehicle.xml")])
    assert result.exit_code == 0, result.output
    assert "Behavior (" not in result.output


def test_describe_lines(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat.behavior.toml", simdef)
    lines = behavior.describe(spec)
    assert any(line.startswith("HEATER_ON:") for line in lines)
    assert any("tau=30.0s" in line for line in lines)


# ---- runtime engine ---------------------------------------------------------


@pytest.fixture()
def engine(simdef):
    spec = load_behavior(EXAMPLES / "imaging_sat.behavior.toml", simdef)
    return behavior.BehaviorEngine(spec, simdef)


def _cmd(simdef, name):
    return simdef.command_by_name(name)


def test_engine_seeds_initial_values(engine, simdef):
    thermal = simdef.packet_by_name("THERMAL_STATUS")
    values = engine.values_for(thermal)
    assert values["THM_HEATER1_TEMP"] == 20.0
    assert values["THM_HEATER1_SETPOINT"] == 40.0
    # values_for filters per packet: imager fields don't leak into thermal.
    assert "IMG_FOCAL_PLANE_TEMP" not in values


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
    spec = _load(tmp_path, simdef, "[SET_EXPOSURE]\nIMG_GAIN = \"@arg:GainLevel\"\n")
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "SET_EXPOSURE"), {"GainLevel": 99999})
    field = next(
        f for p in simdef.packets for f in p.fields if f.name == "IMG_GAIN"
    )
    assert eng.state["IMG_GAIN"] == (1 << field.size_bits) - 1  # clamped, no overflow


async def test_server_end_to_end_command_changes_telemetry(simdef):
    # The full loop: command in -> overlay mutated -> beacon carries it.
    from xtce_sim import ccsds, client, codec
    from xtce_sim.server import SimServer

    spec = load_behavior(EXAMPLES / "imaging_sat.behavior.toml", simdef)
    engine = behavior.BehaviorEngine(spec, simdef)
    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=0.05,
        behavior_engine=engine,
    )
    await server.start()
    try:
        import asyncio

        cmd = simdef.command_by_name("IMAGER_ON")
        await asyncio.to_thread(
            client.send_command, "127.0.0.1", server.bound_port, cmd, {}
        )
        await asyncio.sleep(0.1)  # let the dispatch land

        img = simdef.packet_by_name("IMAGER_STATUS")

        def read_one():
            for pkt in client.stream_packets(
                "127.0.0.1", server.bound_port, timeout=2.0
            ):
                header = ccsds.CCSDSHeader.unpack(pkt[:6])
                if header.apid == img.apid:
                    return codec.unpack_telemetry(img, pkt[6:])
            return None

        values = await asyncio.to_thread(read_one)
        assert values is not None
        assert values["IMG_STATE"] == 1  # IDLE, set by the command
        assert values["IMG_FOCAL_PLANE_TEMP"] == 20  # [_initial] seed (int field)
    finally:
        await server.stop()


# ---- review-driven runtime cases --------------------------------------------


def test_nonfinite_values_rejected_at_load(tmp_path, simdef):
    # TOML permits nan/inf literals; they must never reach the engine.
    assert "must be finite" in _errors(tmp_path, simdef, "[_initial]\nIMG_GAIN = nan\n")
    assert "must be finite" in _errors(tmp_path, simdef, "[IMAGER_ON]\nIMG_GAIN = inf\n")


def test_nonfinite_copied_argument_skipped_at_runtime(tmp_path, simdef):
    # A float argument can decode to nan off the wire — skip, never crash.
    spec = _load(tmp_path, simdef, '[SET_HEATER_SETPOINT]\nTHM_HEATER1_SETPOINT = "@arg:Setpoint"\n')
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "SET_HEATER_SETPOINT"), {"Setpoint": float("nan"), "HeaterId": 1})
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
        simdef, host="127.0.0.1", port=1,  # never started; just merging
        behavior_engine=engine,
        telemetry_source=lambda pkt: {f.name: 7 for f in pkt.fields},
    )
    thermal = simdef.packet_by_name("THERMAL_STATUS")
    values = server._packet_values(thermal)
    assert values["THM_HEATER1_TEMP"] == 20  # overlay ([_initial]) wins
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


def test_sidecar_path_empty_list():
    assert sidecar_path([]) is None


def test_describe_increment_line(tmp_path, simdef):
    spec = _load(tmp_path, simdef, "[TAKE_IMAGE]\nIMG_CAPTURE_COUNT = { increment = 2 }\n")
    assert any("IMG_CAPTURE_COUNT += 2" in line for line in behavior.describe(spec))


def test_non_table_bodies_rejected(tmp_path, simdef):
    msg = _errors(tmp_path, simdef, "_initial = 5\nIMAGER_ON = 7\n")
    assert "[_initial]: must be a table" in msg
    assert "[IMAGER_ON]: must be a table" in msg


def test_verb_count_must_be_exactly_one(tmp_path, simdef):
    assert "exactly one of set/ramp_to/increment" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = { emit = "interval" }\n'
    )
    assert "exactly one of set/ramp_to/increment" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_STATE = { set = 1, increment = 1 }\n"
    )


def test_increment_amount_must_be_number(tmp_path, simdef):
    assert "increment must be a number" in _errors(
        tmp_path, simdef, '[IMAGER_ON]\nIMG_STATE = { increment = "lots" }\n'
    )


def test_ramp_target_bool_rejected(tmp_path, simdef):
    assert "must be a number or an @FIELD reference" in _errors(
        tmp_path, simdef, "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = true, tau = 5 }\n"
    )


def test_ramp_on_string_field_rejected(tmp_path, simdef):
    # both the ramped field and an @target must be numeric
    assert "not a numeric field" in _errors(
        tmp_path, simdef, "[FILE_DELETE]\nFR_FILENAME = { ramp_to = 1, tau = 5 }\n"
    )
    assert "not a numeric field" in _errors(
        tmp_path, simdef,
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


def test_enum_arg_template_expands_raw_values(tmp_path, simdef):
    # Mode is enumerated: the template expands over its raw values, and the
    # nonexistent expansions are all named in the error.
    msg = _errors(tmp_path, simdef, '[SET_MODE]\n"X_{Mode}_Y" = 1\n')
    assert "X_0_Y" in msg and "X_4_Y" in msg


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
        tmp_path, simdef,
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
    # tau=30 toward the 40.0 setpoint from 20.0: one 30s tick covers 1-1/e.
    engine.tick(30.0)
    expected = 20.0 + (40.0 - 20.0) * (1.0 - math.exp(-1.0))
    assert engine.state["THM_HEATER1_TEMP"] == round(expected)  # int16 field view
    # after many time constants the ramp lands exactly and retires
    for _ in range(20):
        engine.tick(30.0)
    assert engine.state["THM_HEATER1_TEMP"] == 40
    assert "THM_HEATER1_TEMP" not in engine._ramps


def test_ramp_trajectory_is_tick_size_independent(tmp_path, simdef):
    spec = _load(
        tmp_path, simdef,
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
    assert engine.state["THM_HEATER1_TEMP"] == 40  # reached, not stalled at 20


def test_ramp_target_reread_live_each_tick(engine, simdef):
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    engine.tick(30.0)
    part_way = engine.state["THM_HEATER1_TEMP"]
    # raise the setpoint mid-ramp: the curve bends toward the new target
    engine.apply_command(_cmd(simdef, "SET_HEATER_SETPOINT"), {"HeaterId": 1, "Setpoint": 55})
    for _ in range(20):
        engine.tick(30.0)
    assert part_way < 40 < engine.state["THM_HEATER1_TEMP"] == 55


def test_new_ramp_replaces_old_per_field(engine, simdef):
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    for _ in range(10):
        engine.tick(30.0)  # warm up toward 40
    engine.apply_command(_cmd(simdef, "HEATER_OFF"), {"HeaterId": 1})  # cooling replaces
    for _ in range(30):
        engine.tick(30.0)
    assert engine.state["THM_HEATER1_TEMP"] == 20  # cooled back to ambient
    # heater 2 was never involved
    assert "THM_HEATER2_TEMP" not in engine._ramps


def test_ramp_holds_when_target_field_not_numeric_yet(tmp_path, simdef, caplog):
    import logging as _logging

    # @FIELD target with no value in the overlay: hold (warn), don't move.
    spec = _load(
        tmp_path, simdef,
        '[HEATER_ON]\n"THM_HEATER{HeaterId}_TEMP" = { ramp_to = "@THM_HEATER{HeaterId}_SETPOINT", tau = 30.0 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)  # no [_initial]: setpoint unset
    eng.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    with caplog.at_level(_logging.WARNING, logger="xtce_sim.behavior"):
        eng.tick(30.0)
    assert "THM_HEATER1_TEMP" not in eng.state  # held, nothing written
    assert any("has no numeric value yet" in r.getMessage() for r in caplog.records)


def test_tick_ignores_nonpositive_dt(engine, simdef):
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    engine.tick(0.0)
    engine.tick(-5.0)
    assert engine.state["THM_HEATER1_TEMP"] == 20  # unmoved


async def test_server_beacon_ticks_ramps_end_to_end(simdef):
    # Full loop: HEATER_ON, then watch the temperature climb across beacons.
    import asyncio

    from xtce_sim import ccsds, client, codec
    from xtce_sim.server import SimServer

    spec = load_behavior(EXAMPLES / "imaging_sat.behavior.toml", simdef)
    engine = behavior.BehaviorEngine(spec, simdef)
    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=0.05,
        behavior_engine=engine,
    )
    await server.start()
    try:
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
        assert values["THM_HEATER1_STATE"] == 1  # ON
        assert values["THM_HEATER1_TEMP"] > 20  # physics moved it
    finally:
        await server.stop()


def test_float_field_ramp_lands_exactly_and_retires(tmp_path, simdef):
    spec = _load(
        tmp_path, simdef,
        "[_initial]\nHK_ISSUED_TIMESTAMP = 0.0\n"
        "[IMAGER_ON]\nHK_ISSUED_TIMESTAMP = { ramp_to = 100.0, tau = 5 }\n",
    )
    eng = behavior.BehaviorEngine(spec, simdef)
    eng.apply_command(_cmd(simdef, "IMAGER_ON"), {})
    for _ in range(40):  # 40 * 5s = 40 time constants
        eng.tick(5.0)
    assert eng.state["HK_ISSUED_TIMESTAMP"] == 100.0  # exact landing
    assert "HK_ISSUED_TIMESTAMP" not in eng._ramps  # retired


def test_direct_write_cancels_active_ramp(engine, simdef):
    # Last command wins: an explicit set on a ramped field cancels the ramp,
    # so the next tick cannot silently revert the operator's value.
    engine.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    engine.tick(30.0)
    assert "THM_HEATER1_TEMP" in engine._ramps
    # a direct copy onto the ramped field (via setpoint command redirected)...
    spec_over = engine.spec.commands.setdefault("NOOP", [])
    from xtce_sim.behavior import SetEffect
    spec_over.append(SetEffect(field="THM_HEATER1_TEMP", value=33))
    engine.apply_command(_cmd(simdef, "NOOP"), {})
    assert engine.state["THM_HEATER1_TEMP"] == 33
    assert "THM_HEATER1_TEMP" not in engine._ramps  # ramp cancelled
    engine.tick(30.0)
    assert engine.state["THM_HEATER1_TEMP"] == 33  # value survives ticks


def test_missing_ramp_target_warns_once_not_per_tick(tmp_path, simdef, caplog):
    import logging as _logging

    spec = _load(
        tmp_path, simdef,
        '[HEATER_ON]\n"THM_HEATER{HeaterId}_TEMP" = { ramp_to = "@THM_HEATER{HeaterId}_SETPOINT", tau = 30.0 }\n',
    )
    eng = behavior.BehaviorEngine(spec, simdef)  # setpoint never seeded
    eng.apply_command(_cmd(simdef, "HEATER_ON"), {"HeaterId": 1})
    with caplog.at_level(_logging.WARNING, logger="xtce_sim.behavior"):
        for _ in range(10):
            eng.tick(1.0)
    warnings = [r for r in caplog.records if "no numeric value yet" in r.getMessage()]
    assert len(warnings) == 1  # once per ramp, not once per beacon tick
    # and the ramp recovers when the target appears (seed it directly —
    # this minimal spec has no SET_HEATER_SETPOINT table):
    eng.state["THM_HEATER1_SETPOINT"] = 40
    eng.tick(30.0)
    assert eng.state["THM_HEATER1_TEMP"] > 0  # moving now


def test_infinite_ramp_target_rejected_at_load(tmp_path, simdef):
    assert "must be finite" in _errors(
        tmp_path, simdef,
        "[IMAGER_ON]\nIMG_FOCAL_PLANE_TEMP = { ramp_to = inf, tau = 5 }\n",
    )
