"""
XTCE Parser package

Parses XTCE (XML Telemetric and Command Exchange) files to extract
command and telemetry definitions. Supports XTCE 1.2 and 1.3 standard elements
including CommandMetaData, MetaCommandSet, ArgumentTypeSet, TelemetryMetaData,
StaticAlarmRanges, ContextAlarms, and AggregateDataTypes.

Reference: OMG XTCE 1.2/1.3 Specification (https://www.omg.org/spec/XTCE)
"""

from xtce_sim.parser.core import XTCEParser
from xtce_sim.parser.reader import XTCE_NAMESPACES

__all__ = ["XTCEParser", "XTCE_NAMESPACES"]
