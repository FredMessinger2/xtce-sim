"""The Boolean type family: BooleanArgumentType and BooleanParameterType."""

import xml.etree.ElementTree as ET

from xtce_sim.models import BooleanArgumentType, BooleanParameterType, XTCEDefinition
from xtce_sim.parser.fields import (
    _parse_boolean_fields,
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
)
from xtce_sim.parser.reader import ReaderMixin


def _parse_boolean_argument_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> BooleanArgumentType:
    """Parse BooleanArgumentType element."""
    name, zero_str, one_str, initial_value, size_in_bits = _parse_boolean_fields(reader, elem)
    return BooleanArgumentType(
        name=name,
        size_in_bits=size_in_bits,
        zero_string_value=zero_str,
        one_string_value=one_str,
        initial_value=initial_value,
    )


def _parse_boolean_parameter_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> BooleanParameterType:
    """Parse BooleanParameterType element for telemetry (boolean fields + alarms)."""
    name, zero_str, one_str, initial_value, size_in_bits = _parse_boolean_fields(reader, elem)
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)
    return BooleanParameterType(
        name=name,
        size_in_bits=size_in_bits,
        zero_string_value=zero_str,
        one_string_value=one_str,
        initial_value=initial_value,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
