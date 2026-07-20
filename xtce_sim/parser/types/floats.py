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
    _parse_scalar_data_encoding,
    _parse_static_alarm_ranges,
    _parse_unit_set_enhanced,
    _parse_unit_text,
    _parse_valid_range,
)
from xtce_sim.parser.reader import ReaderMixin


def _parse_float_argument_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> FloatArgumentType:
    """Parse FloatArgumentType element."""
    name = reader._get_attr(elem, "name")

    # Defaults when no FloatDataEncoding is declared.
    size_in_bits = 32
    encoding = DataEncoding.IEEE754_1985

    enc = _parse_scalar_data_encoding(
        reader, elem, tag="FloatDataEncoding", default_bits="32", fallback=DataEncoding.IEEE754_1985
    )
    if enc is not None:
        size_in_bits, encoding, _ = enc

    unit = _parse_unit_text(reader, elem)
    valid_range = _parse_valid_range(reader, elem)

    return FloatArgumentType(
        name=name,
        size_in_bits=size_in_bits,
        encoding=encoding,
        unit=unit,
        valid_range=valid_range,
    )


def _parse_float_parameter_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> FloatParameterType:
    """Parse FloatParameterType element."""
    name = reader._get_attr(elem, "name")

    # Defaults when no encoding at all is declared.
    size_in_bits = 32
    encoding = DataEncoding.IEEE754_1985
    calibrator = None

    # Check for FloatDataEncoding (native float)
    enc = _parse_scalar_data_encoding(
        reader, elem, tag="FloatDataEncoding", default_bits="32", fallback=DataEncoding.IEEE754_1985
    )
    if enc is not None:
        size_in_bits, encoding, _ = enc

    # Check for IntegerDataEncoding (raw integer with calibration to float);
    # when both are declared, the integer encoding wins.
    enc = _parse_scalar_data_encoding(
        reader, elem, tag="IntegerDataEncoding", default_bits="16", fallback=DataEncoding.UNSIGNED
    )
    if enc is not None:
        size_in_bits, encoding, int_enc = enc
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
