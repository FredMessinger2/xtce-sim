"""Edge-case coverage for generate.py builders and emitters."""

import pytest

from xtce_sim import models
from xtce_sim.definition import (
    CommandDef,
    FieldInfo,
    PacketDef,
    ParamInfo,
    SimDefinition,
)
from xtce_sim.generate import (
    GeneratorError,
    _fields_for_param,
    _param_info_for_arg,
    build_commands,
    build_packets,
    emit_python,
    extract_opcode,
    format_text,
    python_type_for_param,
)
from xtce_sim.models import DataEncoding


def _cmd_with_opcode(name, opcode_hex):
    container = models.CommandContainer(
        name=name + "C",
        entries=[
            models.ContainerEntry(
                entry_type="fixed", name="opcode", size_in_bits=8, binary_value=opcode_hex
            )
        ],
    )
    return models.MetaCommand(name=name, container=container)


def test_duplicate_real_opcode_rejected():
    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    xdef.meta_commands["A"] = _cmd_with_opcode("A", "10")
    xdef.meta_commands["B"] = _cmd_with_opcode("B", "10")
    with pytest.raises(GeneratorError, match="duplicate opcode"):
        build_commands(xdef)


def test_out_of_range_opcode_rejected():
    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    xdef.meta_commands["A"] = _cmd_with_opcode("A", "1FF")  # 0x1FF = 511 > 0xFF
    with pytest.raises(GeneratorError, match="outside 0x00"):
        build_commands(xdef)


def test_synthetic_opcode_exhaustion_raises():
    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    for i in range(65):  # 0xC0..0xFF is only 64 slots
        xdef.meta_commands[f"C{i}"] = models.MetaCommand(name=f"C{i}")
    with pytest.raises(GeneratorError, match="ran out of synthetic opcodes"):
        build_commands(xdef)


def test_duplicate_apid_rejected():
    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    xdef.containers["P1"] = models.SequenceContainer(
        name="P1", restriction_criteria={"CCSDS_APID": 100}
    )
    xdef.containers["P2"] = models.SequenceContainer(
        name="P2", restriction_criteria={"CCSDS_APID": 100}
    )
    with pytest.raises(GeneratorError, match="duplicate APID"):
        build_packets(xdef)


def test_reserved_echo_apid_rejected():
    from xtce_sim import ccsds

    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    xdef.containers["P1"] = models.SequenceContainer(
        name="P1", restriction_criteria={"CCSDS_APID": ccsds.CMD_ECHO_APID}
    )
    with pytest.raises(GeneratorError, match="reserved for the simulator's command echo"):
        build_packets(xdef)


def test_reserved_file_uplink_apid_rejected():
    from xtce_sim import ccsds

    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    xdef.containers["P1"] = models.SequenceContainer(
        name="P1", restriction_criteria={"CCSDS_APID": ccsds.FILE_UPLINK_APID}
    )
    with pytest.raises(GeneratorError, match="reserved for the simulator's file uplink"):
        build_packets(xdef)


def test_type_widths_and_signed_encodings():
    assert python_type_for_param(models.IntegerParameterType(name="x", size_in_bits=64)) == "uint64"
    assert (
        python_type_for_param(models.IntegerParameterType(name="x", size_in_bits=64, signed=True))
        == "int64"
    )
    # ones-complement / sign-magnitude are signed even without the flag.
    assert (
        python_type_for_param(
            models.IntegerParameterType(
                name="x", size_in_bits=16, encoding=DataEncoding.SIGN_MAGNITUDE
            )
        )
        == "int16"
    )
    # Boolean and time types get a real width, not the uint8 fallback.
    assert python_type_for_param(models.BooleanParameterType(name="b", size_in_bits=8)) == "uint8"
    assert (
        python_type_for_param(models.AbsoluteTimeParameterType(name="t", size_in_bits=32))
        == "uint32"
    )


def test_emit_python_sanitizes_exotic_names():
    sd = SimDefinition(
        space_system_name="S",
        commands=[
            CommandDef(name="class", opcode=1),  # reserved word
            CommandDef(name="Reset.System", opcode=2, description="line1\nline2 with \"q\""),
            CommandDef(name="1CMD", opcode=3),  # leading digit
            CommandDef(name="Reset_System", opcode=4),  # collides after sanitizing #2
            CommandDef(name="mro", opcode=5),  # Enum-reserved name
        ],
        packets=[PacketDef(name="9pkt", apid=1), PacketDef(name="_ID_", apid=2)],  # sunder
    )
    code = emit_python(sd)
    ns: dict = {}
    exec(compile(code, "generated.py", "exec"), ns)  # must compile & import cleanly
    assert len(list(ns["CommandOpcode"])) == 5  # all survived (no aliasing/collision)
    assert len(ns["COMMAND_PARAMS"]) == 5
    assert len(list(ns["TelemetryAPID"])) == 2
    # Pin the naming contract, not just cardinality.
    opcode = ns["CommandOpcode"]
    assert opcode["class_"] == 1  # keyword suffixed
    assert opcode["Reset_System"] == 2  # dots -> underscores
    assert opcode["_1CMD"] == 3  # leading digit prefixed
    assert opcode["Reset_System_2"] == 4  # collision suffixed
    assert opcode["mro_"] == 5  # Enum-reserved suffixed
    assert ns["TelemetryAPID"]["_ID__"] == 2  # sunder made safe


def test_extract_opcode_no_container():
    assert extract_opcode(models.MetaCommand(name="X")) is None


def test_extract_opcode_fallback_to_8bit_fixed():
    # No entry named 'opcode', but an 8-bit fixed non-CCSDS entry is the fallback.
    container = models.CommandContainer(
        name="c",
        entries=[
            models.ContainerEntry(entry_type="fixed", name="APID", size_in_bits=11,
                                  binary_value="00"),  # CCSDS header, skipped
            models.ContainerEntry(entry_type="fixed", name="Marker", size_in_bits=8,
                                  binary_value="AB"),
        ],
    )
    cmd = models.MetaCommand(name="X", container=container)
    assert extract_opcode(cmd) == 0xAB


def test_extract_opcode_from_argument_assignment():
    # The canonical XTCE idiom (BogusSAT-style): no FixedValueEntry at all,
    # OPCODE pinned on the base command via ArgumentAssignment.
    cmd = models.MetaCommand(name="X", argument_assignments={"OPCODE": "0x2A"})
    assert extract_opcode(cmd) == 0x2A  # 0x hex honored
    cmd = models.MetaCommand(name="X", argument_assignments={"OPCODE": "42"})
    assert extract_opcode(cmd) == 42  # argument values are decimal by convention


def test_extract_opcode_named_entry_beats_assignment():
    cmd = _cmd_with_opcode("X", "10")  # FixedValueEntry binaryValue (hex) -> 0x10
    cmd.argument_assignments = {"OPCODE": "99"}
    assert extract_opcode(cmd) == 0x10  # explicit layout entry wins


def test_extract_opcode_assignment_beats_8bit_heuristic():
    container = models.CommandContainer(
        name="c",
        entries=[
            models.ContainerEntry(entry_type="fixed", name="Marker", size_in_bits=8,
                                  binary_value="AB"),
        ],
    )
    cmd = models.MetaCommand(
        name="X", container=container, argument_assignments={"OPCODE": "0x11"}
    )
    assert extract_opcode(cmd) == 0x11  # declared assignment beats the guess


def test_extract_opcode_junk_assignment_falls_through():
    cmd = models.MetaCommand(name="X", argument_assignments={"OPCODE": "banana"})
    assert extract_opcode(cmd) is None  # -> synthetic later, no crash


def test_extract_opcode_zero_padded_decimal_assignment():
    # "010" is decimal ten (argument values are decimal unless 0x-prefixed),
    # not octal, and must not be silently skipped.
    cmd = models.MetaCommand(name="X", argument_assignments={"OPCODE": "010"})
    assert extract_opcode(cmd) == 10


def test_extract_opcode_junk_assignment_still_reaches_heuristic():
    # An unparseable assignment must not block the 8-bit fixed-entry fallback.
    container = models.CommandContainer(
        name="c",
        entries=[
            models.ContainerEntry(entry_type="fixed", name="Marker", size_in_bits=8,
                                  binary_value="AB"),
        ],
    )
    cmd = models.MetaCommand(
        name="X", container=container, argument_assignments={"OPCODE": "banana"}
    )
    assert extract_opcode(cmd) == 0xAB


def test_extract_opcode_assignment_inherited_from_base_chain():
    # A grandparent may pin OPCODE; the full inheritance chain is consulted.
    base = models.MetaCommand(name="Base", argument_assignments={"OPCODE": "0x33"})
    child = models.MetaCommand(name="Child")
    child.base_command = base
    assert extract_opcode(child) == 0x33
    # ...and a child override wins.
    child.argument_assignments = {"OPCODE": "0x44"}
    assert extract_opcode(child) == 0x44


def test_extract_opcode_prefers_named_entry():
    # An entry named 'opcode' is preferred over a later 8-bit fixed entry.
    container = models.CommandContainer(
        name="c",
        entries=[
            models.ContainerEntry(entry_type="fixed", name="OPCODE", size_in_bits=8,
                                  binary_value="7F"),
            models.ContainerEntry(entry_type="fixed", name="Marker", size_in_bits=8,
                                  binary_value="AB"),
        ],
    )
    cmd = models.MetaCommand(name="X", container=container)
    assert extract_opcode(cmd) == 0x7F


def test_extract_opcode_skips_non_hex_binary_value():
    # A non-hex fixed value is tolerated (no crash) and yields None.
    container = models.CommandContainer(
        name="c",
        entries=[
            models.ContainerEntry(entry_type="fixed", name="Marker", size_in_bits=8,
                                  binary_value="ZZ"),  # not valid hex
        ],
    )
    cmd = models.MetaCommand(name="X", container=container)
    assert extract_opcode(cmd) is None


def test_param_info_for_arg_unresolved_type():
    arg = models.Argument(name="Mystery", argument_type_ref="Gone", argument_type=None)
    info = _param_info_for_arg(arg)
    assert info.name == "Mystery" and info.python_type == "uint8" and info.size_bits == 8


def test_fields_for_param_aggregate_missing_member_type():
    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    agg = models.AggregateParameterType(
        name="AggT", members=[models.AggregateMember("m", "DoesNotExist")]
    )
    param = models.Parameter(name="Agg", parameter_type_ref="AggT", parameter_type=agg)
    fields = _fields_for_param(param, xdef)
    assert fields[0].name == "Agg_m"
    assert fields[0].python_type == "uint32"  # unknown member type -> fallback


def test_fields_for_param_unresolved_type():
    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    param = models.Parameter(name="P", parameter_type_ref="Gone", parameter_type=None)
    fields = _fields_for_param(param, xdef)
    assert fields[0].python_type == "uint32"


def test_format_text_empty_command_and_packet():
    simdef = SimDefinition(
        space_system_name="Empty",
        commands=[CommandDef(name="BARE", opcode=0, synthetic=True)],
        packets=[PacketDef(name="VOID", apid=1)],
    )
    text = format_text(simdef)
    assert "(no parameters)" in text
    assert "(no fields)" in text
    assert "synthetic opcode" in text


def test_emit_python_param_and_field_variants():
    simdef = SimDefinition(
        space_system_name="S",
        commands=[
            CommandDef(name="EMPTY", opcode=1, params=[]),
            CommandDef(
                name="RICH",
                opcode=2,
                params=[
                    ParamInfo(
                        "A", 8, "uint8", unit="V", description="amps",
                        valid_min=0, valid_max=10, enumerations={"OFF": 0},
                    ),
                    ParamInfo("B", 16, "uint16"),
                ],
            ),
        ],
        packets=[
            PacketDef(name="EMPTYPKT", apid=1, fields=[]),
            PacketDef(
                name="P",
                apid=2,
                fields=[
                    FieldInfo("F1", 16, "uint16", unit="V", description="volts"),
                    FieldInfo("F2", 8, "uint8"),
                ],
                struct_format=">HB",
            ),
        ],
    )
    code = emit_python(simdef)
    # Compiles and defines the expected symbols.
    ns: dict = {}
    exec(compile(code, "generated.py", "exec"), ns)
    assert ns["CommandOpcode"].RICH == 2
    assert ns["TelemetryAPID"].P == 2
    assert ns["COMMAND_PARAMS"][ns["CommandOpcode"].EMPTY] == []
    assert ns["PACKET_FORMATS"][ns["TelemetryAPID"].P] == ">HB"


def test_emit_python_empty_definition():
    simdef = SimDefinition(space_system_name="Nothing")
    code = emit_python(simdef)
    ns: dict = {}
    exec(compile(code, "generated.py", "exec"), ns)
    assert list(ns["CommandOpcode"]) == []
    assert list(ns["TelemetryAPID"]) == []


def test_significance_inherits_up_the_base_chain():
    from xtce_sim import models as m

    base = m.MetaCommand(
        name="BASE", abstract=True, significance="critical", significance_reason="why"
    )
    inherits = m.MetaCommand(name="CHILD", base_meta_command_ref="BASE", base_command=base)
    overrides = m.MetaCommand(
        name="TAME", base_meta_command_ref="BASE", base_command=base, significance="normal"
    )
    xdef = models.XTCEDefinition(space_system_name="S", namespace="")
    xdef.meta_commands = {"BASE": base, "CHILD": inherits, "TAME": overrides}
    cmds = {c.name: c for c in build_commands(xdef)}
    assert "BASE" not in cmds  # abstract: not dispatchable
    assert cmds["CHILD"].significance == "critical"
    assert cmds["CHILD"].significance_reason == "why"
    assert cmds["CHILD"].hazardous
    # An explicit declaration on the derived command wins over the base's.
    assert cmds["TAME"].significance == "normal" and not cmds["TAME"].hazardous
