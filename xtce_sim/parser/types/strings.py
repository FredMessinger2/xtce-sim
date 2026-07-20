"""The String type family: StringArgumentType and StringParameterType."""

import xml.etree.ElementTree as ET

from xtce_sim.models import StringArgumentType, StringParameterType, XTCEDefinition
from xtce_sim.parser.fields import (
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
    _string_size_and_length,
)


def _parse_string_argument_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> StringArgumentType:
    """Parse StringArgumentType element."""
    name = reader._get_attr(elem, "name")
    size_in_bits, max_length = _string_size_and_length(reader, elem)
    return StringArgumentType(name=name, size_in_bits=size_in_bits, max_length=max_length)


def _parse_string_parameter_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> StringParameterType:
    """Parse StringParameterType element."""
    name = reader._get_attr(elem, "name")
    size_in_bits, max_length = _string_size_and_length(reader, elem)
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)
    return StringParameterType(
        name=name,
        size_in_bits=size_in_bits,
        max_length=max_length,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
