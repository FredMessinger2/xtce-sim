"""Telemetry-side parsing for the XTCE parser: parameter types, parameter
sets, containers, and per-container rates.
"""

import logging
import math
import xml.etree.ElementTree as ET
from typing import Optional

# Import all models from the models module
from xtce_sim.models import (
    AbsoluteTimeParameterType,
    ArrayParameterType,
    BinaryParameterType,
    BooleanParameterType,
    Calibrator,
    DataEncoding,
    EnumeratedParameterType,
    # Supporting types
    EnumerationValue,
    FloatParameterType,
    IntegerParameterType,
    Parameter,
    # Parameter types
    RelativeTimeParameterType,
    SequenceContainer,
    StringParameterType,
    XTCEDefinition,
)

logger = logging.getLogger("xtce_sim.parser")


class TelemetryParsingMixin:
    """TelemetryMetaData: ParameterTypeSet, ParameterSet, ContainerSet."""

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
        """Parse DefaultCalibrator: PolynomialCalibrator or SplineCalibrator."""
        default_cal = self._find(data_enc, "DefaultCalibrator")
        if default_cal is None:
            return None

        poly_cal = self._find(default_cal, "PolynomialCalibrator")
        if poly_cal is not None:
            coefficients = []
            for term in self._findall(poly_cal, "Term"):
                coef = float(self._get_attr(term, "coefficient", "0"))
                exp = int(self._get_attr(term, "exponent", "0"))
                if exp < 0:
                    # The XTCE schema requires non-negative exponents; a
                    # negative one would make raw=0 packets undefined.
                    logger.warning(
                        "! PolynomialCalibrator term with negative exponent "
                        "%d ignored (schema requires >= 0)",
                        exp,
                    )
                    continue
                coefficients.append((coef, exp))
            if coefficients:
                return Calibrator(coefficients=coefficients)
            return None

        spline_cal = self._find(default_cal, "SplineCalibrator")
        if spline_cal is not None:
            points = []
            for point in self._findall(spline_cal, "SplinePoint"):
                raw = float(self._get_attr(point, "raw", "0"))
                calibrated = float(self._get_attr(point, "calibrated", "0"))
                points.append((raw, calibrated))
            if len(points) >= 2:
                return Calibrator(spline_points=sorted(points))
            return None

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
            rate_per_second=self._parse_rate_in_stream(elem, name),
        )

    def _parse_rate_in_stream(self, elem: ET.Element, name: str) -> Optional[float]:
        """A container's ``<DefaultRateInStream>`` as a per-second rate.

        The standard's ``basis`` defaults to perSecond; a perContainerUpdate
        basis has no meaning for a top-level packet, so it is warned about
        and ignored rather than misread. ``minimumValue`` is the guaranteed
        rate (the one ground systems consume); when it is absent or declares
        no guarantee (``minimumValue="0"``), a positive ``maximumValue`` is
        accepted as the best-effort fallback.
        """
        rate_elem = self._find(elem, "DefaultRateInStream")
        if rate_elem is None:
            return None

        def ignored(detail: str, *fmt) -> None:
            if self._warn:
                logger.warning(
                    "SequenceContainer %r: DefaultRateInStream " + detail + "; rate ignored",
                    name,
                    *fmt,
                )

        basis = rate_elem.get("basis", "perSecond")
        if basis != "perSecond":
            ignored("basis %r not supported (only perSecond)", basis)
            return None
        for attr in ("minimumValue", "maximumValue"):
            value = rate_elem.get(attr)
            if value is None:
                continue
            try:
                rate = float(value)
            except ValueError:
                ignored("%s %r is not a number", attr, value)
                return None
            if rate > 0 and math.isfinite(rate):
                return rate
            # minimumValue="0" is the standard's 'no guaranteed rate' —
            # fall through to maximumValue before giving up.
        ignored("carries no positive finite rate value")
        return None
