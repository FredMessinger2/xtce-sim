"""Command-side parsing for the XTCE parser: argument types, MetaCommands,
significance, and command containers.
"""

import logging
import xml.etree.ElementTree as ET
from typing import Optional

from xtce_sim.models import (
    # Command structures
    Argument,
    CommandContainer,
    ContainerEntry,
    MetaCommand,
    # Top-level
    XTCEDefinition,
)

# Module import (not from-import) so the family registry is read at call time.
# The tuples are immutable, so extending/reordering means rebinding the module
# attribute — a from-import would freeze the tuple at import time and silently
# ignore the rebinding (the behavior package's VERBS registry is live the same
# way: names resolve to current registry state when the walk runs).
from xtce_sim.parser import types as type_families

logger = logging.getLogger("xtce_sim.parser")


class CommandParsingMixin:
    """CommandMetaData: ArgumentTypeSet and MetaCommandSet."""

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
        """Parse ArgumentTypeSet and populate definition.argument_types.

        Walks the family registry in its semantic order (scalars first;
        arrays and aggregates last so their element/member types resolve).
        """
        for family in type_families.ARGUMENT_FAMILIES:
            for elem in self._findall(arg_type_set, family.tag + "ArgumentType"):
                arg_type = family.parse_argument(self, elem, definition)
                self._store_type(definition.argument_types, arg_type)

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
