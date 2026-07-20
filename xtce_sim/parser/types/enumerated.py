"""The Enumerated type family: EnumeratedArgumentType and EnumeratedParameterType."""

import logging
import xml.etree.ElementTree as ET

from xtce_sim.models import (
    EnumeratedArgumentType,
    EnumeratedParameterType,
    EnumerationValue,
    XTCEDefinition,
)
from xtce_sim.parser.fields import (
    _parse_context_alarm_list,
    _parse_static_alarm_ranges,
)

logger = logging.getLogger("xtce_sim.parser")


def _parse_enumerated_argument_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> EnumeratedArgumentType:
    """Parse EnumeratedArgumentType element."""
    name = reader._get_attr(elem, "name")

    enumerations = []
    enum_list = reader._find(elem, "EnumerationList")
    if enum_list is not None:
        for enum_elem in reader._findall(enum_list, "Enumeration"):
            label = reader._get_attr(enum_elem, "label")
            value = int(reader._get_attr(enum_elem, "value", "0"))
            enumerations.append(EnumerationValue(label=label, value=value))

    # Get size from IntegerDataEncoding if present
    data_enc = reader._find(elem, "IntegerDataEncoding")
    if data_enc is not None:
        size_in_bits = int(reader._get_attr(data_enc, "sizeInBits", "8"))
    else:
        # Determine size based on max value
        max_val = max((e.value for e in enumerations), default=0)
        if max_val <= 255:
            size_in_bits = 8
        elif max_val <= 65535:
            size_in_bits = 16
        else:
            size_in_bits = 32
        logger.info(
            "~ enum %r: no IntegerDataEncoding — inferred %d bits from max enumeration value %d",
            name,
            size_in_bits,
            max_val,
        )

    return EnumeratedArgumentType(name=name, size_in_bits=size_in_bits, enumerations=enumerations)


def _parse_enumerated_parameter_type(
    reader, elem: ET.Element, definition: XTCEDefinition
) -> EnumeratedParameterType:
    """Parse EnumeratedParameterType element."""
    name = reader._get_attr(elem, "name")

    enumerations = []
    enum_list = reader._find(elem, "EnumerationList")
    if enum_list is not None:
        for enum_elem in reader._findall(enum_list, "Enumeration"):
            label = reader._get_attr(enum_elem, "label")
            value = int(reader._get_attr(enum_elem, "value", "0"))
            enumerations.append(EnumerationValue(label=label, value=value))

    # Get size from IntegerDataEncoding
    size_in_bits = 8
    data_enc = reader._find(elem, "IntegerDataEncoding")
    if data_enc is not None:
        size_in_bits = int(reader._get_attr(data_enc, "sizeInBits", "8"))

    # Parse alarm ranges (enumerations can have alarms on specific values)
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)

    return EnumeratedParameterType(
        name=name,
        size_in_bits=size_in_bits,
        enumerations=enumerations,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
