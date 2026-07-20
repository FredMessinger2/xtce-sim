"""The Float type family: FloatArgumentType and FloatParameterType."""

import xml.etree.ElementTree as ET

from xtce_sim.models import (
    DataEncoding,
    FloatArgumentType,
    FloatParameterType,
    XTCEDefinition,
)
from xtce_sim.parser.fields import (
    _parse_calibrator,
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
    _parse_unit_set_enhanced,
    _parse_valid_range,
)


def _parse_float_argument_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> FloatArgumentType:
    """Parse FloatArgumentType element."""
    name = reader._get_attr(elem, "name")

    # Default values
    size_in_bits = 32
    encoding = DataEncoding.IEEE754_1985
    unit = None
    valid_range = None

    # Parse FloatDataEncoding
    data_enc = reader._find(elem, "FloatDataEncoding")
    if data_enc is not None:
        size_in_bits = int(reader._get_attr(data_enc, "sizeInBits", "32"))
        enc_str = reader._get_attr(data_enc, "encoding", "IEEE754_1985")
        try:
            encoding = DataEncoding(enc_str)
        except ValueError:
            encoding = DataEncoding.IEEE754_1985

    # Parse UnitSet
    unit_set = reader._find(elem, "UnitSet")
    if unit_set is not None:
        unit_elem = reader._find(unit_set, "Unit")
        if unit_elem is not None and unit_elem.text:
            unit = unit_elem.text

    # Parse ValidRange
    valid_range = _parse_valid_range(reader, elem)

    return FloatArgumentType(
        name=name,
        size_in_bits=size_in_bits,
        encoding=encoding,
        unit=unit,
        valid_range=valid_range,
    )


def _parse_float_parameter_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> FloatParameterType:
    """Parse FloatParameterType element."""
    name = reader._get_attr(elem, "name")

    size_in_bits = 32
    encoding = DataEncoding.IEEE754_1985
    calibrator = None

    # Check for FloatDataEncoding (native float)
    float_enc = reader._find(elem, "FloatDataEncoding")
    if float_enc is not None:
        size_in_bits = int(reader._get_attr(float_enc, "sizeInBits", "32"))
        enc_str = reader._get_attr(float_enc, "encoding", "IEEE754_1985")
        try:
            encoding = DataEncoding(enc_str)
        except ValueError:
            encoding = DataEncoding.IEEE754_1985

    # Check for IntegerDataEncoding (raw integer with calibration to float)
    int_enc = reader._find(elem, "IntegerDataEncoding")
    if int_enc is not None:
        size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "16"))
        enc_str = reader._get_attr(int_enc, "encoding", "unsigned")
        try:
            encoding = DataEncoding(enc_str)
        except ValueError:
            encoding = DataEncoding.UNSIGNED
        calibrator = _parse_calibrator(reader, int_enc)

    # Parse UnitSet with full metadata
    unit, unit_info = _parse_unit_set_enhanced(reader, elem)

    # Parse ValidRange
    valid_range = _parse_valid_range(reader, elem)

    # Parse alarm ranges
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)

    # Parse context-dependent alarms
    context_alarms = _parse_context_alarm_list(reader, elem)

    return FloatParameterType(
        name=name,
        size_in_bits=size_in_bits,
        encoding=encoding,
        unit=unit,
        unit_info=unit_info,
        valid_range=valid_range,
        calibrator=calibrator,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
