"""The XTCE parser's entry points and document walk."""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from xtce_sim.models import XTCEDefinition
from xtce_sim.parser.commands import CommandParsingMixin
from xtce_sim.parser.reader import ReaderMixin
from xtce_sim.parser.resolve import ResolutionMixin
from xtce_sim.parser.telemetry import TelemetryParsingMixin

logger = logging.getLogger("xtce_sim.parser")


class XTCEParser(CommandParsingMixin, TelemetryParsingMixin, ResolutionMixin, ReaderMixin):
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
