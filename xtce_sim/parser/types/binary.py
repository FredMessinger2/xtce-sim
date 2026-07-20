"""The Binary type family: BinaryArgumentType and BinaryParameterType."""

import xml.etree.ElementTree as ET

from xtce_sim.models import BinaryArgumentType, BinaryParameterType, XTCEDefinition
from xtce_sim.parser.fields import (
    _binary_size_in_bits,
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
)


def _parse_binary_argument_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> BinaryArgumentType:
    """Parse BinaryArgumentType element."""
    name = reader._get_attr(elem, "name")
    return BinaryArgumentType(name=name, size_in_bits=_binary_size_in_bits(reader, elem))


def _parse_binary_parameter_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> BinaryParameterType:
    """Parse BinaryParameterType element."""
    name = reader._get_attr(elem, "name")
    size_in_bits = _binary_size_in_bits(reader, elem)

    # Parse alarm ranges
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)

    return BinaryParameterType(
        name=name,
        size_in_bits=size_in_bits,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
