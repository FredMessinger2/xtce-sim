"""One module per XTCE type family; the dispatch registry is built here.

Each family module holds both flavors of its family — the ArgumentType
parser and the ParameterType parser — as functions taking the parser
(``reader``) first. The registry below drives the two type-set walks in
commands.py and telemetry.py.

REGISTRATION ORDER IS SEMANTIC, for two reasons:

1. Arrays and aggregates must come last in each tuple. Their parsers
   (types/arrays.py, types/aggregate.py) resolve element/member type
   references against the definition at parse time, so every type they
   can reference must already be in the definition's dict when they run.
   Reordering them ahead of the scalar families yields size_in_bits 0
   and unresolved element/member types.

2. Within each side, the walk order is observable and pinned: types land
   in the definition's dict in walk order (insertion order is visible to
   consumers), and the per-type DEBUG trace is emitted in walk order
   (the equivalence baseline pins both). The two sides genuinely differ
   — the argument walk reads Binary before String, the parameter walk
   String before Binary — so each side keeps its own tuple.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

from xtce_sim.models import XTCEDefinition
from xtce_sim.parser.reader import ReaderMixin
from xtce_sim.parser.types import (
    aggregate,
    arrays,
    binary,
    boolean,
    enumerated,
    floats,
    integer,
    strings,
    times,
)

# Uniform parser signature: (reader, elem, definition) -> parsed type.
# Only arrays and aggregates read the definition (to resolve element/member
# types at parse time); the other families name the parameter _definition
# to mark it as required-by-contract but deliberately unused.
ParseFn = Callable[[ReaderMixin, ET.Element, XTCEDefinition], Any]


@dataclass(frozen=True)
class TypeFamily:
    """One XTCE type family: the element tag stem and both flavor parsers.

    ``tag`` is the stem the XTCE element names are built from:
    ``tag + "ArgumentType"`` / ``tag + "ParameterType"`` (e.g. "Integer"
    -> IntegerArgumentType / IntegerParameterType).
    """

    tag: str
    parse_argument: ParseFn
    parse_parameter: ParseFn


_INTEGER = TypeFamily(
    "Integer", integer._parse_integer_argument_type, integer._parse_integer_parameter_type
)
_FLOAT = TypeFamily("Float", floats._parse_float_argument_type, floats._parse_float_parameter_type)
_ENUMERATED = TypeFamily(
    "Enumerated",
    enumerated._parse_enumerated_argument_type,
    enumerated._parse_enumerated_parameter_type,
)
_BINARY = TypeFamily(
    "Binary", binary._parse_binary_argument_type, binary._parse_binary_parameter_type
)
_STRING = TypeFamily(
    "String", strings._parse_string_argument_type, strings._parse_string_parameter_type
)
_BOOLEAN = TypeFamily(
    "Boolean", boolean._parse_boolean_argument_type, boolean._parse_boolean_parameter_type
)
_ARRAY = TypeFamily("Array", arrays._parse_array_argument_type, arrays._parse_array_parameter_type)
_ABSOLUTE_TIME = TypeFamily(
    "AbsoluteTime",
    times._parse_absolute_time_argument_type,
    times._parse_absolute_time_parameter_type,
)
_RELATIVE_TIME = TypeFamily(
    "RelativeTime",
    times._parse_relative_time_argument_type,
    times._parse_relative_time_parameter_type,
)
_AGGREGATE = TypeFamily(
    "Aggregate",
    aggregate._parse_aggregate_argument_type,
    aggregate._parse_aggregate_parameter_type,
)

# ArgumentTypeSet walk order (Binary before String).
ARGUMENT_FAMILIES: tuple[TypeFamily, ...] = (
    _INTEGER,
    _FLOAT,
    _ENUMERATED,
    _BINARY,
    _STRING,
    _BOOLEAN,
    _ARRAY,
    _ABSOLUTE_TIME,
    _RELATIVE_TIME,
    _AGGREGATE,
)

# ParameterTypeSet walk order (String before Binary).
PARAMETER_FAMILIES: tuple[TypeFamily, ...] = (
    _INTEGER,
    _FLOAT,
    _ENUMERATED,
    _STRING,
    _BINARY,
    _BOOLEAN,
    _ARRAY,
    _ABSOLUTE_TIME,
    _RELATIVE_TIME,
    _AGGREGATE,
)
