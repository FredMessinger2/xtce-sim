"""The Aggregate type family: AggregateArgumentType and AggregateParameterType.

Aggregates are registered last on purpose — member type references are
resolved against the definition, so the referenced types must already
be parsed.
"""

import xml.etree.ElementTree as ET

from xtce_sim.models import AggregateArgumentType, AggregateParameterType, XTCEDefinition
from xtce_sim.parser.fields import (
    _parse_aggregate_members,
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
)
from xtce_sim.parser.reader import ReaderMixin


def _parse_aggregate_argument_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> AggregateArgumentType:
    """
    Parse AggregateArgumentType element.

    XTCE 1.3 aggregate types represent structured command data
    with multiple named members.
    """
    name = reader._get_attr(elem, "name")
    description = reader._get_attr(elem, "shortDescription")

    members = _parse_aggregate_members(reader, elem)

    # Calculate total size from member types if available
    size_in_bits = 0
    for member in members:
        member_type = definition.argument_types.get(member.type_ref)
        if member_type:
            size_in_bits += member_type.size_in_bits

    return AggregateArgumentType(
        name=name,
        size_in_bits=size_in_bits,
        description=description if description else None,
        members=members,
    )


def _parse_aggregate_parameter_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> AggregateParameterType:
    """
    Parse AggregateParameterType element.

    XTCE 1.3 aggregate types represent structured telemetry data
    with multiple named members (similar to C structs).
    """
    name = reader._get_attr(elem, "name")
    description = reader._get_attr(elem, "shortDescription")

    members = _parse_aggregate_members(reader, elem)

    # Calculate total size from member types if available
    size_in_bits = 0
    for member in members:
        member_type = definition.parameter_types.get(member.type_ref)
        if member_type:
            size_in_bits += member_type.size_in_bits

    # Parse alarm ranges (aggregates can have them too)
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)

    return AggregateParameterType(
        name=name,
        size_in_bits=size_in_bits,
        description=description if description else None,
        members=members,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
