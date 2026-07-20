"""Telemetry-side parsing for the XTCE parser: parameter types, parameter
sets, containers, and per-container rates.
"""

import logging
import math
import xml.etree.ElementTree as ET
from typing import Optional

# Import all models from the models module
from xtce_sim.models import (
    Parameter,
    SequenceContainer,
    XTCEDefinition,
)
from xtce_sim.parser.types import PARAMETER_FAMILIES

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
        """Parse ParameterTypeSet and populate definition.parameter_types.

        Walks the family registry in its semantic order (scalars first;
        arrays and aggregates last so their element/member types resolve).
        """
        for family in PARAMETER_FAMILIES:
            for elem in self._findall(param_type_set, family.tag + "ParameterType"):
                param_type = family.parse_parameter(self, elem, definition)
                self._store_type(definition.parameter_types, param_type)

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
