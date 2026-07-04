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
