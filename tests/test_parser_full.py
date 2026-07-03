"""Parser breadth coverage via the full-features fixture.

Exercises every parameter/argument type kind, encodings, calibrators, alarms
(static + context), inheritance, arrays, time types, aggregates, ancillary
data, restriction criteria, path-qualified refs, and nested SpaceSystems.
"""

from pathlib import Path

import pytest

from xtce_sim import models
from xtce_sim.definition import SimDefinition
from xtce_sim.generate import build_sim_definition, format_text
from xtce_sim.parser import XTCEParser

DATA = Path(__file__).resolve().parent / "data"
FULL = DATA / "full_features.xml"
NS = 'xmlns:xtce="http://www.omg.org/spec/XTCE/20250214"'


@pytest.fixture(scope="module")
def defn():
    return XTCEParser().parse(FULL)


@pytest.mark.parametrize(
    "encoding_xml, expected",
    [
        # Correct XTCE binary form: SizeInBits/FixedValue (no Fixed wrapper).
        (
            "<xtce:BinaryDataEncoding><xtce:SizeInBits>"
            "<xtce:FixedValue>2048</xtce:FixedValue>"
            "</xtce:SizeInBits></xtce:BinaryDataEncoding>",
            2048,
        ),
        # Lenient fallback: the Fixed/FixedValue wrapper (as StringDataEncoding uses).
        (
            "<xtce:BinaryDataEncoding><xtce:SizeInBits><xtce:Fixed>"
            "<xtce:FixedValue>128</xtce:FixedValue>"
            "</xtce:Fixed></xtce:SizeInBits></xtce:BinaryDataEncoding>",
            128,
        ),
        # No size declared -> 0.
        ("", 0),
        # Malformed FixedValue text degrades to 0 instead of crashing the parse.
        (
            "<xtce:BinaryDataEncoding><xtce:SizeInBits>"
            "<xtce:FixedValue>   </xtce:FixedValue>"
            "</xtce:SizeInBits></xtce:BinaryDataEncoding>",
            0,
        ),
    ],
)
def test_binary_parameter_size_forms(tmp_path, encoding_xml, expected):
    doc = (
        f'<xtce:SpaceSystem {NS} name="S"><xtce:TelemetryMetaData><xtce:ParameterTypeSet>'
        f'<xtce:BinaryParameterType name="B">{encoding_xml}</xtce:BinaryParameterType>'
        "</xtce:ParameterTypeSet></xtce:TelemetryMetaData></xtce:SpaceSystem>"
    )
    f = tmp_path / "b.xml"
    f.write_text(doc)
    defn = XTCEParser().parse(f)
    assert defn.parameter_types["B"].size_in_bits == expected


def test_binary_argument_size_from_encoding_and_attribute(tmp_path):
    doc = (
        f'<xtce:SpaceSystem {NS} name="S"><xtce:CommandMetaData><xtce:ArgumentTypeSet>'
        '<xtce:BinaryArgumentType name="Enc"><xtce:BinaryDataEncoding><xtce:SizeInBits>'
        "<xtce:FixedValue>256</xtce:FixedValue>"
        "</xtce:SizeInBits></xtce:BinaryDataEncoding></xtce:BinaryArgumentType>"
        '<xtce:BinaryArgumentType name="Attr" sizeInBits="64"/>'
        "</xtce:ArgumentTypeSet></xtce:CommandMetaData></xtce:SpaceSystem>"
    )
    f = tmp_path / "a.xml"
    f.write_text(doc)
    defn = XTCEParser().parse(f)
    assert defn.argument_types["Enc"].size_in_bits == 256  # from BinaryDataEncoding
    assert defn.argument_types["Attr"].size_in_bits == 64  # legacy attribute


def test_space_system_and_nested_flattened(defn):
    assert defn.space_system_name == "FullFeatures"
    # Nested SpaceSystem's parameter type/param are flattened into the same def.
    assert "SubType" in defn.parameter_types
    assert "SubValue" in defn.parameters


def test_parameter_type_kinds(defn):
    pt = defn.parameter_types
    assert isinstance(pt["TempType"], models.IntegerParameterType)
    assert isinstance(pt["VoltageType"], models.FloatParameterType)
    assert isinstance(pt["ModeType"], models.EnumeratedParameterType)
    assert isinstance(pt["LabelType"], models.StringParameterType)
    assert isinstance(pt["BlobType"], models.BinaryParameterType)
    assert isinstance(pt["FlagType"], models.BooleanParameterType)
    assert isinstance(pt["SamplesType"], models.ArrayParameterType)
    assert isinstance(pt["PktTimeType"], models.AbsoluteTimeParameterType)
    assert isinstance(pt["ElapsedType"], models.RelativeTimeParameterType)
    assert isinstance(pt["GPSType"], models.AggregateParameterType)


def test_calibrator_and_encoding(defn):
    temp = defn.parameter_types["TempType"]
    assert temp.signed is True
    assert temp.encoding == models.DataEncoding.TWOS_COMPLEMENT
    assert temp.calibrator is not None
    assert (-40.0, 0) in temp.calibrator.coefficients
    # Calibrated float uses an integer wire encoding.
    assert defn.parameter_types["PressureType"].calibrator is not None


def test_unit_metadata(defn):
    temp = defn.parameter_types["TempType"]
    assert temp.unit == "degC"
    assert temp.unit_info is not None
    assert temp.unit_info.description == "Celsius"
    assert temp.unit_info.power == 1


def test_alarms_static_and_context(defn):
    temp = defn.parameter_types["TempType"]
    assert temp.alarm_ranges is not None
    assert temp.alarm_ranges.warning_range.min_inclusive == -20
    assert temp.alarm_ranges.distress_range.min_exclusive == -35
    assert temp.alarm_ranges.critical_range.max_inclusive == 85
    assert len(temp.context_alarms) == 1
    ctx = temp.context_alarms[0]
    assert ctx.context_match.parameter_ref == "ModeParam"  # path-qualified ref stripped
    assert ctx.context_match.value == "SAFE"


def test_valid_range_and_enum(defn):
    v = defn.parameter_types["VoltageType"]
    assert v.valid_range.max_inclusive == 12
    mode = defn.parameter_types["ModeType"]
    labels = {e.label: e.value for e in mode.enumerations}
    assert labels == {"NOMINAL": 0, "SAFE": 1, "FAULT": 2}


def test_string_binary_boolean_sizes(defn):
    assert defn.parameter_types["LabelType"].size_in_bits == 64
    assert defn.parameter_types["BlobType"].size_in_bits == 32
    flag = defn.parameter_types["FlagType"]
    assert flag.zero_string_value == "OFF"
    assert flag.one_string_value == "ON"
    assert flag.initial_value is False


def test_array_fixed_and_dynamic(defn):
    fixed = defn.parameter_types["SamplesType"]
    assert fixed.get_total_elements() == 4  # 0..3 inclusive
    assert fixed.size_in_bits == 4 * 32
    dyn = defn.parameter_types["DynSamplesType"]
    assert dyn.get_total_elements() is None  # dynamic dimension
    assert dyn.dimensions[0][1] is True


def test_time_types(defn):
    pkt = defn.parameter_types["PktTimeType"]
    assert pkt.epoch == "1970-01-01T00:00:00"
    assert pkt.reference_time_ref == "Counter"
    assert pkt.unit == "s"
    elapsed = defn.parameter_types["ElapsedType"]
    assert elapsed.scale == 0.001


def test_aggregate_members(defn):
    gps = defn.parameter_types["GPSType"]
    assert [m.name for m in gps.members] == ["Lat", "Lon", "Fix"]
    assert gps.get_member("Lat").description == "Latitude"
    assert gps.get_member("missing") is None
    # size = sum of member type sizes (Voltage 32 + Voltage 32 + Mode 8)
    assert gps.get_total_size(defn.parameter_types) == 72


def test_containers_restriction_criteria(defn):
    health = defn.containers["HEALTH"]
    assert health.restriction_criteria["CCSDS_APID"] == 100
    assert health.restriction_criteria["SecHdrFlag"] == "0"
    assert health.base_container_ref == "CCSDSPacket"
    counts = defn.containers["COUNTS"]
    assert counts.restriction_criteria["CCSDS_APID"] == 101
    packets = defn.get_telemetry_packets()
    assert {c.name for c in packets} == {"HEALTH", "COUNTS"}


def test_command_inheritance_and_opcode(defn):
    do_thing = defn.meta_commands["DO_THING"]
    assert do_thing.base_command is defn.meta_commands["BaseCmd"]
    # OPCODE is inherited + assigned -> not a user argument; user args are the rest.
    user = [a.name for a in do_thing.get_user_arguments()]
    assert "OPCODE" not in user
    assert set(user) == {"Duration", "State", "Gain"}
    # Command + argument ancillary data parsed.
    assert "tlm_side_effect" in do_thing.ancillary_data
    assert "Counter=1;ModeParam=SAFE" == do_thing.ancillary_data["tlm_side_effect"]
    dur = next(a for a in do_thing.arguments if a.name == "Duration")
    assert dur.ancillary_data["tlm_field"] == "Counter"


def test_argument_type_kinds(defn):
    at = defn.argument_types
    assert isinstance(at["U8Arg"], models.IntegerArgumentType)
    assert isinstance(at["F32Arg"], models.FloatArgumentType)
    assert isinstance(at["StateArg"], models.EnumeratedArgumentType)
    assert isinstance(at["BigEnumArg"], models.EnumeratedArgumentType)
    assert isinstance(at["StrArg"], models.StringArgumentType)
    assert isinstance(at["BinArg"], models.BinaryArgumentType)
    assert isinstance(at["BoolArg"], models.BooleanArgumentType)
    assert isinstance(at["ArrArg"], models.ArrayArgumentType)
    assert isinstance(at["AtArg"], models.AbsoluteTimeArgumentType)
    assert isinstance(at["RtArg"], models.RelativeTimeArgumentType)
    assert isinstance(at["VecArg"], models.AggregateArgumentType)
    # Enum without explicit encoding sizes itself from the max value (300 -> 16 bits).
    assert at["BigEnumArg"].size_in_bits == 16
    # Enum helper methods.
    assert at["StateArg"].get_value("ON") == 1
    assert at["StateArg"].get_label(0) == "OFF"
    assert at["StateArg"].get_value("MISSING") is None


def test_build_sim_definition_flattens_aggregate(defn):
    simdef = build_sim_definition(defn)
    health = next(p for p in simdef.packets if p.name == "HEALTH")
    field_names = [f.name for f in health.fields]
    # Aggregate GPS flattened into prefixed member fields.
    assert "GPS_Lat" in field_names
    assert "GPS_Fix" in field_names
    # Binary field -> bytes, string field -> string.
    types = {f.name: f.python_type for f in health.fields}
    assert types["Blob"] == "bytes"
    assert types["Label"] == "string"
    # A synthetic opcode was assigned to the command lacking one.
    assert any(c.synthetic for c in simdef.commands)
    assert format_text(simdef)  # renders without error


def test_parse_multiple_merges(tmp_path):
    """parse_multiple merges files and re-resolves references."""
    parser = XTCEParser()
    merged = parser.parse_multiple([FULL, FULL])  # merging with itself is idempotent
    assert merged.space_system_name == "FullFeatures"
    assert "TempType" in merged.parameter_types
    # from_xtce with a list routes through parse_multiple.
    sd = SimDefinition.from_xtce([FULL, FULL])
    assert sd.space_system_name == "FullFeatures"
