"""The Array type family: ArrayArgumentType and ArrayParameterType.

Arrays are registered after the scalar families on purpose — the element
type reference is resolved against the definition, so the referenced
types must already be parsed.
"""

import xml.etree.ElementTree as ET
from typing import Optional

from xtce_sim.models import ArrayArgumentType, ArrayParameterType, XTCEDefinition
from xtce_sim.parser.fields import (
    _parse_context_alarm_list,
    _parse_dimension,
    _parse_static_alarm_ranges,
)
from xtce_sim.parser.reader import ReaderMixin

# One dimension: (size, is_dynamic, dynamic_ref), as _parse_dimension returns.
_Dimension = tuple[int, bool, Optional[str]]


def _parse_array_fields(
    reader: ReaderMixin, elem: ET.Element, resolving_types: dict
) -> tuple[str, object, list[_Dimension], int]:
    """Shared Array parsing: type ref, element type, dimensions, total size.

    ``resolving_types`` is the definition dict the ``arrayTypeRef`` is
    resolved against — ``definition.argument_types`` for the argument
    flavor, ``definition.parameter_types`` for the parameter flavor. This
    parse-time resolution is why arrays register after the scalar
    families. The total size is ``product(dimensions) * element size``
    when the element type resolved and every dimension is fixed; 0
    otherwise (unresolved element type or any dynamic dimension).
    """
    array_type_ref = reader._strip_path_ref(reader._get_attr(elem, "arrayTypeRef", ""))
    element_type = resolving_types.get(array_type_ref)

    dimensions: list[_Dimension] = []
    dim_list = reader._find(elem, "DimensionList")
    if dim_list is not None:
        for dim in reader._findall(dim_list, "Dimension"):
            dimensions.append(_parse_dimension(reader, dim))

    size_in_bits = 0
    if element_type:
        total_elements = 1
        all_fixed = True
        for size, is_dynamic, _ in dimensions:
            if is_dynamic:
                all_fixed = False
                break
            total_elements *= size
        if all_fixed:
            size_in_bits = total_elements * element_type.size_in_bits

    return array_type_ref, element_type, dimensions, size_in_bits


def _parse_array_argument_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> ArrayArgumentType:
    """
    Parse ArrayArgumentType element.

    XTCE Arrays define:
    - ArrayTypeRef: Reference to the element type
    - DimensionList: One or more dimensions with fixed or dynamic sizes

    Example XTCE:
    <ArrayArgumentType name="DataBuffer" arrayTypeRef="Uint8Type">
      <DimensionList>
        <Dimension>
          <StartingIndex><FixedValue>0</FixedValue></StartingIndex>
          <EndingIndex><FixedValue>255</FixedValue></EndingIndex>
        </Dimension>
      </DimensionList>
    </ArrayArgumentType>
    """
    name = reader._get_attr(elem, "name")
    array_type_ref, element_type, dimensions, size_in_bits = _parse_array_fields(
        reader, elem, definition.argument_types
    )

    return ArrayArgumentType(
        name=name,
        size_in_bits=size_in_bits,
        array_type_ref=array_type_ref,
        element_type=element_type,
        dimensions=dimensions,
    )


def _parse_array_parameter_type(
    reader: ReaderMixin, elem: ET.Element, definition: XTCEDefinition
) -> ArrayParameterType:
    """
    Parse ArrayParameterType element for telemetry.

    Used for vector telemetry data like multi-channel sensors,
    memory dumps, or other repeated data structures.
    """
    name = reader._get_attr(elem, "name")
    array_type_ref, element_type, dimensions, size_in_bits = _parse_array_fields(
        reader, elem, definition.parameter_types
    )

    # Parse alarm ranges
    alarm_ranges = _parse_static_alarm_ranges(reader, elem)
    context_alarms = _parse_context_alarm_list(reader, elem)

    return ArrayParameterType(
        name=name,
        size_in_bits=size_in_bits,
        array_type_ref=array_type_ref,
        element_type=element_type,
        dimensions=dimensions,
        alarm_ranges=alarm_ranges,
        context_alarms=context_alarms,
    )
