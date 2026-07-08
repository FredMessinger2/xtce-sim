"""
XTCE Parser Module

Parses XTCE (XML Telemetric and Command Exchange) files to extract
command and telemetry definitions. Supports XTCE 1.2 and 1.3 standard elements
including CommandMetaData, MetaCommandSet, ArgumentTypeSet, TelemetryMetaData,
StaticAlarmRanges, ContextAlarms, and AggregateDataTypes.

Reference: OMG XTCE 1.2/1.3 Specification (https://www.omg.org/spec/XTCE)
"""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# Import all models from the models module
from xtce_sim.models import (
    AbsoluteTimeArgumentType,
    AbsoluteTimeParameterType,
    AggregateArgumentType,
    AggregateMember,
    AggregateParameterType,
    AlarmRange,
    # Command structures
    Argument,
    # Argument types
    ArrayArgumentType,
    ArrayParameterType,
    BinaryArgumentType,
    BinaryParameterType,
    BooleanArgumentType,
    BooleanParameterType,
    Calibrator,
    CommandContainer,
    ContainerEntry,
    ContextAlarm,
    ContextMatch,
    # Enums
    DataEncoding,
    EnumeratedArgumentType,
    EnumeratedParameterType,
    # Supporting types
    EnumerationValue,
    FloatArgumentType,
    FloatParameterType,
    IntegerArgumentType,
    IntegerParameterType,
    MetaCommand,
    # Telemetry structures
    Parameter,
    # Parameter types
    RelativeTimeArgumentType,
    RelativeTimeParameterType,
    SequenceContainer,
    StaticAlarmRanges,
    StringArgumentType,
    StringParameterType,
    Unit,
    ValidRange,
    # Top-level
    XTCEDefinition,
)

logger = logging.getLogger("xtce_sim.parser")

# Common XTCE namespace URIs (different versions use different URIs)
XTCE_NAMESPACES = [
    "http://www.omg.org/spec/XTCE/20250214",  # XTCE 1.3 (newest)
    "http://www.omg.org/spec/XTCE/20180204",  # XTCE 1.2
    "http://www.omg.org/space/xtce",  # Older format
    "www.omg.org",  # Simplified (used in our telemetry file)
]


class XTCEParser:
    """
    Parser for XTCE XML files.

    Extracts command and telemetry definitions from XTCE files following
    the OMG XTCE 1.2 standard. Supports ArgumentTypeSet, MetaCommandSet,
    ParameterTypeSet, ParameterSet, and ContainerSet elements.

    Usage:
        parser = XTCEParser()
        definition = parser.parse("spacecraft.xml")
        for cmd in definition.get_concrete_commands():
            print(f"Command: {cmd.name}")
    """

    def __init__(self):
        self.ns: str = ""  # Namespace URI
        self.ns_prefix: str = ""  # Namespace prefix for ElementTree
        # Diagnostic warnings are suppressed for the intermediate per-file parses
        # inside parse_multiple() — refs may only resolve after the merge.
        self._warn: bool = True
        # Elements the parser actually looked at (by id()), recorded in
        # _find/_findall — the choke points all element access goes through.
        # Swept after each parse to report what the file declared but the
        # parser never consumed. Reset per parse().
        self._touched: set[int] = set()

    def _detect_namespace(self, root: ET.Element) -> str:
        """Detect XTCE namespace from root element."""
        # Check tag for namespace
        if root.tag.startswith("{"):
            ns = root.tag[1 : root.tag.index("}")]
            return ns

        # Check xmlns attribute
        for attr, value in root.attrib.items():
            if attr == "xmlns" or attr.endswith("}xmlns"):
                if any(known_ns in value for known_ns in XTCE_NAMESPACES):
                    return value

        # Default to XTCE 1.2
        return XTCE_NAMESPACES[0]

    def _tag(self, name: str) -> str:
        """Create namespaced tag name."""
        if self.ns:
            return f"{{{self.ns}}}{name}"
        return name

    def _find(self, element: ET.Element, path: str) -> Optional[ET.Element]:
        """Find element with namespace handling, recording it as consumed."""
        # Convert path to namespaced version
        parts = path.split("/")
        ns_path = "/".join(self._tag(p) for p in parts if p)
        found = element.find(ns_path)
        if found is not None:
            self._touched.add(id(found))
        return found

    def _findall(self, element: ET.Element, path: str) -> list[ET.Element]:
        """Find all elements with namespace handling, recording them as consumed."""
        parts = path.split("/")
        ns_path = "/".join(self._tag(p) for p in parts if p)
        found = element.findall(ns_path)
        for child in found:
            self._touched.add(id(child))
        return found

    def _get_attr(self, element: ET.Element, name: str, default: str = "") -> str:
        """Get attribute value with default."""
        return element.attrib.get(name, default)

    def _strip_path_ref(self, ref: str) -> str:
        """Strip XTCE path-qualified reference down to the leaf name.

        XTCE allows cross-SpaceSystem references with relative paths like:
          ../../CCSDSDirectTelecommand
          ../PUS_Structure_ID
          BusElectronics/../BusElectronics/Battery_Charge_Mode

        Since we flatten all SpaceSystems into one definition, we only
        need the final name component.
        """
        if "/" in ref:
            return ref.split("/")[-1]
        return ref

    def parse_multiple(self, xml_paths: list[str | Path]) -> XTCEDefinition:
        """Parse multiple XTCE files and merge them into one definition.

        Files are processed in order. Later files can add new definitions or
        override existing ones (e.g., an alarms file overlaying a base file).
        The SpaceSystem name comes from the first file.

        Args:
            xml_paths: List of paths to XTCE XML files.

        Returns:
            Merged XTCEDefinition containing all commands, telemetry, and types.
        """
        if not xml_paths:
            raise ValueError("At least one XTCE file is required")

        # Suppress *base-ref* warnings for the per-file parses (a base ref may
        # live in a later file and only resolve after the merge). Emptiness is
        # still checked per file, so a single file that contributes nothing —
        # e.g. a namespace mismatch — is caught even when others populate the
        # merged definition.
        self._warn = False
        try:
            merged = self.parse(xml_paths[0])
            self._warn_if_empty(merged, xml_paths[0])
            for path in xml_paths[1:]:
                additional = self.parse(path)
                self._warn_if_empty(additional, path)
                merged.merge(additional)
        finally:
            self._warn = True

        # Re-resolve references (now warning) once all definitions are merged.
        self._resolve_references(merged)

        return merged

    def parse(self, xml_path: str | Path) -> XTCEDefinition:
        """
        Parse a single XTCE XML file and return definition.

        Recursively walks nested SpaceSystem elements to collect all
        CommandMetaData and TelemetryMetaData from every level of the
        hierarchy. This handles both flat XTCE and deeply nested XTCE
        (SpaceSystems with subsystems).

        For multiple files, use parse_multiple() which merges additively.

        Args:
            xml_path: Path to XTCE XML file

        Returns:
            XTCEDefinition containing parsed commands, telemetry, and types
        """
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Detect and set namespace
        self.ns = self._detect_namespace(root)

        # Get SpaceSystem name from root element
        space_system_name = self._get_attr(root, "name", "Unknown")
        logger.info("parsing %s (SpaceSystem %r)", xml_path, space_system_name)

        definition = XTCEDefinition(space_system_name=space_system_name, namespace=self.ns)

        # Recursively walk all SpaceSystem elements (starting from root)
        # to collect CommandMetaData and TelemetryMetaData from all levels
        self._touched.clear()
        self._parse_space_system(root, definition)

        # Resolve references after all SpaceSystems have been parsed
        self._resolve_references(definition)

        # Report elements the file declared but the parse above never read
        # (only when a trace is listening — the sweep is pure diagnostics).
        if logger.isEnabledFor(logging.INFO):
            self._report_unconsumed(root)

        logger.info(
            "parsed %s: %d parameter types, %d parameters, %d containers, "
            "%d argument types, %d commands",
            xml_path,
            len(definition.parameter_types),
            len(definition.parameters),
            len(definition.containers),
            len(definition.argument_types),
            len(definition.meta_commands),
        )
        with_anc = sum(1 for c in definition.meta_commands.values() if c.ancillary_data)
        if with_anc:
            logger.info(
                "~ %d command(s) carry ancillary data — "
                "parsed and preserved but not interpreted by the sim",
                with_anc,
            )

        if self._warn:
            self._warn_if_empty(definition, xml_path)
        return definition

    # Purely documentational elements — reported at DEBUG (the firehose tier)
    # rather than INFO, so a Header on every file doesn't drown real gaps
    # like an unsupported calibrator or container entry type.
    _DOC_ELEMENTS = frozenset({"Header", "LongDescription", "AliasSet"})

    @staticmethod
    def _local_tag(tag: object) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else str(tag)

    def _report_unconsumed(self, root: ET.Element) -> None:
        """Report elements the file declared but the parse never read.

        Walks the tree, descending only into elements the parser consumed
        (recorded by _find/_findall). The topmost unconsumed element is
        reported once per tag — with a count, its nested-element total, and
        one example location — rather than flooding a line per occurrence.
        """
        ignored = self._collect_unconsumed(root)
        for tag in sorted(ignored, key=lambda t: (-ignored[t][0], t)):
            count, nested, context = ignored[tag]
            level = logging.DEBUG if tag in self._DOC_ELEMENTS else logging.INFO
            logger.log(
                level,
                "~ ignored %d <%s> element(s)%s (e.g. under %s) — present in "
                "the XTCE but not read by this parser",
                count,
                tag,
                f" (+{nested} nested)" if nested else "",
                context,
            )

    def _collect_unconsumed(self, root: ET.Element) -> dict[str, list]:
        """Gather topmost unconsumed elements, grouped by local tag.

        Returns ``{tag: [occurrences, nested descendants, example context]}``.
        """
        ignored: dict[str, list] = {}
        stack: list[ET.Element] = [root]
        while stack:
            elem = stack.pop()
            # reversed() so LIFO pops visit children in document order — the
            # "e.g. under ..." example is then the first occurrence in the file.
            for child in reversed(elem):
                if not isinstance(child.tag, str):  # comments / PIs
                    continue
                if id(child) in self._touched:
                    stack.append(child)
                else:
                    self._record_ignored(ignored, elem, child)
        return ignored

    def _record_ignored(self, ignored: dict[str, list], parent: ET.Element, child: ET.Element) -> None:
        """Fold one unconsumed element into the per-tag grouping."""
        entry = ignored.setdefault(self._local_tag(child.tag), [0, 0, ""])
        entry[0] += 1
        entry[1] += sum(1 for _ in child.iter()) - 1  # iter() includes self
        if not entry[2]:
            parent_name = self._get_attr(parent, "name")
            entry[2] = self._local_tag(parent.tag) + (
                f" {parent_name!r}" if parent_name else ""
            )

    def _warn_if_empty(self, definition: XTCEDefinition, source) -> None:
        """Warn when a parse yields nothing — usually a namespace mismatch.

        The parser matches elements in the detected namespace; if the file's
        namespace isn't one we recognize, everything silently fails to match and
        the result is an empty definition rather than an error.
        """
        if not (
            definition.meta_commands
            or definition.containers
            or definition.parameter_types
            or definition.argument_types
        ):
            logger.warning(
                "parsed no commands or telemetry from %s (namespace %r) — "
                "the file may use an unsupported XTCE namespace",
                source,
                definition.namespace,
            )

    def _parse_space_system(self, element: ET.Element, definition: XTCEDefinition):
        """Recursively parse a SpaceSystem element and its nested children.

        Each SpaceSystem can contain CommandMetaData, TelemetryMetaData,
        and nested SpaceSystem elements. All definitions are collected
        into a single flat XTCEDefinition — path-qualified references
        are stripped to leaf names by _strip_path_ref().
        """
        # Parse CommandMetaData if present at this level
        cmd_metadata = self._find(element, "CommandMetaData")
        if cmd_metadata is not None:
            self._parse_command_metadata(cmd_metadata, definition)

        # Parse TelemetryMetaData if present at this level
        tlm_metadata = self._find(element, "TelemetryMetaData")
        if tlm_metadata is not None:
            self._parse_telemetry_metadata(tlm_metadata, definition)

        # Recurse into nested SpaceSystem elements
        for child_ss in self._findall(element, "SpaceSystem"):
            logger.info(
                "nested SpaceSystem %r flattened into the definition",
                self._get_attr(child_ss, "name"),
            )
            self._parse_space_system(child_ss, definition)

    # ========================================================================
    # COMMAND PARSING
    # ========================================================================

    def _store_type(self, store: dict, parsed) -> None:
        """Store a parsed argument/parameter type, tracing it at firehose level."""
        store[parsed.name] = parsed
        logger.debug(
            "    %s %r: %s bits",
            type(parsed).__name__,
            parsed.name,
            getattr(parsed, "size_in_bits", "?"),
        )

    def _parse_command_metadata(self, cmd_metadata: ET.Element, definition: XTCEDefinition):
        """Parse CommandMetaData section."""
        # Parse ArgumentTypeSet
        arg_type_set = self._find(cmd_metadata, "ArgumentTypeSet")
        if arg_type_set is not None:
            self._parse_argument_type_set(arg_type_set, definition)

        # Parse MetaCommandSet
        meta_cmd_set = self._find(cmd_metadata, "MetaCommandSet")
        if meta_cmd_set is not None:
            self._parse_meta_command_set(meta_cmd_set, definition)

    def _parse_argument_type_set(self, arg_type_set: ET.Element, definition: XTCEDefinition):
        """Parse ArgumentTypeSet and populate definition.argument_types."""

        # Integer argument types
        for elem in self._findall(arg_type_set, "IntegerArgumentType"):
            arg_type = self._parse_integer_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # Float argument types
        for elem in self._findall(arg_type_set, "FloatArgumentType"):
            arg_type = self._parse_float_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # Enumerated argument types
        for elem in self._findall(arg_type_set, "EnumeratedArgumentType"):
            arg_type = self._parse_enumerated_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # Binary argument types
        for elem in self._findall(arg_type_set, "BinaryArgumentType"):
            arg_type = self._parse_binary_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # String argument types
        for elem in self._findall(arg_type_set, "StringArgumentType"):
            arg_type = self._parse_string_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # Boolean argument types (XTCE 1.2+)
        for elem in self._findall(arg_type_set, "BooleanArgumentType"):
            arg_type = self._parse_boolean_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # Array argument types
        # Note: Arrays are parsed after other types so element types can be resolved
        for elem in self._findall(arg_type_set, "ArrayArgumentType"):
            arg_type = self._parse_array_argument_type(elem, definition)
            self._store_type(definition.argument_types, arg_type)

        # Absolute time argument types
        for elem in self._findall(arg_type_set, "AbsoluteTimeArgumentType"):
            arg_type = self._parse_absolute_time_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # Relative time argument types
        for elem in self._findall(arg_type_set, "RelativeTimeArgumentType"):
            arg_type = self._parse_relative_time_argument_type(elem)
            self._store_type(definition.argument_types, arg_type)

        # Aggregate argument types (XTCE 1.3+)
        # Note: Aggregates are parsed after other types so member types can be resolved
        for elem in self._findall(arg_type_set, "AggregateArgumentType"):
            arg_type = self._parse_aggregate_argument_type(elem, definition)
            self._store_type(definition.argument_types, arg_type)

    def _parse_integer_argument_type(self, elem: ET.Element) -> IntegerArgumentType:
        """Parse IntegerArgumentType element."""
        name = self._get_attr(elem, "name")
        signed = self._get_attr(elem, "signed", "false").lower() == "true"

        # Default values
        size_in_bits = 32
        encoding = DataEncoding.UNSIGNED
        unit = None
        valid_range = None

        # Parse IntegerDataEncoding
        data_enc = self._find(elem, "IntegerDataEncoding")
        if data_enc is not None:
            size_in_bits = int(self._get_attr(data_enc, "sizeInBits", "8"))
            enc_str = self._get_attr(data_enc, "encoding", "unsigned")
            try:
                encoding = DataEncoding(enc_str)
            except ValueError:
                encoding = DataEncoding.UNSIGNED

        # Parse UnitSet
        unit_set = self._find(elem, "UnitSet")
        if unit_set is not None:
            unit_elem = self._find(unit_set, "Unit")
            if unit_elem is not None and unit_elem.text:
                unit = unit_elem.text

        # Parse ValidRange
        valid_range = self._parse_valid_range(elem)

        return IntegerArgumentType(
            name=name,
            size_in_bits=size_in_bits,
            encoding=encoding,
            signed=signed,
            unit=unit,
            valid_range=valid_range,
        )

    def _parse_float_argument_type(self, elem: ET.Element) -> FloatArgumentType:
        """Parse FloatArgumentType element."""
        name = self._get_attr(elem, "name")

        # Default values
        size_in_bits = 32
        encoding = DataEncoding.IEEE754_1985
        unit = None
        valid_range = None

        # Parse FloatDataEncoding
        data_enc = self._find(elem, "FloatDataEncoding")
        if data_enc is not None:
            size_in_bits = int(self._get_attr(data_enc, "sizeInBits", "32"))
            enc_str = self._get_attr(data_enc, "encoding", "IEEE754_1985")
            try:
                encoding = DataEncoding(enc_str)
            except ValueError:
                encoding = DataEncoding.IEEE754_1985

        # Parse UnitSet
        unit_set = self._find(elem, "UnitSet")
        if unit_set is not None:
            unit_elem = self._find(unit_set, "Unit")
            if unit_elem is not None and unit_elem.text:
                unit = unit_elem.text

        # Parse ValidRange
        valid_range = self._parse_valid_range(elem)

        return FloatArgumentType(
            name=name,
            size_in_bits=size_in_bits,
            encoding=encoding,
            unit=unit,
            valid_range=valid_range,
        )

    def _parse_enumerated_argument_type(self, elem: ET.Element) -> EnumeratedArgumentType:
        """Parse EnumeratedArgumentType element."""
        name = self._get_attr(elem, "name")

        enumerations = []
        enum_list = self._find(elem, "EnumerationList")
        if enum_list is not None:
            for enum_elem in self._findall(enum_list, "Enumeration"):
                label = self._get_attr(enum_elem, "label")
                value = int(self._get_attr(enum_elem, "value", "0"))
                enumerations.append(EnumerationValue(label=label, value=value))

        # Get size from IntegerDataEncoding if present
        data_enc = self._find(elem, "IntegerDataEncoding")
        if data_enc is not None:
            size_in_bits = int(self._get_attr(data_enc, "sizeInBits", "8"))
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
                "~ enum %r: no IntegerDataEncoding — inferred %d bits from "
                "max enumeration value %d",
                name,
                size_in_bits,
                max_val,
            )

        return EnumeratedArgumentType(
            name=name, size_in_bits=size_in_bits, enumerations=enumerations
        )

    def _fixed_size_in_bits(self, size_bits: ET.Element) -> Optional[int]:
        """Read a fixed bit count from a ``<SizeInBits>`` element.

        Accepts either ``<SizeInBits><FixedValue>N</FixedValue></SizeInBits>``
        or the ``<SizeInBits><Fixed><FixedValue>N</FixedValue></Fixed>`` wrapper
        (the form StringDataEncoding uses). Returns None when no numeric fixed
        value is present.
        """
        fixed_val = self._find(size_bits, "FixedValue")
        if fixed_val is None:
            fixed = self._find(size_bits, "Fixed")
            if fixed is not None:
                fixed_val = self._find(fixed, "FixedValue")
        if fixed_val is not None and (fixed_val.text or "").strip().isdigit():
            return int(fixed_val.text)
        return None

    def _binary_encoding_size_bits(self, elem: ET.Element) -> Optional[int]:
        """Fixed bit count from an element's ``<BinaryDataEncoding><SizeInBits>``.

        Returns None when there is no BinaryDataEncoding, no SizeInBits, or no
        numeric fixed value declared.
        """
        bin_enc = self._find(elem, "BinaryDataEncoding")
        if bin_enc is None:
            return None
        size_bits = self._find(bin_enc, "SizeInBits")
        if size_bits is None:
            return None
        return self._fixed_size_in_bits(size_bits)

    def _binary_size_in_bits(self, elem: ET.Element) -> int:
        """Fixed size (in bits) of a binary type's BinaryDataEncoding.

        Prefers ``<BinaryDataEncoding><SizeInBits>`` (with or without the
        ``<Fixed>`` wrapper) and falls back to a legacy ``sizeInBits``
        attribute. Returns 0 if no size is declared.
        """
        bits = self._binary_encoding_size_bits(elem)
        if bits is not None:
            return bits
        attr = self._get_attr(elem, "sizeInBits").strip()
        if attr.isdigit():
            logger.info(
                "~ %r: binary size %s bits taken from legacy sizeInBits "
                "attribute (no BinaryDataEncoding/SizeInBits element)",
                self._get_attr(elem, "name"),
                attr,
            )
            return int(attr)
        return 0

    def _parse_binary_argument_type(self, elem: ET.Element) -> BinaryArgumentType:
        """Parse BinaryArgumentType element."""
        name = self._get_attr(elem, "name")
        return BinaryArgumentType(name=name, size_in_bits=self._binary_size_in_bits(elem))

    # Charsets the codec actually honors: it always encodes UTF-8, of which
    # US-ASCII is a byte-identical subset. Anything else (UTF-16, ...) would
    # be silently mis-encoded, so declaring it draws a parse-time warning.
    _SUPPORTED_STRING_CHARSETS = ("UTF-8", "US-ASCII", "ASCII")

    def _string_size_and_length(self, elem: ET.Element) -> tuple[int, Optional[int]]:
        """Fixed size and byte length from a String type's StringDataEncoding.

        Returns ``(size_in_bits, max_length)`` where max_length is
        ``size_in_bits // 8``; ``(0, None)`` when no fixed size is declared.
        Warns when the declared charset is one the codec does not honor.
        """
        str_enc = self._find(elem, "StringDataEncoding")
        if str_enc is None:
            return 0, None
        charset = self._get_attr(str_enc, "encoding", "UTF-8")
        if charset.upper() not in self._SUPPORTED_STRING_CHARSETS:
            # Deliberately NOT gated on self._warn: unlike base-ref warnings,
            # an unsupported charset cannot become valid after a multi-file
            # merge, and each type element is parsed exactly once — so this
            # must fire even during parse_multiple's intermediate parses.
            logger.warning(
                "string type %r declares encoding %r; xtce-sim encodes UTF-8 "
                "only, so values will not match this declaration",
                self._get_attr(elem, "name"),
                charset,
            )
        size_bits = self._find(str_enc, "SizeInBits")
        if size_bits is None:
            return 0, None
        bits = self._fixed_size_in_bits(size_bits)
        if bits is None:
            return 0, None
        return bits, bits // 8

    def _parse_string_argument_type(self, elem: ET.Element) -> StringArgumentType:
        """Parse StringArgumentType element."""
        name = self._get_attr(elem, "name")
        size_in_bits, max_length = self._string_size_and_length(elem)
        return StringArgumentType(name=name, size_in_bits=size_in_bits, max_length=max_length)

    def _parse_boolean_fields(
        self, elem: ET.Element
    ) -> tuple[str, str, str, Optional[bool], int]:
        """Shared parsing for Boolean argument/parameter types.

        Returns ``(name, zero_string, one_string, initial_value, size_in_bits)``.
        XTCE Boolean types specify zero/one display strings, an optional
        initial "true"/"false", and a bit size (default 1). An
        IntegerDataEncoding ``sizeInBits`` takes precedence; otherwise a
        BinaryDataEncoding fixed size is used if present.
        """
        name = self._get_attr(elem, "name")
        zero_str = self._get_attr(elem, "zeroStringValue", "False")
        one_str = self._get_attr(elem, "oneStringValue", "True")

        initial_value = None
        init_str = self._get_attr(elem, "initialValue", "")
        if init_str.lower() == "true":
            initial_value = True
        elif init_str.lower() == "false":
            initial_value = False

        size_in_bits = 1
        data_enc = self._find(elem, "IntegerDataEncoding")
        if data_enc is not None:
            size_in_bits = int(self._get_attr(data_enc, "sizeInBits", "1"))
        else:
            bits = self._binary_encoding_size_bits(elem)
            if bits is not None:
                size_in_bits = bits
            else:
                logger.info(
                    "~ Boolean %r: no encoding declared — defaulted to 1 bit", name
                )

        return name, zero_str, one_str, initial_value, size_in_bits

    def _parse_boolean_argument_type(self, elem: ET.Element) -> BooleanArgumentType:
        """Parse BooleanArgumentType element."""
        name, zero_str, one_str, initial_value, size_in_bits = self._parse_boolean_fields(elem)
        return BooleanArgumentType(
            name=name,
            size_in_bits=size_in_bits,
            zero_string_value=zero_str,
            one_string_value=one_str,
            initial_value=initial_value,
        )

    def _parse_array_argument_type(
        self, elem: ET.Element, definition: XTCEDefinition
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
        name = self._get_attr(elem, "name")
        array_type_ref = self._strip_path_ref(self._get_attr(elem, "arrayTypeRef", ""))

        # Resolve element type
        element_type = definition.argument_types.get(array_type_ref)

        # Parse dimensions
        dimensions = []
        dim_list = self._find(elem, "DimensionList")
        if dim_list is not None:
            for dim in self._findall(dim_list, "Dimension"):
                dim_size, is_dynamic, dynamic_ref = self._parse_dimension(dim)
                dimensions.append((dim_size, is_dynamic, dynamic_ref))

        # Calculate total size in bits if possible
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

        return ArrayArgumentType(
            name=name,
            size_in_bits=size_in_bits,
            array_type_ref=array_type_ref,
            element_type=element_type,
            dimensions=dimensions,
        )

    def _parse_index_element(self, idx_elem: ET.Element) -> tuple[int, Optional[str]]:
        """Read one array-dimension index (``<StartingIndex>``/``<EndingIndex>``).

        Returns ``(index, dynamic_ref)``: the fixed index value (0 if not
        given) and, when the index is a DynamicValue/ParameterInstanceRef, the
        referenced parameter name — else None.
        """
        index = 0
        fixed = self._find(idx_elem, "FixedValue")
        if fixed is not None and fixed.text:
            index = int(fixed.text)
        dynamic_ref = None
        dyn_val = self._find(idx_elem, "DynamicValue")
        if dyn_val is not None:
            param_ref = self._find(dyn_val, "ParameterInstanceRef")
            if param_ref is not None:
                dynamic_ref = self._strip_path_ref(self._get_attr(param_ref, "parameterRef"))
        return index, dynamic_ref

    def _parse_dimension(self, dim: ET.Element) -> tuple[int, bool, Optional[str]]:
        """
        Parse a Dimension element for array types.

        Returns (size, is_dynamic, dynamic_ref) where:
        - size: Fixed dimension size (or 0 if dynamic)
        - is_dynamic: True if size comes from another parameter
        - dynamic_ref: Parameter reference for dynamic sizing
        """
        start_idx = 0
        end_idx = 0
        is_dynamic = False
        dynamic_ref = None

        # StartingIndex / EndingIndex share the same shape: a fixed value or a
        # DynamicValue/ParameterInstanceRef. Either index being dynamic makes
        # the whole dimension dynamic.
        start_elem = self._find(dim, "StartingIndex")
        if start_elem is not None:
            start_idx, ref = self._parse_index_element(start_elem)
            if ref is not None:
                is_dynamic = True
                dynamic_ref = ref

        end_elem = self._find(dim, "EndingIndex")
        if end_elem is not None:
            end_idx, ref = self._parse_index_element(end_elem)
            if ref is not None:
                is_dynamic = True
                dynamic_ref = ref

        # Size is end - start + 1 (inclusive range)
        size = end_idx - start_idx + 1 if not is_dynamic else 0

        return (size, is_dynamic, dynamic_ref)

    def _parse_absolute_time_argument_type(self, elem: ET.Element) -> AbsoluteTimeArgumentType:
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
        name = self._get_attr(elem, "name")
        epoch = "UNIX"
        scale = 1.0
        offset = 0.0
        size_in_bits = 32
        reference_time_ref = None

        # Check for Encoding element
        encoding = self._find(elem, "Encoding")
        if encoding is not None:
            # Get scale and offset from encoding
            scale_str = self._get_attr(encoding, "scale", "1.0")
            offset_str = self._get_attr(encoding, "offset", "0.0")
            scale = float(scale_str)
            offset = float(offset_str)

            # Check for IntegerDataEncoding inside encoding
            int_enc = self._find(encoding, "IntegerDataEncoding")
            if int_enc is not None:
                size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Also check for direct IntegerDataEncoding (XTCE 1.2 style)
        int_enc = self._find(elem, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Parse ReferenceTime to get epoch
        ref_time = self._find(elem, "ReferenceTime")
        if ref_time is not None:
            # Check for Epoch element
            epoch_elem = self._find(ref_time, "Epoch")
            if epoch_elem is not None and epoch_elem.text:
                epoch = epoch_elem.text
            # Check for OffsetFrom (relative to another time parameter)
            offset_from = self._find(ref_time, "OffsetFrom")
            if offset_from is not None:
                reference_time_ref = self._strip_path_ref(
                    self._get_attr(offset_from, "parameterRef")
                )

        return AbsoluteTimeArgumentType(
            name=name,
            size_in_bits=size_in_bits,
            epoch=epoch,
            scale=scale,
            offset=offset,
            reference_time_ref=reference_time_ref,
        )

    def _parse_relative_time_argument_type(self, elem: ET.Element) -> RelativeTimeArgumentType:
        """
        Parse RelativeTimeArgumentType element.

        RelativeTime represents durations/intervals rather than absolute timestamps.
        Typically encoded as scaled integers representing seconds or milliseconds.
        """
        name = self._get_attr(elem, "name")
        scale = 1.0
        offset = 0.0
        size_in_bits = 32

        # Check for Encoding element
        encoding = self._find(elem, "Encoding")
        if encoding is not None:
            scale_str = self._get_attr(encoding, "scale", "1.0")
            offset_str = self._get_attr(encoding, "offset", "0.0")
            scale = float(scale_str)
            offset = float(offset_str)

            int_enc = self._find(encoding, "IntegerDataEncoding")
            if int_enc is not None:
                size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Also check for direct IntegerDataEncoding
        int_enc = self._find(elem, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Parse unit information
        unit = None
        unit_set = self._find(elem, "UnitSet")
        if unit_set is not None:
            unit_elem = self._find(unit_set, "Unit")
            if unit_elem is not None and unit_elem.text:
                unit = unit_elem.text

        return RelativeTimeArgumentType(
            name=name, size_in_bits=size_in_bits, scale=scale, offset=offset, unit=unit
        )

    def _parse_valid_range(self, elem: ET.Element) -> Optional[ValidRange]:
        """Parse ValidRange element from argument type."""
        # Check for ValidRange directly
        vr = self._find(elem, "ValidRange")
        if vr is None:
            # Check for ValidRangeSet
            vr_set = self._find(elem, "ValidRangeSet")
            if vr_set is not None:
                vr = self._find(vr_set, "ValidRange")

        if vr is None:
            return None

        valid_range = ValidRange()

        min_inc = self._get_attr(vr, "minInclusive")
        if min_inc:
            valid_range.min_inclusive = float(min_inc)

        max_inc = self._get_attr(vr, "maxInclusive")
        if max_inc:
            valid_range.max_inclusive = float(max_inc)

        min_exc = self._get_attr(vr, "minExclusive")
        if min_exc:
            valid_range.min_exclusive = float(min_exc)

        max_exc = self._get_attr(vr, "maxExclusive")
        if max_exc:
            valid_range.max_exclusive = float(max_exc)

        return valid_range

    def _parse_alarm_range(self, elem: ET.Element) -> AlarmRange:
        """Parse a single alarm range element (WarningRange, CriticalRange, etc.)."""
        alarm = AlarmRange()

        min_inc = self._get_attr(elem, "minInclusive")
        if min_inc:
            alarm.min_inclusive = float(min_inc)

        max_inc = self._get_attr(elem, "maxInclusive")
        if max_inc:
            alarm.max_inclusive = float(max_inc)

        min_exc = self._get_attr(elem, "minExclusive")
        if min_exc:
            alarm.min_exclusive = float(min_exc)

        max_exc = self._get_attr(elem, "maxExclusive")
        if max_exc:
            alarm.max_exclusive = float(max_exc)

        return alarm

    def _parse_static_alarm_ranges(self, elem: ET.Element) -> Optional[StaticAlarmRanges]:
        """
        Parse StaticAlarmRanges element for telemetry alarm thresholds.

        XTCE defines four severity levels:
        - WatchRange: First level of concern
        - WarningRange: Approaching limits
        - DistressRange: Serious concern
        - CriticalRange: Immediate action required

        Returns None if no alarm ranges are defined.
        """
        # StaticAlarmRanges may be a direct child or nested inside DefaultAlarm
        alarm_elem = self._find(elem, "StaticAlarmRanges")
        if alarm_elem is None:
            default_alarm = self._find(elem, "DefaultAlarm")
            if default_alarm is not None:
                alarm_elem = self._find(default_alarm, "StaticAlarmRanges")
        if alarm_elem is None:
            return None

        alarms = StaticAlarmRanges()

        # Parse WatchRange (lowest severity)
        watch = self._find(alarm_elem, "WatchRange")
        if watch is not None:
            alarms.watch_range = self._parse_alarm_range(watch)

        # Parse WarningRange
        warning = self._find(alarm_elem, "WarningRange")
        if warning is not None:
            alarms.warning_range = self._parse_alarm_range(warning)

        # Parse DistressRange
        distress = self._find(alarm_elem, "DistressRange")
        if distress is not None:
            alarms.distress_range = self._parse_alarm_range(distress)

        # Parse CriticalRange
        critical = self._find(alarm_elem, "CriticalRange")
        if critical is not None:
            alarms.critical_range = self._parse_alarm_range(critical)

        return alarms

    def _parse_context_alarm_list(self, elem: ET.Element) -> list[ContextAlarm]:
        """
        Parse ContextAlarmList for context-dependent alarm definitions.

        Context alarms allow different alarm thresholds based on operational
        mode or other parameter values.

        Example XTCE:
        <ContextAlarmList>
          <ContextAlarm>
            <ContextMatch>
              <Comparison parameterRef="OperatingMode" value="SAFE"/>
            </ContextMatch>
            <StaticAlarmRanges>
              <WarningRange minInclusive="5" maxInclusive="20"/>
            </StaticAlarmRanges>
          </ContextAlarm>
        </ContextAlarmList>
        """
        context_list_elem = self._find(elem, "ContextAlarmList")
        if context_list_elem is None:
            return []

        context_alarms = []
        for context_alarm_elem in self._findall(context_list_elem, "ContextAlarm"):
            # Parse ContextMatch
            match_elem = self._find(context_alarm_elem, "ContextMatch")
            if match_elem is None:
                continue

            # Look for Comparison element
            comparison_elem = self._find(match_elem, "Comparison")
            if comparison_elem is None:
                continue

            param_ref = self._strip_path_ref(self._get_attr(comparison_elem, "parameterRef"))
            value = self._get_attr(comparison_elem, "value")
            # Default comparison is equality
            comparison_op = self._get_attr(comparison_elem, "comparisonOperator", "==")

            context_match = ContextMatch(
                parameter_ref=param_ref, value=value, comparison=comparison_op
            )

            # Parse the alarm ranges for this context
            alarm_ranges = self._parse_static_alarm_ranges(context_alarm_elem)
            if alarm_ranges is None:
                alarm_ranges = StaticAlarmRanges()

            context_alarms.append(
                ContextAlarm(context_match=context_match, alarm_ranges=alarm_ranges)
            )

        return context_alarms

    def _parse_unit_set_enhanced(self, elem: ET.Element) -> tuple[Optional[str], Optional[Unit]]:
        """
        Parse UnitSet with full XTCE 1.3 support.

        Returns tuple of (simple_unit_string, full_unit_info).
        The simple string maintains backward compatibility.

        Example XTCE:
        <UnitSet>
          <Unit description="Volts" power="1">V</Unit>
        </UnitSet>
        """
        unit_set = self._find(elem, "UnitSet")
        if unit_set is None:
            return (None, None)

        unit_elem = self._find(unit_set, "Unit")
        if unit_elem is None or not unit_elem.text:
            return (None, None)

        # Simple string for backward compatibility
        simple_unit = unit_elem.text.strip()

        # Full Unit object with all metadata
        description = self._get_attr(unit_elem, "description")
        power_str = self._get_attr(unit_elem, "power")
        power = int(power_str) if power_str else None

        unit_info = Unit(
            name=simple_unit, description=description if description else None, power=power
        )

        return (simple_unit, unit_info)

    def _parse_aggregate_members(self, elem: ET.Element) -> list[AggregateMember]:
        """
        Parse MemberList for aggregate types.

        Example XTCE:
        <MemberList>
          <Member name="Latitude" typeRef="Float64Type"/>
          <Member name="Longitude" typeRef="Float64Type"/>
        </MemberList>
        """
        members = []
        member_list = self._find(elem, "MemberList")
        if member_list is None:
            return members

        for member_elem in self._findall(member_list, "Member"):
            name = self._get_attr(member_elem, "name")
            type_ref = self._strip_path_ref(self._get_attr(member_elem, "typeRef"))
            description = self._get_attr(member_elem, "shortDescription")

            if name and type_ref:
                members.append(
                    AggregateMember(
                        name=name,
                        type_ref=type_ref,
                        description=description if description else None,
                    )
                )

        return members

    def _parse_aggregate_argument_type(
        self, elem: ET.Element, definition: XTCEDefinition
    ) -> AggregateArgumentType:
        """
        Parse AggregateArgumentType element.

        XTCE 1.3 aggregate types represent structured command data
        with multiple named members.
        """
        name = self._get_attr(elem, "name")
        description = self._get_attr(elem, "shortDescription")

        members = self._parse_aggregate_members(elem)

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
        self, elem: ET.Element, definition: XTCEDefinition
    ) -> AggregateParameterType:
        """
        Parse AggregateParameterType element.

        XTCE 1.3 aggregate types represent structured telemetry data
        with multiple named members (similar to C structs).
        """
        name = self._get_attr(elem, "name")
        description = self._get_attr(elem, "shortDescription")

        members = self._parse_aggregate_members(elem)

        # Calculate total size from member types if available
        size_in_bits = 0
        for member in members:
            member_type = definition.parameter_types.get(member.type_ref)
            if member_type:
                size_in_bits += member_type.size_in_bits

        # Parse alarm ranges (aggregates can have them too)
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)

        return AggregateParameterType(
            name=name,
            size_in_bits=size_in_bits,
            description=description if description else None,
            members=members,
            alarm_ranges=alarm_ranges,
            context_alarms=context_alarms,
        )

    def _parse_meta_command_set(self, meta_cmd_set: ET.Element, definition: XTCEDefinition):
        """Parse MetaCommandSet and populate definition.meta_commands."""
        for elem in self._findall(meta_cmd_set, "MetaCommand"):
            cmd = self._parse_meta_command(elem, definition)
            definition.meta_commands[cmd.name] = cmd
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  MetaCommand %r: %d arg(s)%s%s",
                    cmd.name,
                    len(cmd.arguments),
                    ", abstract" if cmd.abstract else "",
                    f", base={cmd.base_meta_command_ref}" if cmd.base_meta_command_ref else "",
                )

    def _parse_ancillary_data(self, parent: ET.Element, *, merge: bool = False) -> dict[str, str]:
        """Parse a ``<AncillaryDataSet>`` under *parent* into ``{name: value}``.

        With ``merge=False`` a repeated name keeps the last value; with
        ``merge=True`` repeated names are joined with ``;`` (used at the
        command level, where a name may legitimately repeat).
        """
        data: dict[str, str] = {}
        anc_set = self._find(parent, "AncillaryDataSet")
        if anc_set is not None:
            for anc_elem in self._findall(anc_set, "AncillaryData"):
                anc_name = self._get_attr(anc_elem, "name", "")
                if not anc_name:
                    continue
                value = (anc_elem.text or "").strip()
                if merge and data.get(anc_name):
                    data[anc_name] = data[anc_name] + ";" + value
                else:
                    data[anc_name] = value
        return data

    def _parse_argument_assignments(self, base_elem: ET.Element) -> dict[str, str]:
        """Parse a BaseMetaCommand's ArgumentAssignmentList into ``{name: value}``.

        Derived commands fix inherited argument values (e.g. RW_UNIT_ID=1).
        """
        assignments: dict[str, str] = {}
        assign_list = self._find(base_elem, "ArgumentAssignmentList")
        if assign_list is not None:
            for assign_elem in self._findall(assign_list, "ArgumentAssignment"):
                arg_name = self._get_attr(assign_elem, "argumentName")
                if arg_name:
                    assignments[arg_name] = self._get_attr(assign_elem, "argumentValue") or ""
        return assignments

    def _parse_command_arguments(
        self, arg_list: ET.Element, definition: XTCEDefinition
    ) -> list[Argument]:
        """Parse an ``<ArgumentList>`` into resolved Argument objects."""
        arguments = []
        for arg_elem in self._findall(arg_list, "Argument"):
            arg_type_ref = self._strip_path_ref(self._get_attr(arg_elem, "argumentTypeRef"))
            arguments.append(
                Argument(
                    name=self._get_attr(arg_elem, "name"),
                    argument_type_ref=arg_type_ref,
                    argument_type=definition.argument_types.get(arg_type_ref),
                    ancillary_data=self._parse_ancillary_data(arg_elem),
                )
            )
        return arguments

    def _parse_meta_command(self, elem: ET.Element, definition: XTCEDefinition) -> MetaCommand:
        """Parse single MetaCommand element."""
        name = self._get_attr(elem, "name")
        description = self._get_attr(elem, "shortDescription")
        abstract = self._get_attr(elem, "abstract", "false").lower() == "true"

        # Base command reference and inherited argument assignments.
        base_ref = None
        argument_assignments: dict[str, str] = {}
        base_elem = self._find(elem, "BaseMetaCommand")
        if base_elem is not None:
            base_ref = self._strip_path_ref(self._get_attr(base_elem, "metaCommandRef"))
            argument_assignments = self._parse_argument_assignments(base_elem)

        arguments: list[Argument] = []
        arg_list = self._find(elem, "ArgumentList")
        if arg_list is not None:
            arguments = self._parse_command_arguments(arg_list, definition)

        # Command-level AncillaryDataSet, semicolon-merged.
        cmd_anc_data = self._parse_ancillary_data(elem, merge=True)

        container = None
        container_elem = self._find(elem, "CommandContainer")
        if container_elem is not None:
            container = self._parse_command_container(container_elem)

        return MetaCommand(
            name=name,
            description=description,
            abstract=abstract,
            base_meta_command_ref=base_ref,
            arguments=arguments,
            container=container,
            argument_assignments=argument_assignments,
            ancillary_data=cmd_anc_data,
        )

    def _parse_command_container(self, elem: ET.Element) -> CommandContainer:
        """Parse CommandContainer element."""
        name = self._get_attr(elem, "name")

        # Base container reference
        base_ref = None
        base_elem = self._find(elem, "BaseContainer")
        if base_elem is not None:
            base_ref = self._strip_path_ref(self._get_attr(base_elem, "containerRef"))

        # Parse EntryList
        entries = []
        entry_list = self._find(elem, "EntryList")
        if entry_list is not None:
            # Fixed value entries
            for entry in self._findall(entry_list, "FixedValueEntry"):
                entry_name = self._get_attr(entry, "name")
                binary_value = self._get_attr(entry, "binaryValue")
                size_in_bits = int(self._get_attr(entry, "sizeInBits", "8"))

                entries.append(
                    ContainerEntry(
                        entry_type="fixed",
                        name=entry_name,
                        size_in_bits=size_in_bits,
                        binary_value=binary_value,
                    )
                )

            # Argument reference entries
            for entry in self._findall(entry_list, "ArgumentRefEntry"):
                arg_ref = self._strip_path_ref(self._get_attr(entry, "argumentRef"))

                entries.append(
                    ContainerEntry(entry_type="argument", name=arg_ref, argument_ref=arg_ref)
                )

            # Parameter reference entries
            for entry in self._findall(entry_list, "ParameterRefEntry"):
                param_ref = self._strip_path_ref(self._get_attr(entry, "parameterRef"))

                entries.append(
                    ContainerEntry(entry_type="parameter", name=param_ref, parameter_ref=param_ref)
                )

        return CommandContainer(name=name, entries=entries, base_container_ref=base_ref)

    # ========================================================================
    # TELEMETRY PARSING
    # ========================================================================

    def _parse_telemetry_metadata(self, tlm_metadata: ET.Element, definition: XTCEDefinition):
        """Parse TelemetryMetaData section."""
        # Parse ParameterTypeSet
        param_type_set = self._find(tlm_metadata, "ParameterTypeSet")
        if param_type_set is not None:
            self._parse_parameter_type_set(param_type_set, definition)

        # Parse ParameterSet
        param_set = self._find(tlm_metadata, "ParameterSet")
        if param_set is not None:
            self._parse_parameter_set(param_set, definition)

        # Parse ContainerSet
        container_set = self._find(tlm_metadata, "ContainerSet")
        if container_set is not None:
            self._parse_container_set(container_set, definition)

    def _parse_parameter_type_set(self, param_type_set: ET.Element, definition: XTCEDefinition):
        """Parse ParameterTypeSet and populate definition.parameter_types."""

        # Integer parameter types
        for elem in self._findall(param_type_set, "IntegerParameterType"):
            param_type = self._parse_integer_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # Float parameter types
        for elem in self._findall(param_type_set, "FloatParameterType"):
            param_type = self._parse_float_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # Enumerated parameter types
        for elem in self._findall(param_type_set, "EnumeratedParameterType"):
            param_type = self._parse_enumerated_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # String parameter types
        for elem in self._findall(param_type_set, "StringParameterType"):
            param_type = self._parse_string_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # Binary parameter types
        for elem in self._findall(param_type_set, "BinaryParameterType"):
            param_type = self._parse_binary_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # Boolean parameter types (XTCE 1.2+)
        for elem in self._findall(param_type_set, "BooleanParameterType"):
            param_type = self._parse_boolean_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # Array parameter types
        # Note: Arrays are parsed after other types so element types can be resolved
        for elem in self._findall(param_type_set, "ArrayParameterType"):
            param_type = self._parse_array_parameter_type(elem, definition)
            self._store_type(definition.parameter_types, param_type)

        # Absolute time parameter types
        for elem in self._findall(param_type_set, "AbsoluteTimeParameterType"):
            param_type = self._parse_absolute_time_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # Relative time parameter types
        for elem in self._findall(param_type_set, "RelativeTimeParameterType"):
            param_type = self._parse_relative_time_parameter_type(elem)
            self._store_type(definition.parameter_types, param_type)

        # Aggregate parameter types (XTCE 1.3+)
        # Note: Aggregates are parsed after other types so member types can be resolved
        for elem in self._findall(param_type_set, "AggregateParameterType"):
            param_type = self._parse_aggregate_parameter_type(elem, definition)
            self._store_type(definition.parameter_types, param_type)

    def _parse_calibrator(self, data_enc: ET.Element) -> Optional[Calibrator]:
        """Parse DefaultCalibrator with PolynomialCalibrator."""
        default_cal = self._find(data_enc, "DefaultCalibrator")
        if default_cal is None:
            return None

        poly_cal = self._find(default_cal, "PolynomialCalibrator")
        if poly_cal is None:
            return None

        coefficients = []
        for term in self._findall(poly_cal, "Term"):
            coef = float(self._get_attr(term, "coefficient", "0"))
            exp = int(self._get_attr(term, "exponent", "0"))
            coefficients.append((coef, exp))

        if coefficients:
            return Calibrator(coefficients=coefficients)
        return None

    def _parse_integer_parameter_type(self, elem: ET.Element) -> IntegerParameterType:
        """Parse IntegerParameterType element."""
        name = self._get_attr(elem, "name")
        signed = self._get_attr(elem, "signed", "false").lower() == "true"

        size_in_bits = 32
        encoding = DataEncoding.UNSIGNED
        calibrator = None

        # Parse IntegerDataEncoding
        data_enc = self._find(elem, "IntegerDataEncoding")
        if data_enc is not None:
            size_in_bits = int(self._get_attr(data_enc, "sizeInBits", "8"))
            enc_str = self._get_attr(data_enc, "encoding", "unsigned")
            try:
                encoding = DataEncoding(enc_str)
            except ValueError:
                encoding = DataEncoding.UNSIGNED
            calibrator = self._parse_calibrator(data_enc)

        # Parse UnitSet with full metadata
        unit, unit_info = self._parse_unit_set_enhanced(elem)

        # Parse ValidRange
        valid_range = self._parse_valid_range(elem)

        # Parse alarm ranges (XTCE 1.2+)
        alarm_ranges = self._parse_static_alarm_ranges(elem)

        # Parse context-dependent alarms
        context_alarms = self._parse_context_alarm_list(elem)

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

    def _parse_float_parameter_type(self, elem: ET.Element) -> FloatParameterType:
        """Parse FloatParameterType element."""
        name = self._get_attr(elem, "name")

        size_in_bits = 32
        encoding = DataEncoding.IEEE754_1985
        calibrator = None

        # Check for FloatDataEncoding (native float)
        float_enc = self._find(elem, "FloatDataEncoding")
        if float_enc is not None:
            size_in_bits = int(self._get_attr(float_enc, "sizeInBits", "32"))
            enc_str = self._get_attr(float_enc, "encoding", "IEEE754_1985")
            try:
                encoding = DataEncoding(enc_str)
            except ValueError:
                encoding = DataEncoding.IEEE754_1985

        # Check for IntegerDataEncoding (raw integer with calibration to float)
        int_enc = self._find(elem, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "16"))
            enc_str = self._get_attr(int_enc, "encoding", "unsigned")
            try:
                encoding = DataEncoding(enc_str)
            except ValueError:
                encoding = DataEncoding.UNSIGNED
            calibrator = self._parse_calibrator(int_enc)

        # Parse UnitSet with full metadata
        unit, unit_info = self._parse_unit_set_enhanced(elem)

        # Parse ValidRange
        valid_range = self._parse_valid_range(elem)

        # Parse alarm ranges
        alarm_ranges = self._parse_static_alarm_ranges(elem)

        # Parse context-dependent alarms
        context_alarms = self._parse_context_alarm_list(elem)

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

    def _parse_enumerated_parameter_type(self, elem: ET.Element) -> EnumeratedParameterType:
        """Parse EnumeratedParameterType element."""
        name = self._get_attr(elem, "name")

        enumerations = []
        enum_list = self._find(elem, "EnumerationList")
        if enum_list is not None:
            for enum_elem in self._findall(enum_list, "Enumeration"):
                label = self._get_attr(enum_elem, "label")
                value = int(self._get_attr(enum_elem, "value", "0"))
                enumerations.append(EnumerationValue(label=label, value=value))

        # Get size from IntegerDataEncoding
        size_in_bits = 8
        data_enc = self._find(elem, "IntegerDataEncoding")
        if data_enc is not None:
            size_in_bits = int(self._get_attr(data_enc, "sizeInBits", "8"))

        # Parse alarm ranges (enumerations can have alarms on specific values)
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)

        return EnumeratedParameterType(
            name=name,
            size_in_bits=size_in_bits,
            enumerations=enumerations,
            alarm_ranges=alarm_ranges,
            context_alarms=context_alarms,
        )

    def _parse_string_parameter_type(self, elem: ET.Element) -> StringParameterType:
        """Parse StringParameterType element."""
        name = self._get_attr(elem, "name")
        size_in_bits, max_length = self._string_size_and_length(elem)
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)
        return StringParameterType(
            name=name,
            size_in_bits=size_in_bits,
            max_length=max_length,
            alarm_ranges=alarm_ranges,
            context_alarms=context_alarms,
        )

    def _parse_binary_parameter_type(self, elem: ET.Element) -> BinaryParameterType:
        """Parse BinaryParameterType element."""
        name = self._get_attr(elem, "name")
        size_in_bits = self._binary_size_in_bits(elem)

        # Parse alarm ranges
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)

        return BinaryParameterType(
            name=name,
            size_in_bits=size_in_bits,
            alarm_ranges=alarm_ranges,
            context_alarms=context_alarms,
        )

    def _parse_boolean_parameter_type(self, elem: ET.Element) -> BooleanParameterType:
        """Parse BooleanParameterType element for telemetry (boolean fields + alarms)."""
        name, zero_str, one_str, initial_value, size_in_bits = self._parse_boolean_fields(elem)
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)
        return BooleanParameterType(
            name=name,
            size_in_bits=size_in_bits,
            zero_string_value=zero_str,
            one_string_value=one_str,
            initial_value=initial_value,
            alarm_ranges=alarm_ranges,
            context_alarms=context_alarms,
        )

    def _parse_array_parameter_type(
        self, elem: ET.Element, definition: XTCEDefinition
    ) -> ArrayParameterType:
        """
        Parse ArrayParameterType element for telemetry.

        Used for vector telemetry data like multi-channel sensors,
        memory dumps, or other repeated data structures.
        """
        name = self._get_attr(elem, "name")
        array_type_ref = self._strip_path_ref(self._get_attr(elem, "arrayTypeRef", ""))

        # Resolve element type
        element_type = definition.parameter_types.get(array_type_ref)

        # Parse dimensions
        dimensions = []
        dim_list = self._find(elem, "DimensionList")
        if dim_list is not None:
            for dim in self._findall(dim_list, "Dimension"):
                dim_size, is_dynamic, dynamic_ref = self._parse_dimension(dim)
                dimensions.append((dim_size, is_dynamic, dynamic_ref))

        # Calculate total size in bits if possible
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

        # Parse alarm ranges
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)

        return ArrayParameterType(
            name=name,
            size_in_bits=size_in_bits,
            array_type_ref=array_type_ref,
            element_type=element_type,
            dimensions=dimensions,
            alarm_ranges=alarm_ranges,
            context_alarms=context_alarms,
        )

    def _parse_absolute_time_parameter_type(self, elem: ET.Element) -> AbsoluteTimeParameterType:
        """
        Parse AbsoluteTimeParameterType element for telemetry.

        Used for packet timestamps, event times, and other absolute time values.
        """
        name = self._get_attr(elem, "name")
        epoch = "UNIX"
        scale = 1.0
        offset = 0.0
        size_in_bits = 32
        reference_time_ref = None

        # Check for Encoding element
        encoding = self._find(elem, "Encoding")
        if encoding is not None:
            scale_str = self._get_attr(encoding, "scale", "1.0")
            offset_str = self._get_attr(encoding, "offset", "0.0")
            scale = float(scale_str)
            offset = float(offset_str)

            int_enc = self._find(encoding, "IntegerDataEncoding")
            if int_enc is not None:
                size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Also check for direct IntegerDataEncoding (XTCE 1.2 style)
        int_enc = self._find(elem, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Parse ReferenceTime to get epoch
        ref_time = self._find(elem, "ReferenceTime")
        if ref_time is not None:
            epoch_elem = self._find(ref_time, "Epoch")
            if epoch_elem is not None and epoch_elem.text:
                epoch = epoch_elem.text
            offset_from = self._find(ref_time, "OffsetFrom")
            if offset_from is not None:
                reference_time_ref = self._strip_path_ref(
                    self._get_attr(offset_from, "parameterRef")
                )

        # Parse UnitSet with full metadata
        unit, unit_info = self._parse_unit_set_enhanced(elem)

        # Parse alarm ranges
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)

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

    def _parse_relative_time_parameter_type(self, elem: ET.Element) -> RelativeTimeParameterType:
        """
        Parse RelativeTimeParameterType element for telemetry.

        Used for uptime counters, elapsed times, and duration values.
        """
        name = self._get_attr(elem, "name")
        scale = 1.0
        offset = 0.0
        size_in_bits = 32

        # Check for Encoding element
        encoding = self._find(elem, "Encoding")
        if encoding is not None:
            scale_str = self._get_attr(encoding, "scale", "1.0")
            offset_str = self._get_attr(encoding, "offset", "0.0")
            scale = float(scale_str)
            offset = float(offset_str)

            int_enc = self._find(encoding, "IntegerDataEncoding")
            if int_enc is not None:
                size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Also check for direct IntegerDataEncoding
        int_enc = self._find(elem, "IntegerDataEncoding")
        if int_enc is not None:
            size_in_bits = int(self._get_attr(int_enc, "sizeInBits", "32"))

        # Parse UnitSet with full metadata
        unit, unit_info = self._parse_unit_set_enhanced(elem)

        # Parse alarm ranges
        alarm_ranges = self._parse_static_alarm_ranges(elem)
        context_alarms = self._parse_context_alarm_list(elem)

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

    def _parse_parameter_set(self, param_set: ET.Element, definition: XTCEDefinition):
        """Parse ParameterSet and populate definition.parameters."""
        for elem in self._findall(param_set, "Parameter"):
            name = self._get_attr(elem, "name")
            type_ref = self._strip_path_ref(self._get_attr(elem, "parameterTypeRef"))

            # Resolve parameter type
            param_type = definition.parameter_types.get(type_ref)

            definition.parameters[name] = Parameter(
                name=name, parameter_type_ref=type_ref, parameter_type=param_type
            )
            logger.debug(
                "  Parameter %r -> type %r%s",
                name,
                type_ref,
                "" if param_type is not None else " (unresolved)",
            )

    def _parse_container_set(self, container_set: ET.Element, definition: XTCEDefinition):
        """Parse ContainerSet and populate definition.containers."""
        for elem in self._findall(container_set, "SequenceContainer"):
            container = self._parse_sequence_container(elem)
            definition.containers[container.name] = container
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  SequenceContainer %r: %d entr%s%s%s",
                    container.name,
                    len(container.entries),
                    "y" if len(container.entries) == 1 else "ies",
                    f", base={container.base_container_ref}"
                    if container.base_container_ref
                    else "",
                    f", restriction={container.restriction_criteria}"
                    if container.restriction_criteria
                    else " (no APID restriction — abstract/base only)",
                )

    def _parse_entry_list(self, elem: ET.Element) -> list[str]:
        """Parse a container's ``<EntryList>`` into a list of parameter refs."""
        entries = []
        entry_list = self._find(elem, "EntryList")
        if entry_list is not None:
            for param_entry in self._findall(entry_list, "ParameterRefEntry"):
                entries.append(self._strip_path_ref(self._get_attr(param_entry, "parameterRef")))
        return entries

    def _parse_restriction_criteria(self, base_elem: ET.Element) -> Optional[dict]:
        """Parse a BaseContainer's ``<RestrictionCriteria>`` into ``{param: value}``.

        Accepts a direct ``<Comparison>`` or a ``<ComparisonList>`` wrapper,
        which may hold several comparisons (e.g. SecHdrFlag + APID), so all of
        them are read. The CCSDS_APID key is normalized to int. Returns None
        when no criteria are present.
        """
        restrict_elem = self._find(base_elem, "RestrictionCriteria")
        if restrict_elem is None:
            return None
        comparisons = self._findall(restrict_elem, "Comparison")
        if not comparisons:
            comp_list = self._find(restrict_elem, "ComparisonList")
            if comp_list is not None:
                comparisons = self._findall(comp_list, "Comparison")
        if not comparisons:
            return None
        criteria: dict = {}
        for comparison in comparisons:
            param_ref = self._strip_path_ref(self._get_attr(comparison, "parameterRef"))
            value = self._get_attr(comparison, "value")
            if "APID" in param_ref.upper():
                criteria["CCSDS_APID"] = int(value)
            else:
                criteria[param_ref] = value
        return criteria

    def _parse_sequence_container(self, elem: ET.Element) -> SequenceContainer:
        """Parse SequenceContainer element."""
        name = self._get_attr(elem, "name")
        entries = self._parse_entry_list(elem)

        base_ref = None
        restriction_criteria = None
        base_elem = self._find(elem, "BaseContainer")
        if base_elem is not None:
            base_ref = self._strip_path_ref(self._get_attr(base_elem, "containerRef"))
            restriction_criteria = self._parse_restriction_criteria(base_elem)

        return SequenceContainer(
            name=name,
            entries=entries,
            base_container_ref=base_ref,
            restriction_criteria=restriction_criteria,
        )

    # ========================================================================
    # REFERENCE RESOLUTION
    # ========================================================================

    def _resolve_references(self, definition: XTCEDefinition):
        """Resolve base command and container references after parsing.

        A ref that names a non-existent target is left as None and warned about
        — otherwise inherited arguments/opcode/entries are silently dropped.
        """
        # Resolve command base references
        for cmd in definition.meta_commands.values():
            if cmd.base_meta_command_ref:
                cmd.base_command = definition.meta_commands.get(cmd.base_meta_command_ref)
                if cmd.base_command is None and self._warn:
                    logger.warning(
                        "command %r references unknown base command %r; "
                        "inherited arguments/opcode will be missing",
                        cmd.name,
                        cmd.base_meta_command_ref,
                    )

        # Resolve container base references
        for container in definition.containers.values():
            if container.base_container_ref:
                container.base_container = definition.containers.get(container.base_container_ref)
                if container.base_container is None and self._warn:
                    logger.warning(
                        "container %r references unknown base container %r; "
                        "inherited entries will be missing",
                        container.name,
                        container.base_container_ref,
                    )

        self._log_inheritance_summary(definition)

    @staticmethod
    def _log_inheritance_summary(definition: XTCEDefinition) -> None:
        """Trace how much inheritance the resolution pass actually wired up."""
        cmds = definition.meta_commands.values()
        with_base = sum(1 for c in cmds if c.base_command is not None)
        with_assign = sum(1 for c in cmds if c.argument_assignments)
        with_base_cont = sum(
            1 for c in definition.containers.values() if c.base_container is not None
        )
        if with_base or with_base_cont:
            logger.info(
                "resolved inheritance: %d command(s) with a base command "
                "(%d fixing inherited args via assignments), "
                "%d container(s) with a base container",
                with_base,
                with_assign,
                with_base_cont,
            )
