"""The time type families: AbsoluteTime and RelativeTime, argument and parameter."""

import xml.etree.ElementTree as ET

from xtce_sim.models import (
    AbsoluteTimeArgumentType,
    AbsoluteTimeParameterType,
    RelativeTimeArgumentType,
    RelativeTimeParameterType,
    XTCEDefinition,
)
from xtce_sim.parser.fields import (
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
    _parse_unit_set_enhanced,
)


def _parse_absolute_time_argument_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> AbsoluteTimeArgumentType:
    """
    Parse AbsoluteTimeArgumentType element.

    XTCE AbsoluteTime defines timestamps with:
    - Encoding (how the raw value is stored)
    - ReferenceTime/Epoch (what the value is relative to)
    - Scale and Offset (for converting raw to seconds)

    Common patterns:
    - CCSDS CDS: 16-bit days + 32-bit milliseconds
    - CCSDS CUC: 32-bit or 48-bit seconds since epoch
    - Unix: 32-bit or 64-bit seconds since 1970
    """
    name = reader._get_attr(elem, "name")
    epoch = "UNIX"
    scale = 1.0
    offset = 0.0
    size_in_bits = 32
    reference_time_ref = None

    # Check for Encoding element
    encoding = reader._find(elem, "Encoding")
    if encoding is not None:
        # Get scale and offset from encoding
        scale_str = reader._get_attr(encoding, "scale", "1.0")
        offset_str = reader._get_attr(encoding, "offset", "0.0")
        scale = float(scale_str)
        offset = float(offset_str)

        # Check for IntegerDataEncoding inside encoding
        int_enc = reader._find(encoding, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Also check for direct IntegerDataEncoding (XTCE 1.2 style)
    int_enc = reader._find(elem, "IntegerDataEncoding")
    if int_enc is not None:
        size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Parse ReferenceTime to get epoch
    ref_time = reader._find(elem, "ReferenceTime")
    if ref_time is not None:
        # Check for Epoch element
        epoch_elem = reader._find(ref_time, "Epoch")
        if epoch_elem is not None and epoch_elem.text:
            epoch = epoch_elem.text
        # Check for OffsetFrom (relative to another time parameter)
        offset_from = reader._find(ref_time, "OffsetFrom")
        if offset_from is not None:
            reference_time_ref = reader._strip_path_ref(
                reader._get_attr(offset_from, "parameterRef")
            )

    return AbsoluteTimeArgumentType(
        name=name,
        size_in_bits=size_in_bits,
        epoch=epoch,
        scale=scale,
        offset=offset,
        reference_time_ref=reference_time_ref,
    )


def _parse_relative_time_argument_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> RelativeTimeArgumentType:
    """
    Parse RelativeTimeArgumentType element.

    RelativeTime represents durations/intervals rather than absolute timestamps.
    Typically encoded as scaled integers representing seconds or milliseconds.
    """
    name = reader._get_attr(elem, "name")
    scale = 1.0
    offset = 0.0
    size_in_bits = 32

    # Check for Encoding element
    encoding = reader._find(elem, "Encoding")
    if encoding is not None:
        scale_str = reader._get_attr(encoding, "scale", "1.0")
        offset_str = reader._get_attr(encoding, "offset", "0.0")
        scale = float(scale_str)
        offset = float(offset_str)

        int_enc = reader._find(encoding, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Also check for direct IntegerDataEncoding
    int_enc = reader._find(elem, "IntegerDataEncoding")
    if int_enc is not None:
        size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Parse unit information
    unit = None
    unit_set = reader._find(elem, "UnitSet")
    if unit_set is not None:
        unit_elem = reader._find(unit_set, "Unit")
        if unit_elem is not None and unit_elem.text:
            unit = unit_elem.text

    return RelativeTimeArgumentType(
        name=name, size_in_bits=size_in_bits, scale=scale, offset=offset, unit=unit
    )


def _parse_absolute_time_parameter_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> AbsoluteTimeParameterType:
    """
    Parse AbsoluteTimeParameterType element for telemetry.

    Used for packet timestamps, event times, and other absolute time values.
    """
    name = reader._get_attr(elem, "name")
    epoch = "UNIX"
    scale = 1.0
    offset = 0.0
    size_in_bits = 32
    reference_time_ref = None

    # Check for Encoding element
    encoding = reader._find(elem, "Encoding")
    if encoding is not None:
        scale_str = reader._get_attr(encoding, "scale", "1.0")
        offset_str = reader._get_attr(encoding, "offset", "0.0")
        scale = float(scale_str)
        offset = float(offset_str)

        int_enc = reader._find(encoding, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Also check for direct IntegerDataEncoding (XTCE 1.2 style)
    int_enc = reader._find(elem, "IntegerDataEncoding")
    if int_enc is not None:
        size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Parse ReferenceTime to get epoch
    ref_time = reader._find(elem, "ReferenceTime")
    if ref_time is not None:
        epoch_elem = reader._find(ref_time, "Epoch")
        if epoch_elem is not None and epoch_elem.text:
            epoch = epoch_elem.text
        offset_from = reader._find(ref_time, "OffsetFrom")
        if offset_from is not None:
            reference_time_ref = reader._strip_path_ref(
                reader._get_attr(offset_from, "parameterRef")
            )

    # Parse UnitSet with full metadata
    unit, unit_info = _parse_unit_set_enhanced(reader, elem)

    # Parse alarm ranges
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)

    return AbsoluteTimeParameterType(
        name=name,
        size_in_bits=size_in_bits,
        epoch=epoch,
        scale=scale,
        offset=offset,
        reference_time_ref=reference_time_ref,
        unit=unit,
        unit_info=unit_info,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )


def _parse_relative_time_parameter_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> RelativeTimeParameterType:
    """
    Parse RelativeTimeParameterType element for telemetry.

    Used for uptime counters, elapsed times, and duration values.
    """
    name = reader._get_attr(elem, "name")
    scale = 1.0
    offset = 0.0
    size_in_bits = 32

    # Check for Encoding element
    encoding = reader._find(elem, "Encoding")
    if encoding is not None:
        scale_str = reader._get_attr(encoding, "scale", "1.0")
        offset_str = reader._get_attr(encoding, "offset", "0.0")
        scale = float(scale_str)
        offset = float(offset_str)

        int_enc = reader._find(encoding, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Also check for direct IntegerDataEncoding
    int_enc = reader._find(elem, "IntegerDataEncoding")
    if int_enc is not None:
        size_in_bits = int(reader._get_attr(int_enc, "sizeInBits", "32"))

    # Parse UnitSet with full metadata
    unit, unit_info = _parse_unit_set_enhanced(reader, elem)

    # Parse alarm ranges
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)

    return RelativeTimeParameterType(
        name=name,
        size_in_bits=size_in_bits,
        scale=scale,
        offset=offset,
        unit=unit,
        unit_info=unit_info,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
