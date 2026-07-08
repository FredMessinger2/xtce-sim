"""Tests for building and dumping a SimDefinition from the example XTCE."""

import importlib.util
import json
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim.cli import main
from xtce_sim.definition import SimDefinition
from xtce_sim.generate import fields_to_struct_format, format_json, format_text

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CMD_XML = EXAMPLES / "my_vehicle_commands.xml"
TLM_XML = EXAMPLES / "my_vehicle_telemetry.xml"
COMBINED_XTCE = EXAMPLES / "my_vehicle.xml"
IMAGING_SAT_XTCE = EXAMPLES / "imaging_sat.xml"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce([CMD_XML, TLM_XML])


def test_builds_commands_and_packets(simdef: SimDefinition):
    assert simdef.space_system_name == "MyVehicle"
    assert len(simdef.commands) == 55
    assert len(simdef.packets) == 14


def test_imaging_sat_example_builds_and_is_generic():
    # The second example (an imaging satellite) parses and builds, and its
    # source vendor branding was scrubbed when it was brought into the repo.
    d = SimDefinition.from_xtce(IMAGING_SAT_XTCE)
    assert d.space_system_name == "ImagingSat"
    assert len(d.commands) == 30
    assert len(d.packets) == 8
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
    assert len(set(opcodes.values())) == len(opcodes)  # all distinct
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


def test_aggregate_member_enum():
    # An aggregate member whose type is enumerated carries the map too.
    ff = SimDefinition.from_xtce(
        Path(__file__).resolve().parent / "data" / "full_features.xml"
    )
    fix = next(
        f for p in ff.packets for f in p.fields if f.name.endswith("_Fix")
    )
    assert fix.enumerations  # GPS aggregate's Fix member is ModeType (enum)


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
    """examples/my_vehicle.xml (single combined file) must stay equivalent to
    the split command/telemetry pair — it's the headline example."""
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


def test_apids_are_unique(simdef: SimDefinition):
    apids = [p.apid for p in simdef.packets]
    assert len(apids) == len(set(apids))


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
    assert len(data["commands"]) == 55
    assert len(data["telemetry"]) == 14


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
    assert len(list(mod.CommandOpcode)) == 55
    assert len(list(mod.TelemetryAPID)) == 14
    assert len(mod.COMMAND_PARAMS) == 55
    for apid, fmt in mod.PACKET_FORMATS.items():
        struct.calcsize(fmt)  # must not raise
