"""The Integer type family: IntegerArgumentType and IntegerParameterType."""

import xml.etree.ElementTree as ET

from xtce_sim.models import (
    DataEncoding,
    IntegerArgumentType,
    IntegerParameterType,
    XTCEDefinition,
)
from xtce_sim.parser.fields import (
    _parse_calibrator,
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
    _parse_unit_set_enhanced,
    _parse_valid_range,
)


def _parse_integer_argument_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> IntegerArgumentType:
    """Parse IntegerArgumentType element."""
    name = reader._get_attr(elem, "name")
    signed = reader._get_attr(elem, "signed", "false").lower() == "true"

    # Default values
    size_in_bits = 32
    encoding = DataEncoding.UNSIGNED
    unit = None
    valid_range = None

    # Parse IntegerDataEncoding
    data_enc = reader._find(elem, "IntegerDataEncoding")
    if data_enc is not None:
        size_in_bits = int(reader._get_attr(data_enc, "sizeInBits", "8"))
        enc_str = reader._get_attr(data_enc, "encoding", "unsigned")
        try:
            encoding = DataEncoding(enc_str)
        except ValueError:
            encoding = DataEncoding.UNSIGNED

    # Parse UnitSet
    unit_set = reader._find(elem, "UnitSet")
    if unit_set is not None:
        unit_elem = reader._find(unit_set, "Unit")
        if unit_elem is not None and unit_elem.text:
            unit = unit_elem.text

    # Parse ValidRange
    valid_range = _parse_valid_range(reader, elem)

    return IntegerArgumentType(
        name=name,
        size_in_bits=size_in_bits,
        encoding=encoding,
        signed=signed,
        unit=unit,
        valid_range=valid_range,
    )


def _parse_integer_parameter_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> IntegerParameterType:
    """Parse IntegerParameterType element."""
    name = reader._get_attr(elem, "name")
    signed = reader._get_attr(elem, "signed", "false").lower() == "true"

    size_in_bits = 32
    encoding = DataEncoding.UNSIGNED
    calibrator = None

    # Parse IntegerDataEncoding
    data_enc = reader._find(elem, "IntegerDataEncoding")
    if data_enc is not None:
        size_in_bits = int(reader._get_attr(data_enc, "sizeInBits", "8"))
        enc_str = reader._get_attr(data_enc, "encoding", "unsigned")
        try:
            encoding = DataEncoding(enc_str)
        except ValueError:
            encoding = DataEncoding.UNSIGNED
        calibrator = _parse_calibrator(reader, data_enc)

    # Parse UnitSet with full metadata
    unit, unit_info = _parse_unit_set_enhanced(reader, elem)

    # Parse ValidRange
    valid_range = _parse_valid_range(reader, elem)

    # Parse alarm ranges (XTCE 1.2+)
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)

    # Parse context-dependent alarms
    context_alarms = _parse_context_alarm_list(reader, elem)

    return IntegerParameterType(
        name=name,
        size_in_bits=size_in_bits,
        encoding=encoding,
        signed=signed or encoding == DataEncoding.TWOS_COMPLEMENT,
        unit=unit,
        unit_info=unit_info,
        valid_range=valid_range,
        calibrator=calibrator,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
