"""Tests for building and dumping a SimDefinition from the example XTCE."""

import importlib.util
import json
import math
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim.cli import main
from xtce_sim.definition import SimDefinition
from xtce_sim.generate import fields_to_struct_format, format_json, format_text

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"
CMD_XML = DATA / "my_vehicle/my_vehicle_commands.xml"
TLM_XML = DATA / "my_vehicle/my_vehicle_telemetry.xml"
COMBINED_XTCE = DATA / "my_vehicle/my_vehicle.xml"
IMAGING_SAT_XTCE = EXAMPLES / "imaging_sat/imaging_sat.xml"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce([CMD_XML, TLM_XML])


def test_builds_commands_and_packets(simdef: SimDefinition):
    assert simdef.space_system_name == "MyVehicle"
    assert len(simdef.commands) == 61
    assert len(simdef.packets) == 18


def test_imaging_sat_example_builds_and_is_generic():
    # The second example (an imaging satellite) parses and builds, and its
    # source vendor branding was scrubbed when it was brought into the repo.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    assert d.space_system_name == "ImagingSat"
    assert len(d.commands) == 41
    assert len(d.packets) == 13  # incl. COMMS_STATUS (APID 28, ENABLE_BEACON's card)
    assert "VendorA" not in IMAGING_SAT_XTCE.read_text()


def test_imaging_sat_opcodes_are_declared_not_synthetic():
    # The example pins every opcode canonically (OPCODE ArgumentAssignment on
    # the abstract base), so nothing falls back to a synthetic opcode and the
    # values match the file's documented ranges.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    assert not any(c.synthetic for c in d.commands)
    opcodes = {c.name: c.opcode for c in d.commands}
    assert opcodes["NOOP"] == 0x00  # housekeeping 0x00-0x0F
    assert opcodes["SET_POWER"] == 0x10  # power 0x10-0x1F
    assert opcodes["LOAD_ATS"] == 0xD5  # ATS 0xD5-0xD8
    # OPCODE is fixed by the definition, not typed by the operator.
    assert all(p.name != "OPCODE" for c in d.commands for p in c.params)


def test_telemetry_field_enumerations_are_preserved():
    # Enumerated telemetry types carry their label map into the built field,
    # through the JSON dump, and into the text report (they used to be
    # dropped at build time, leaving monitor showing raw ints).
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    hk = d.packet_by_name("HOUSEKEEPING")
    mode = next(f for f in hk.fields if f.name == "HK_SYSTEM_MODE")
    assert mode.enumerations == {
        "SAFE": 0, "STANDBY": 1, "NOMINAL": 2, "IMAGING": 3, "DOWNLINK": 4
    }
    # Non-enumerated fields stay None.
    volts = next(f for f in hk.fields if f.name == "HK_BUS_VOLTAGE")
    assert volts.enumerations is None
    # JSON round-trip preserves the map.
    d2 = SimDefinition.from_dict(json.loads(format_json(d)))
    mode2 = next(
        f for f in d2.packet_by_name("HOUSEKEEPING").fields
        if f.name == "HK_SYSTEM_MODE"
    )
    assert mode2.enumerations == mode.enumerations
    # The text report shows it.
    assert "enum={'SAFE': 0" in format_text(d)


def test_imaging_sat_two_level_inheritance_resolves():
    # Commands inherit CCSDSCommand -> ImagingSatCommand (which assigns the
    # shared header values once) -> concrete command (which pins only its
    # OPCODE). Opcode extraction and user-argument exclusion must both walk
    # the full two-level chain.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    sp = d.command_by_name("SET_POWER")
    assert sp.opcode == 0x10 and not sp.synthetic  # own OPCODE, via the chain
    # Header fields assigned on the intermediate ancestor stay hidden; only
    # the command's real arguments remain operator-typed.
    assert [p.name for p in sp.params] == ["SubsystemId", "PowerState"]


def test_power_loads_are_a_subset_of_the_subsystem_ids():
    # PowerLoadIdType hand-copies four label/value pairs of SubsystemIdType
    # (XTCE has no enum-subset construct); this pin is the machine check the
    # XML comment 'Values match SubsystemIdType' promises. If the subsystem
    # numbering ever changes, this fails instead of the two commands quietly
    # addressing different subsystems with the same wire byte.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    loads = next(p for p in d.command_by_name("SET_POWER").params if p.name == "SubsystemId")
    subsystems = next(p for p in d.command_by_name("RESET").params if p.name == "SubsystemId")
    assert set(loads.enumerations.items()) <= set(subsystems.enumerations.items())


def test_declared_packet_periods_parse_to_durations():
    # DefaultRateInStream is the XTCE-standard per-packet rate declaration;
    # the boundary converts the standard's per-second rate into the period
    # our layer carries. Event-driven packets declare none.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    periods = {p.name: p.period_s for p in d.packets}
    assert periods["HOUSEKEEPING"] == 1.0
    assert periods["POWER_STATUS"] == 2.0
    assert periods["ADCS_ATTITUDE"] == 0.5
    assert periods["EVENT_LOG"] is None
    assert periods["FILE_RECEIPT"] is None


def test_periodic_packet_enum_values_are_the_apids():
    # PeriodicPacketIdType's contract: label = packet name, value = APID —
    # the ICD carries the mapping SET_TLM_RATE resolves through. Every
    # declared-periodic packet is addressable; the event packets are not.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    arg = next(p for p in d.command_by_name("SET_TLM_RATE").params if p.name == "Packet")
    by_name = {p.name: p for p in d.packets}
    for label, value in arg.enumerations.items():
        assert by_name[label].apid == value
    # BOTH directions: every enum entry is a declared-periodic packet, and
    # every declared-periodic packet is addressable — a new packet with a
    # DefaultRateInStream must land in the enum or this fails.
    assert set(arg.enumerations) == {
        p.name for p in d.packets if p.period_s is not None
    }


def test_period_survives_the_json_round_trip(tmp_path):
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    path = tmp_path / "cmd_tlm.json"
    path.write_text(format_json(d))
    back = SimDefinition.from_json(path)
    assert {p.name: p.period_s for p in back.packets} == {
        p.name: p.period_s for p in d.packets
    }


def test_enable_disable_declaration_order_is_pinned():
    # The exercise sweep sends a command's example values in declaration
    # order, so the LAST BeaconState sent decides the vehicle's beacon state
    # after a sweep. DISABLE first / ENABLE last keeps every sweep from
    # ending with the vehicle silenced — a cosmetic-looking reorder of
    # EnableDisableType would break the looping exerciser.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    arg = next(p for p in d.command_by_name("ENABLE_BEACON").params if p.name == "BeaconState")
    assert list(arg.enumerations) == ["DISABLE", "ENABLE"]


def test_beacon_state_mirror_enum_matches_the_argument():
    # comms.toml copies BeaconState's RAW value into COMM_BEACON_STATE, so
    # the argument enum (EnableDisableType) and the telemetry enum
    # (EnableStateParamType) must agree pair-for-pair, or the console would
    # label the opposite state. This pin is the machine check for the twin
    # hand-copied enums.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    arg = next(p for p in d.command_by_name("ENABLE_BEACON").params if p.name == "BeaconState")
    field = next(f for p in d.packets for f in p.fields if f.name == "COMM_BEACON_STATE")
    assert arg.enumerations == field.enumerations


def test_example_binary_fields_have_real_sizes():
    """Binary telemetry fields and command args carry their declared size, not 0
    (regression: BinaryDataEncoding SizeInBits/FixedValue was being dropped)."""
    d = SimDefinition.from_xtce(COMBINED_XTCE)

    md = d.packet_by_name("MEMORY_DUMP")
    data = next(f for f in md.fields if f.python_type == "bytes")
    assert data.size_bits == 2048  # 256 bytes
    assert md.struct_format.endswith("256s")  # not "0s"

    fd = d.packet_by_name("FILE_DATA")
    assert any(f.python_type == "bytes" and f.size_bits == 2080 for f in fd.fields)

    mw = d.command_by_name("MEM_WRITE")
    assert any(p.python_type == "bytes" and p.size_bits == 2048 for p in mw.params)


def test_combined_example_matches_split_pair(simdef: SimDefinition):
    """my_vehicle.xml (single combined file) must stay equivalent to the split
    command/telemetry pair — that pair is what covers multi-file merge."""
    combined = SimDefinition.from_xtce(COMBINED_XTCE)
    assert combined.space_system_name == "MyVehicle"
    assert [(c.name, c.opcode) for c in combined.commands] == [
        (c.name, c.opcode) for c in simdef.commands
    ]
    assert [(p.name, p.apid, p.struct_format) for p in combined.packets] == [
        (p.name, p.apid, p.struct_format) for p in simdef.packets
    ]


def test_opcodes_are_unique(simdef: SimDefinition):
    """Synthetic opcodes must not collide with real ones (regression)."""
    opcodes = [c.opcode for c in simdef.commands]
    assert len(opcodes) == len(set(opcodes))


def test_lookups(simdef: SimDefinition):
    cmd = simdef.commands[0]
    assert simdef.command_by_name(cmd.name) is cmd
    assert simdef.command_by_opcode(cmd.opcode) is cmd
    pkt = simdef.packets[0]
    assert simdef.packet_by_name(pkt.name) is pkt
    assert simdef.packet_by_apid(pkt.apid) is pkt


def test_struct_format_matches_field_bytes(simdef: SimDefinition):
    """struct.calcsize of each packet format equals the sum of its field bytes."""
    for pkt in simdef.packets:
        assert pkt.struct_format == fields_to_struct_format(pkt.fields)
        expected_bytes = sum(f.size_bits // 8 for f in pkt.fields)
        assert struct.calcsize(pkt.struct_format) == expected_bytes


def test_text_and_json_render(simdef: SimDefinition):
    text = format_text(simdef)
    assert "MyVehicle" in text and "COMMANDS" in text and "TELEMETRY" in text

    data = json.loads(format_json(simdef))
    assert data["space_system"] == "MyVehicle"
    assert len(data["commands"]) == 61
    assert len(data["telemetry"]) == 18


def test_cli_generate_writes_files(tmp_path):
    out = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["generate", str(CMD_XML), str(TLM_XML), "--out", str(out), "--emit-py"],
    )
    assert result.exit_code == 0, result.output
    assert (out / "cmd_tlm.txt").exists()
    assert (out / "cmd_tlm.json").exists()

    py_path = out / "generated.py"
    assert py_path.exists()

    # generated.py must be importable and internally consistent.
    spec = importlib.util.spec_from_file_location("generated_snapshot", py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert len(list(mod.CommandOpcode)) == 61
    assert len(list(mod.TelemetryAPID)) == 18
    assert len(mod.COMMAND_PARAMS) == 61
    for apid, fmt in mod.PACKET_FORMATS.items():
        struct.calcsize(fmt)  # must not raise


# ---- calibrators: raw counts on the wire, engineering units on display -------


def test_polynomial_calibrator_carried_into_field():
    d = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    hk = d.packet_by_name("HOUSEKEEPING")
    volts = next(f for f in hk.fields if f.name == "HK_BATTERY_VOLTAGE")
    assert volts.calibrator is not None
    assert volts.calibrator.coefficients == [(0.125, 1)]
    assert volts.calibrator.apply(60) == 7.5  # 60 counts * 0.125 V/count
    # the wire stays raw: the field's own type is the unsigned encoding
    assert volts.python_type == "uint8"


def test_spline_calibrator_parsed_and_applied():
    d = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    hk = d.packet_by_name("HOUSEKEEPING")
    therm = next(f for f in hk.fields if f.name == "HK_TEMP_THERMISTOR")
    cal = therm.calibrator
    assert cal is not None and len(cal.spline_points) == 5
    assert cal.apply(0) == -40.0  # exact declared point
    assert cal.apply(1024) == 0.0
    assert cal.apply(512) == -20.0  # linear midpoint between (0,-40) and (1024,0)
    assert cal.apply(-100) == -40.0  # clamped below the table
    assert cal.apply(9999) == 125.0  # clamped above the table


def test_calibrator_round_trips_through_json():
    d = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    d2 = SimDefinition.from_dict(json.loads(format_json(d)))
    hk2 = d2.packet_by_name("HOUSEKEEPING")
    volts = next(f for f in hk2.fields if f.name == "HK_BATTERY_VOLTAGE")
    therm = next(f for f in hk2.fields if f.name == "HK_TEMP_THERMISTOR")
    assert volts.calibrator.apply(60) == 7.5
    assert therm.calibrator.apply(512) == -20.0
    # uncalibrated fields stay clean
    count = next(f for f in hk2.fields if f.name == "HK_CMD_RECV_COUNT")
    assert count.calibrator is None


def test_text_report_marks_calibrated_fields():
    d = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    text = format_text(d)
    assert "cal=poly(1 terms)" in text
    assert "cal=spline(5 pts)" in text


def test_spline_no_longer_reported_ignored():
    from click.testing import CliRunner as _Runner

    result = _Runner().invoke(main, ["inspect", str(DATA / "my_vehicle/my_vehicle.xml")])
    assert result.exit_code == 0
    assert "SplineCalibrator" not in result.output  # consumed, not ignored


def test_negative_polynomial_exponent_rejected(tmp_path):
    # A negative exponent would make raw=0 undefined (ZeroDivisionError in
    # the monitor); the parser drops the term with a warning, mirroring the
    # XTCE schema's non-negative requirement.
    xml = (DATA / "my_vehicle/my_vehicle.xml").read_text().replace(
        '<xtce:Term coefficient="0.125" exponent="1" />',
        '<xtce:Term coefficient="0.125" exponent="-1" />',
        1,
    )
    bad = tmp_path / "bad.xml"
    bad.write_text(xml)
    d = SimDefinition.from_xtce(bad)
    volts = next(
        f for f in d.packet_by_name("HOUSEKEEPING").fields
        if f.name == "HK_BATTERY_VOLTAGE"
    )
    assert volts.calibrator is None  # sole term dropped -> no calibrator


def test_calibrator_defensive_edges():
    from xtce_sim.definition import CalibratorInfo

    # NaN through a spline propagates instead of masquerading as 0.0.
    cal = CalibratorInfo(spline_points=[(0.0, -40.0), (100.0, 60.0)])
    assert math.isnan(cal.apply(float("nan")))
    # An empty calibrator dict in hand-edited JSON collapses to None.
    assert CalibratorInfo.from_dict({}) is None
    assert CalibratorInfo.from_dict({"coefficients": []}) is None
    # Negative exponents are dropped at the JSON ingress too.
    assert CalibratorInfo.from_dict({"coefficients": [[2.0, -1]]}) is None


# ---- ADCS: the attitude control interface on both satellites -----------------


def test_imaging_sat_adcs_interface():
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    # Four packets, split by function like real ADCS ICDs.
    for name, apid in (("ADCS_STATUS", 0x18), ("ADCS_ATTITUDE", 0x19),
                       ("ADCS_WHEELS", 0x1A), ("ADCS_SENSORS", 0x1B)):
        assert d.packet_by_name(name).apid == apid
    # Quaternion: aggregate flattened to 4 calibrated int16 members.
    att = d.packet_by_name("ADCS_ATTITUDE")
    quat = [f for f in att.fields if f.name.startswith("ADCS_ATT_QUAT_Q")]
    assert len(quat) == 4
    for f in quat:
        assert f.python_type == "int16"  # raw counts on the wire
        assert abs(f.calibrator.apply(32767) - 1.0) < 1e-9  # full scale = 1.0
    # 4-wheel pyramid, each wheel speed calibrated at 0.2 RPM/count.
    wheels = d.packet_by_name("ADCS_WHEELS")
    speeds = [f for f in wheels.fields if f.name.endswith("_SPEED")]
    assert len(speeds) == 4
    assert speeds[0].calibrator.apply(5000) == 1000.0  # 5000 counts = 1000 RPM
    # Commands: mode enum carries real labels; wheel id is range-limited.
    slew = d.command_by_name("ADCS_SLEW_TO_QUATERNION")
    assert [p.name for p in slew.params] == ["Q1", "Q2", "Q3", "Q4"]
    assert all(p.valid_min == -1.0 and p.valid_max == 1.0 for p in slew.params)
    wheel = d.command_by_name("ADCS_WHEEL_DISABLE")
    assert wheel.params[0].valid_min == 1 and wheel.params[0].valid_max == 4
    mode = d.command_by_name("ADCS_SET_MODE")
    assert "TARGET_TRACK" in mode.params[0].enumerations


def test_my_vehicle_adcs_is_a_three_wheel_variant():
    d = SimDefinition.from_xtce(COMBINED_XTCE)
    wheels = d.packet_by_name("ADCS_WHEELS")
    speeds = [f for f in wheels.fields if f.name.endswith("_SPEED")]
    assert len(speeds) == 3  # deliberately different configuration
    wheel = d.command_by_name("ADCS_WHEEL_DISABLE")
    assert wheel.params[0].valid_max == 3
    att = d.packet_by_name("ADCS_ATTITUDE")
    assert sum(1 for f in att.fields if f.name.startswith("ADCS_ATT_QUAT_Q")) == 4


# ---- DefaultSignificance (command criticality) ------------------------------


def test_significance_parsed_and_carried():
    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    desat = simdef.command_by_name("ADCS_DESATURATE")
    assert desat.significance == "critical"
    assert "momentum" in desat.significance_reason.lower()
    assert desat.hazardous
    wheel_speed = simdef.command_by_name("ADCS_WHEEL_SET_SPEED")
    assert wheel_speed.significance == "vital"  # was 'caution': not legal XTCE
    noop = simdef.command_by_name("NOOP")
    assert noop.significance == "normal" and not noop.hazardous
    take = simdef.command_by_name("TAKE_IMAGE")
    assert take.significance is None and not take.hazardous


def test_significance_round_trips_through_json(tmp_path):
    from xtce_sim.generate import format_json

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    p = tmp_path / "cmd_tlm.json"
    p.write_text(format_json(simdef))
    again = SimDefinition.from_json(p)
    a = again.command_by_name("ADCS_DESATURATE")
    assert a.significance == "critical"
    assert a.significance_reason == simdef.command_by_name("ADCS_DESATURATE").significance_reason


def test_significance_marks_text_report():
    from xtce_sim.generate import format_text

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    text = format_text(simdef)
    assert "ADCS_DESATURATE (synthetic opcode)" not in text  # sanity: real opcode
    assert "[CRITICAL]" in text and "[VITAL]" in text
    # The declared reason rides along under the header.
    assert "! Attitude transients while momentum unloads" in text
