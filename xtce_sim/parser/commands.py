"""Command-side parsing for the XTCE parser: argument types, MetaCommands,
significance, and command containers.
"""

import logging
import xml.etree.ElementTree as ET
from typing import Optional

# Import all models from the models module
from xtce_sim.models import (
    AbsoluteTimeArgumentType,
    AggregateArgumentType,
    AggregateMember,
    AggregateParameterType,
    AlarmRange,
    # Command structures
    Argument,
    # Argument types
    ArrayArgumentType,
    BinaryArgumentType,
    BooleanArgumentType,
    CommandContainer,
    ContainerEntry,
    ContextAlarm,
    ContextMatch,
    # Enums
    DataEncoding,
    EnumeratedArgumentType,
    EnumerationValue,
    FloatArgumentType,
    IntegerArgumentType,
    MetaCommand,
    # Telemetry structures
    RelativeTimeArgumentType,
    StaticAlarmRanges,
    StringArgumentType,
    Unit,
    ValidRange,
    # Top-level
    XTCEDefinition,
)

logger = logging.getLogger("xtce_sim.parser")


class CommandParsingMixin:
    """CommandMetaData: ArgumentTypeSet and MetaCommandSet."""

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

    def _parse_boolean_fields(self, elem: ET.Element) -> tuple[str, str, str, Optional[bool], int]:
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
                logger.info("~ Boolean %r: no encoding declared — defaulted to 1 bit", name)

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

        significance, significance_reason = self._parse_significance(elem, name)

        return MetaCommand(
            name=name,
            description=description,
            abstract=abstract,
            base_meta_command_ref=base_ref,
            arguments=arguments,
            container=container,
            argument_assignments=argument_assignments,
            ancillary_data=cmd_anc_data,
            significance=significance,
            significance_reason=significance_reason,
        )

    # Legal XTCE 1.2 ConsequenceLevelType values (ISO 14950 criticality:
    # normal=D, vital=C, critical=B, forbidden=A; user1 is mission-defined).
    _CONSEQUENCE_LEVELS = ("normal", "vital", "critical", "forbidden", "user1")

    def _parse_significance(
        self, elem: ET.Element, cmd_name: str
    ) -> tuple[Optional[str], Optional[str]]:
        """DefaultSignificance: (consequenceLevel, reasonForWarning) or Nones."""
        sig = self._find(elem, "DefaultSignificance")
        if sig is None:
            return None, None
        # Validate the RAW value: the XSD enum is case-sensitive, so
        # "Critical" is as illegal as "caution" and both deserve a warning.
        raw = self._get_attr(sig, "consequenceLevel", "normal")
        if raw not in self._CONSEQUENCE_LEVELS:
            logger.warning(
                "%s: consequenceLevel %r is not a legal XTCE value %s — normalized to %r and kept",
                cmd_name,
                raw,
                self._CONSEQUENCE_LEVELS,
                raw.lower(),
            )
        reason = self._get_attr(sig, "reasonForWarning") or None
        return raw.lower(), reason

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
