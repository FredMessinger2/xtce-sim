"""Shared piece-parsers used by both sides of the XTCE type families.

Sizes, encodings, ranges, alarms, units, calibrators, aggregate members,
and array dimensions — the fragments the argument-type and parameter-type
parsers assemble. Every function takes the parser (``reader``) first; it
provides the namespace-aware element access (_find/_findall/_get_attr)
whose consumption bookkeeping feeds the ignored-element report.
"""

import logging
import xml.etree.ElementTree as ET
from typing import Optional

from xtce_sim.models import (
    AggregateMember,
    AlarmRange,
    Calibrator,
    ContextAlarm,
    ContextMatch,
    StaticAlarmRanges,
    Unit,
    ValidRange,
)

logger = logging.getLogger("xtce_sim.parser")


def _fixed_size_in_bits(reader, size_bits: ET.Element) -> Optional[int]:
    """Read a fixed bit count from a ``<SizeInBits>`` element.

    Accepts either ``<SizeInBits><FixedValue>N</FixedValue></SizeInBits>``
    or the ``<SizeInBits><Fixed><FixedValue>N</FixedValue></Fixed>`` wrapper
    (the form StringDataEncoding uses). Returns None when no numeric fixed
    value is present.
    """
    fixed_val = reader._find(size_bits, "FixedValue")
    if fixed_val is None:
        fixed = reader._find(size_bits, "Fixed")
        if fixed is not None:
            fixed_val = reader._find(fixed, "FixedValue")
    if fixed_val is not None and (fixed_val.text or "").strip().isdigit():
        return int(fixed_val.text)
    return None


def _binary_encoding_size_bits(reader, elem: ET.Element) -> Optional[int]:
    """Fixed bit count from an element's ``<BinaryDataEncoding><SizeInBits>``.

    Returns None when there is no BinaryDataEncoding, no SizeInBits, or no
    numeric fixed value declared.
    """
    bin_enc = reader._find(elem, "BinaryDataEncoding")
    if bin_enc is None:
        return None
    size_bits = reader._find(bin_enc, "SizeInBits")
    if size_bits is None:
        return None
    return _fixed_size_in_bits(reader, size_bits)


def _binary_size_in_bits(reader, elem: ET.Element) -> int:
    """Fixed size (in bits) of a binary type's BinaryDataEncoding.

    Prefers ``<BinaryDataEncoding><SizeInBits>`` (with or without the
    ``<Fixed>`` wrapper) and falls back to a legacy ``sizeInBits``
    attribute. Returns 0 if no size is declared.
    """
    bits = _binary_encoding_size_bits(reader, elem)
    if bits is not None:
        return bits
    attr = reader._get_attr(elem, "sizeInBits").strip()
    if attr.isdigit():
        logger.info(
            "~ %r: binary size %s bits taken from legacy sizeInBits "
            "attribute (no BinaryDataEncoding/SizeInBits element)",
            reader._get_attr(elem, "name"),
            attr,
        )
        return int(attr)
    return 0


# Charsets the codec actually honors: it always encodes UTF-8, of which
# US-ASCII is a byte-identical subset. Anything else (UTF-16, ...) would
# be silently mis-encoded, so declaring it draws a parse-time warning.
_SUPPORTED_STRING_CHARSETS = ("UTF-8", "US-ASCII", "ASCII")


def _string_size_and_length(reader, elem: ET.Element) -> tuple[int, Optional[int]]:
    """Fixed size and byte length from a String type's StringDataEncoding.

    Returns ``(size_in_bits, max_length)`` where max_length is
    ``size_in_bits // 8``; ``(0, None)`` when no fixed size is declared.
    Warns when the declared charset is one the codec does not honor.
    """
    str_enc = reader._find(elem, "StringDataEncoding")
    if str_enc is None:
        return 0, None
    charset = reader._get_attr(str_enc, "encoding", "UTF-8")
    if charset.upper() not in _SUPPORTED_STRING_CHARSETS:
        # Deliberately NOT gated on reader._warn: unlike base-ref warnings,
        # an unsupported charset cannot become valid after a multi-file
        # merge, and each type element is parsed exactly once — so this
        # must fire even during parse_multiple's intermediate parses.
        logger.warning(
            "string type %r declares encoding %r; xtce-sim encodes UTF-8 "
            "only, so values will not match this declaration",
            reader._get_attr(elem, "name"),
            charset,
        )
    size_bits = reader._find(str_enc, "SizeInBits")
    if size_bits is None:
        return 0, None
    bits = _fixed_size_in_bits(reader, size_bits)
    if bits is None:
        return 0, None
    return bits, bits // 8


def _parse_boolean_fields(reader, elem: ET.Element) -> tuple[str, str, str, Optional[bool], int]:
    """Shared parsing for Boolean argument/parameter types.

    Returns ``(name, zero_string, one_string, initial_value, size_in_bits)``.
    XTCE Boolean types specify zero/one display strings, an optional
    initial "true"/"false", and a bit size (default 1). An
    IntegerDataEncoding ``sizeInBits`` takes precedence; otherwise a
    BinaryDataEncoding fixed size is used if present.
    """
    name = reader._get_attr(elem, "name")
    zero_str = reader._get_attr(elem, "zeroStringValue", "False")
    one_str = reader._get_attr(elem, "oneStringValue", "True")

    initial_value = None
    init_str = reader._get_attr(elem, "initialValue", "")
    if init_str.lower() == "true":
        initial_value = True
    elif init_str.lower() == "false":
        initial_value = False

    size_in_bits = 1
    data_enc = reader._find(elem, "IntegerDataEncoding")
    if data_enc is not None:
        size_in_bits = int(reader._get_attr(data_enc, "sizeInBits", "1"))
    else:
        bits = _binary_encoding_size_bits(reader, elem)
        if bits is not None:
            size_in_bits = bits
        else:
            logger.info("~ Boolean %r: no encoding declared — defaulted to 1 bit", name)

    return name, zero_str, one_str, initial_value, size_in_bits


def _parse_valid_range(reader, elem: ET.Element) -> Optional[ValidRange]:
    """Parse ValidRange element from argument type."""
    # Check for ValidRange directly
    vr = reader._find(elem, "ValidRange")
    if vr is None:
        # Check for ValidRangeSet
        vr_set = reader._find(elem, "ValidRangeSet")
        if vr_set is not None:
            vr = reader._find(vr_set, "ValidRange")

    if vr is None:
        return None

    valid_range = ValidRange()

    min_inc = reader._get_attr(vr, "minInclusive")
    if min_inc:
        valid_range.min_inclusive = float(min_inc)

    max_inc = reader._get_attr(vr, "maxInclusive")
    if max_inc:
        valid_range.max_inclusive = float(max_inc)

    min_exc = reader._get_attr(vr, "minExclusive")
    if min_exc:
        valid_range.min_exclusive = float(min_exc)

    max_exc = reader._get_attr(vr, "maxExclusive")
    if max_exc:
        valid_range.max_exclusive = float(max_exc)

    return valid_range


def _parse_alarm_range(reader, elem: ET.Element) -> AlarmRange:
    """Parse a single alarm range element (WarningRange, CriticalRange, etc.)."""
    alarm = AlarmRange()

    min_inc = reader._get_attr(elem, "minInclusive")
    if min_inc:
        alarm.min_inclusive = float(min_inc)

    max_inc = reader._get_attr(elem, "maxInclusive")
    if max_inc:
        alarm.max_inclusive = float(max_inc)

    min_exc = reader._get_attr(elem, "minExclusive")
    if min_exc:
        alarm.min_exclusive = float(min_exc)

    max_exc = reader._get_attr(elem, "maxExclusive")
    if max_exc:
        alarm.max_exclusive = float(max_exc)

    return alarm


def _parse_static_alarm_ranges(reader, elem: ET.Element) -> Optional[StaticAlarmRanges]:
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
    alarm_elem = reader._find(elem, "StaticAlarmRanges")
    if alarm_elem is None:
        default_alarm = reader._find(elem, "DefaultAlarm")
        if default_alarm is not None:
            alarm_elem = reader._find(default_alarm, "StaticAlarmRanges")
    if alarm_elem is None:
        return None

    alarms = StaticAlarmRanges()

    # Parse WatchRange (lowest severity)
    watch = reader._find(alarm_elem, "WatchRange")
    if watch is not None:
        alarms.watch_range = _parse_alarm_range(reader, watch)

    # Parse WarningRange
    warning = reader._find(alarm_elem, "WarningRange")
    if warning is not None:
        alarms.warning_range = _parse_alarm_range(reader, warning)

    # Parse DistressRange
    distress = reader._find(alarm_elem, "DistressRange")
    if distress is not None:
        alarms.distress_range = _parse_alarm_range(reader, distress)

    # Parse CriticalRange
    critical = reader._find(alarm_elem, "CriticalRange")
    if critical is not None:
        alarms.critical_range = _parse_alarm_range(reader, critical)

    return alarms


def _parse_context_alarm_list(reader, elem: ET.Element) -> list[ContextAlarm]:
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
    context_list_elem = reader._find(elem, "ContextAlarmList")
    if context_list_elem is None:
        return []

    context_alarms = []
    for context_alarm_elem in reader._findall(context_list_elem, "ContextAlarm"):
        # Parse ContextMatch
        match_elem = reader._find(context_alarm_elem, "ContextMatch")
        if match_elem is None:
            continue

        # Look for Comparison element
        comparison_elem = reader._find(match_elem, "Comparison")
        if comparison_elem is None:
            continue

        param_ref = reader._strip_path_ref(reader._get_attr(comparison_elem, "parameterRef"))
        value = reader._get_attr(comparison_elem, "value")
        # Default comparison is equality
        comparison_op = reader._get_attr(comparison_elem, "comparisonOperator", "==")

        context_match = ContextMatch(parameter_ref=param_ref, value=value, comparison=comparison_op)

        # Parse the alarm ranges for this context
        alarm_ranges = _parse_static_alarm_ranges(reader, context_alarm_elem)
        if alarm_ranges is None:
            alarm_ranges = StaticAlarmRanges()

        context_alarms.append(ContextAlarm(context_match=context_match, alarm_ranges=alarm_ranges))

    return context_alarms


def _parse_unit_set_enhanced(reader, elem: ET.Element) -> tuple[Optional[str], Optional[Unit]]:
    """
    Parse UnitSet with full XTCE 1.3 support.

    Returns tuple of (simple_unit_string, full_unit_info).
    The simple string maintains backward compatibility.

    Example XTCE:
    <UnitSet>
      <Unit description="Volts" power="1">V</Unit>
    </UnitSet>
    """
    unit_set = reader._find(elem, "UnitSet")
    if unit_set is None:
        return (None, None)

    unit_elem = reader._find(unit_set, "Unit")
    if unit_elem is None or not unit_elem.text:
        return (None, None)

    # Simple string for backward compatibility
    simple_unit = unit_elem.text.strip()

    # Full Unit object with all metadata
    description = reader._get_attr(unit_elem, "description")
    power_str = reader._get_attr(unit_elem, "power")
    power = int(power_str) if power_str else None

    unit_info = Unit(
        name=simple_unit, description=description if description else None, power=power
    )

    return (simple_unit, unit_info)


def _parse_aggregate_members(reader, elem: ET.Element) -> list[AggregateMember]:
    """
    Parse MemberList for aggregate types.

    Example XTCE:
    <MemberList>
      <Member name="Latitude" typeRef="Float64Type"/>
      <Member name="Longitude" typeRef="Float64Type"/>
    </MemberList>
    """
    members = []
    member_list = reader._find(elem, "MemberList")
    if member_list is None:
        return members

    for member_elem in reader._findall(member_list, "Member"):
        name = reader._get_attr(member_elem, "name")
        type_ref = reader._strip_path_ref(reader._get_attr(member_elem, "typeRef"))
        description = reader._get_attr(member_elem, "shortDescription")

        if name and type_ref:
            members.append(
                AggregateMember(
                    name=name,
                    type_ref=type_ref,
                    description=description if description else None,
                )
            )

    return members


def _parse_index_element(reader, idx_elem: ET.Element) -> tuple[int, Optional[str]]:
    """Read one array-dimension index (``<StartingIndex>``/``<EndingIndex>``).

    Returns ``(index, dynamic_ref)``: the fixed index value (0 if not
    given) and, when the index is a DynamicValue/ParameterInstanceRef, the
    referenced parameter name — else None.
    """
    index = 0
    fixed = reader._find(idx_elem, "FixedValue")
    if fixed is not None and fixed.text:
        index = int(fixed.text)
    dynamic_ref = None
    dyn_val = reader._find(idx_elem, "DynamicValue")
    if dyn_val is not None:
        param_ref = reader._find(dyn_val, "ParameterInstanceRef")
        if param_ref is not None:
            dynamic_ref = reader._strip_path_ref(reader._get_attr(param_ref, "parameterRef"))
    return index, dynamic_ref


def _parse_dimension(reader, dim: ET.Element) -> tuple[int, bool, Optional[str]]:
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
    start_elem = reader._find(dim, "StartingIndex")
    if start_elem is not None:
        start_idx, ref = _parse_index_element(reader, start_elem)
        if ref is not None:
            is_dynamic = True
            dynamic_ref = ref

    end_elem = reader._find(dim, "EndingIndex")
    if end_elem is not None:
        end_idx, ref = _parse_index_element(reader, end_elem)
        if ref is not None:
            is_dynamic = True
            dynamic_ref = ref

    # Size is end - start + 1 (inclusive range)
    size = end_idx - start_idx + 1 if not is_dynamic else 0

    return (size, is_dynamic, dynamic_ref)


def _parse_calibrator(reader, data_enc: ET.Element) -> Optional[Calibrator]:
    """Parse DefaultCalibrator: PolynomialCalibrator or SplineCalibrator."""
    default_cal = reader._find(data_enc, "DefaultCalibrator")
    if default_cal is None:
        return None

    poly_cal = reader._find(default_cal, "PolynomialCalibrator")
    if poly_cal is not None:
        coefficients = []
        for term in reader._findall(poly_cal, "Term"):
            coef = float(reader._get_attr(term, "coefficient", "0"))
            exp = int(reader._get_attr(term, "exponent", "0"))
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

    spline_cal = reader._find(default_cal, "SplineCalibrator")
    if spline_cal is not None:
        points = []
        for point in reader._findall(spline_cal, "SplinePoint"):
            raw = float(reader._get_attr(point, "raw", "0"))
            calibrated = float(reader._get_attr(point, "calibrated", "0"))
            points.append((raw, calibrated))
        if len(points) >= 2:
            return Calibrator(spline_points=sorted(points))
        return None

    return None
