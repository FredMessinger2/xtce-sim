"""
XTCE Data Models

Dataclasses representing XTCE (XML Telemetric and Command Exchange) elements
for both commands and telemetry. These models are used by the parser and
generator modules.

Reference: OMG XTCE 1.2/1.3 Specification (https://www.omg.org/spec/XTCE)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# =============================================================================
# ENUMERATIONS
# =============================================================================


class DataEncoding(Enum):
    """Data encoding types supported by XTCE."""

    UNSIGNED = "unsigned"
    TWOS_COMPLEMENT = "twosComplement"
    ONES_COMPLEMENT = "onesComplement"
    SIGN_MAGNITUDE = "signMagnitude"
    IEEE754_1985 = "IEEE754_1985"
    MILSTD_1750A = "MILSTD_1750A"


# =============================================================================
# SUPPORTING TYPES
# =============================================================================


@dataclass
class EnumerationValue:
    """Single enumeration label/value pair."""

    label: str
    value: int


@dataclass
class Unit:
    """
    XTCE Unit definition with full metadata.

    Supports the complete XTCE Unit element including description and power.
    Example: <Unit description="Volts" power="1">V</Unit>
    """

    name: str  # The unit symbol (e.g., "V", "A", "Hz")
    description: Optional[str] = None  # Full name (e.g., "Volts")
    power: Optional[int] = None  # Exponent for derived units (e.g., -1 for "per second")


@dataclass
class AlarmRange:
    """
    Single alarm range for telemetry monitoring.

    Defines threshold boundaries for a single alarm level (warning, critical, etc.).
    """

    min_inclusive: Optional[float] = None
    max_inclusive: Optional[float] = None
    min_exclusive: Optional[float] = None
    max_exclusive: Optional[float] = None


@dataclass
class StaticAlarmRanges:
    """
    Static alarm ranges for telemetry parameters.

    XTCE defines four alarm levels in order of severity:
    - Watch: First level of concern
    - Warning: Approaching limits
    - Distress: Serious concern
    - Critical: Immediate action required
    """

    watch_range: Optional[AlarmRange] = None
    warning_range: Optional[AlarmRange] = None
    distress_range: Optional[AlarmRange] = None
    critical_range: Optional[AlarmRange] = None


@dataclass
class ContextMatch:
    """
    Context match condition for context-dependent alarms.

    Specifies when a particular set of alarm ranges applies based on
    another parameter's value.
    """

    parameter_ref: str  # Parameter to check
    value: str  # Value to match
    comparison: str = "=="  # Comparison operator


@dataclass
class ContextAlarm:
    """
    Context-dependent alarm definition.

    Allows different alarm ranges to apply based on operational mode
    or other parameter values.
    """

    context_match: ContextMatch
    alarm_ranges: StaticAlarmRanges


@dataclass
class AggregateMember:
    """
    Member of an aggregate (struct-like) data type.

    References an existing type definition for the member's data type.
    """

    name: str
    type_ref: str  # Reference to a ParameterType or ArgumentType name
    description: Optional[str] = None


@dataclass
class ValidRange:
    """Valid range constraints for an argument or parameter."""

    min_inclusive: Optional[float] = None
    max_inclusive: Optional[float] = None
    min_exclusive: Optional[float] = None
    max_exclusive: Optional[float] = None


@dataclass
class Calibrator:
    """Calibration for converting raw counts to engineering values.

    Either a polynomial (coefficients) or a piecewise-linear spline
    (points); one of the two lists is populated.
    """

    coefficients: list[tuple[float, int]] = field(default_factory=list)  # (coefficient, exponent)
    spline_points: list[tuple[float, float]] = field(default_factory=list)  # (raw, calibrated)


# =============================================================================
# ARGUMENT TYPES (Commands)
# =============================================================================


@dataclass
class ArgumentType:
    """
    Base class for XTCE argument types.
    ArgumentTypes define the metadata for command arguments including
    data type, encoding, valid range, and units.
    """

    name: str
    size_in_bits: int = 8
    encoding: DataEncoding = DataEncoding.UNSIGNED
    unit: Optional[str] = None
    valid_range: Optional[ValidRange] = None
    description: Optional[str] = None


@dataclass
class IntegerArgumentType(ArgumentType):
    """Integer argument type with signed/unsigned encoding."""

    signed: bool = False


@dataclass
class FloatArgumentType(ArgumentType):
    """Floating point argument type (IEEE754)."""

    encoding: DataEncoding = DataEncoding.IEEE754_1985


@dataclass
class EnumeratedArgumentType(ArgumentType):
    """Enumerated argument type with label/value mappings."""

    enumerations: list[EnumerationValue] = field(default_factory=list)

    def get_value(self, label: str) -> Optional[int]:
        """Get numeric value for enumeration label."""
        for enum in self.enumerations:
            if enum.label == label:
                return enum.value
        return None

    def get_label(self, value: int) -> Optional[str]:
        """Get label for enumeration value."""
        for enum in self.enumerations:
            if enum.value == value:
                return enum.label
        return None


@dataclass
class BinaryArgumentType(ArgumentType):
    """Binary/raw data argument type."""

    pass


@dataclass
class StringArgumentType(ArgumentType):
    """String argument type."""

    max_length: Optional[int] = None


@dataclass
class BooleanArgumentType(ArgumentType):
    """
    Boolean argument type representing true/false values.

    XTCE defines Boolean as a specialized type that can be encoded as:
    - A single bit (0=false, 1=true)
    - An integer with specified true/false values
    - A string with "true"/"false" literals

    The zeroStringValue and oneStringValue attributes define the string
    representation of the boolean values (default: "False"/"True").
    """

    # String representation of boolean values (for display/parsing)
    zero_string_value: str = "False"
    one_string_value: str = "True"
    # Initial value (default state)
    initial_value: Optional[bool] = None
    # Size defaults to 1 bit for boolean
    size_in_bits: int = 1


@dataclass
class ArrayArgumentType(ArgumentType):
    """
    Array argument type containing repeated elements of the same type.

    XTCE arrays can have:
    - Fixed dimensions (known at definition time)
    - Dynamic dimensions (determined at runtime from another parameter)

    The array_type_ref points to the element type definition.
    """

    # Reference to the element type (ArgumentType name)
    array_type_ref: str = ""
    # Resolved element type
    element_type: Optional[ArgumentType] = None
    # Dimension sizes: list of (size, is_dynamic, dynamic_ref) tuples
    # is_dynamic=False: size is a fixed integer
    # is_dynamic=True: size comes from parameter named in dynamic_ref
    dimensions: list[tuple[int, bool, Optional[str]]] = field(default_factory=list)

    def get_total_elements(self) -> Optional[int]:
        """
        Calculate total number of elements for fixed-size arrays.
        Returns None if any dimension is dynamic.
        """
        total = 1
        for size, is_dynamic, _ in self.dimensions:
            if is_dynamic:
                return None
            total *= size
        return total


@dataclass
class AbsoluteTimeArgumentType(ArgumentType):
    """
    Absolute time argument type representing a specific point in time.

    XTCE AbsoluteTime encodes timestamps as an epoch plus offset.
    Common encodings include:
    - CCSDS Day Segmented (CDS): Days since epoch + milliseconds in day
    - CCSDS Unsegmented (CUC): Seconds since epoch
    - Unix time: Seconds since 1970-01-01

    The epoch attribute defines the reference point (e.g., "TAI", "GPS", "UNIX").
    """

    # Reference epoch for the timestamp
    epoch: str = "UNIX"  # Common: "TAI", "GPS", "UNIX", "1958-01-01"
    # Offset from epoch in seconds (for initial value)
    offset_from_epoch: Optional[float] = None
    # Encoding scale (seconds per LSB)
    scale: float = 1.0
    # Encoding offset
    offset: float = 0.0
    # Reference to a ReferenceTime definition
    reference_time_ref: Optional[str] = None


@dataclass
class RelativeTimeArgumentType(ArgumentType):
    """
    Relative time argument type representing a time duration/interval.

    Unlike AbsoluteTime, RelativeTime represents a duration rather than
    a point in time. Used for timeouts, delays, intervals, etc.

    Encoded as a scaled integer or float representing the duration.
    """

    # Offset from reference (for initial value in seconds)
    offset_from: Optional[float] = None
    # Encoding scale (seconds per LSB)
    scale: float = 1.0
    # Encoding offset
    offset: float = 0.0


@dataclass
class AggregateArgumentType(ArgumentType):
    """
    Aggregate (struct-like) argument type for composite command data.

    XTCE 1.3 formalizes aggregate types for representing structured command
    parameters with multiple named members.

    Example uses:
    - Target coordinates (x, y, z)
    - Configuration blocks
    - Complex command payloads

    Example XTCE:
    <AggregateArgumentType name="TargetPositionType">
      <MemberList>
        <Member name="X" typeRef="Float32Type"/>
        <Member name="Y" typeRef="Float32Type"/>
        <Member name="Z" typeRef="Float32Type"/>
      </MemberList>
    </AggregateArgumentType>
    """

    members: list[AggregateMember] = field(default_factory=list)

    def get_member(self, name: str) -> Optional[AggregateMember]:
        """Get member by name."""
        for member in self.members:
            if member.name == name:
                return member
        return None

    def get_total_size(self, type_registry: dict) -> int:
        """
        Calculate total size in bits if all member types are known.
        Returns 0 if any member type is not found.
        """
        total = 0
        for member in self.members:
            member_type = type_registry.get(member.type_ref)
            if member_type is None:
                return 0
            total += member_type.size_in_bits
        return total


# =============================================================================
# COMMAND STRUCTURES
# =============================================================================


@dataclass
class Argument:
    """
    Command argument instance.
    References an ArgumentType and provides the argument name used in the command.
    """

    name: str
    argument_type_ref: str  # Reference to ArgumentType name
    argument_type: Optional[ArgumentType] = None  # Resolved type
    ancillary_data: dict = field(
        default_factory=dict
    )  # XTCE AncillaryData key/value pairs


@dataclass
class ContainerEntry:
    """Entry in a CommandContainer's EntryList."""

    entry_type: str  # 'fixed', 'argument', 'parameter'
    name: str
    size_in_bits: int = 0
    binary_value: Optional[str] = None  # Hex string for fixed values
    argument_ref: Optional[str] = None
    parameter_ref: Optional[str] = None


@dataclass
class CommandContainer:
    """
    Defines the binary packet layout for a command.
    Contains a list of entries (fixed values, arguments, parameters).
    """

    name: str
    entries: list[ContainerEntry] = field(default_factory=list)
    base_container_ref: Optional[str] = None


@dataclass
class MetaCommand:
    """
    XTCE MetaCommand definition.
    Represents a complete command with its arguments and packet structure.
    """

    name: str
    description: Optional[str] = None
    abstract: bool = False
    base_meta_command_ref: Optional[str] = None
    arguments: list[Argument] = field(default_factory=list)
    container: Optional[CommandContainer] = None
    # ArgumentAssignmentList: maps argument name → fixed value for derived commands.
    # When a derived command sets e.g. RW_UNIT_ID=1, that arg is pre-assigned
    # and should not be user-editable.
    argument_assignments: dict[str, str] = field(default_factory=dict)
    ancillary_data: dict[str, str] = field(
        default_factory=dict
    )  # Command-level AncillaryData

    # Resolved references (populated after parsing)
    base_command: Optional["MetaCommand"] = None

    def get_all_arguments(self) -> list[Argument]:
        """Get all arguments including inherited from base command."""
        args = []
        if self.base_command:
            args.extend(self.base_command.get_all_arguments())
        args.extend(self.arguments)
        return args

    def get_all_argument_assignments(self) -> dict[str, str]:
        """Get all argument assignments, including from ancestor commands."""
        assignments = {}
        if self.base_command:
            assignments.update(self.base_command.get_all_argument_assignments())
        assignments.update(self.argument_assignments)
        return assignments

    def _get_abstract_ancestor_arg_names(self) -> set[str]:
        """Collect argument names defined on abstract ancestors.

        Abstract MetaCommands define protocol-level fields (CCSDS header,
        opcode discriminators, etc.) that are managed by the transport layer,
        not by the operator.  These should never appear as user-editable
        parameters regardless of whether they have explicit assignments.
        This approach is vendor-agnostic — it relies on XTCE structure
        (abstract vs concrete) rather than hardcoded field names.
        """
        names: set[str] = set()
        ancestor = self.base_command
        while ancestor:
            if ancestor.abstract:
                names.update(a.name for a in ancestor.arguments)
            ancestor = ancestor.base_command
        return names

    def get_user_arguments(self) -> list[Argument]:
        """Get only arguments that are user-configurable.

        Excludes:
        - Arguments with explicit assignments (e.g. OPCODE set by the
          concrete command's ArgumentAssignmentList).
        - Arguments inherited from abstract base commands (CCSDS header
          fields, opcode discriminators, etc.) which are protocol-managed.
        """
        assigned = self.get_all_argument_assignments()
        abstract_args = self._get_abstract_ancestor_arg_names()
        return [
            a
            for a in self.get_all_arguments()
            if a.name not in assigned and a.name not in abstract_args
        ]


# =============================================================================
# PARAMETER TYPES (Telemetry)
# =============================================================================


@dataclass
class ParameterType:
    """
    Base class for XTCE parameter types (telemetry).
    Similar to ArgumentType but for telemetry parameters.
    """

    name: str
    size_in_bits: int = 8
    encoding: DataEncoding = DataEncoding.UNSIGNED
    unit: Optional[str] = None  # Simple unit string for backward compatibility
    unit_info: Optional[Unit] = None  # Full unit information (XTCE 1.3+)
    valid_range: Optional[ValidRange] = None
    description: Optional[str] = None
    calibrator: Optional[Calibrator] = None
    alarm_ranges: Optional[StaticAlarmRanges] = None  # Static alarm thresholds
    context_alarms: list[ContextAlarm] = field(default_factory=list)  # Context-dependent alarms


@dataclass
class IntegerParameterType(ParameterType):
    """Integer parameter type with signed/unsigned encoding."""

    signed: bool = False


@dataclass
class FloatParameterType(ParameterType):
    """Floating point parameter type."""

    encoding: DataEncoding = DataEncoding.IEEE754_1985


@dataclass
class EnumeratedParameterType(ParameterType):
    """Enumerated parameter type with label/value mappings."""

    enumerations: list[EnumerationValue] = field(default_factory=list)


@dataclass
class StringParameterType(ParameterType):
    """String parameter type."""

    max_length: Optional[int] = None


@dataclass
class BinaryParameterType(ParameterType):
    """Binary/raw data parameter type."""

    pass


@dataclass
class BooleanParameterType(ParameterType):
    """
    Boolean parameter type representing true/false telemetry values.

    Similar to BooleanArgumentType but for telemetry parameters.
    Can include calibration and alarm definitions.
    """

    zero_string_value: str = "False"
    one_string_value: str = "True"
    initial_value: Optional[bool] = None
    size_in_bits: int = 1


@dataclass
class ArrayParameterType(ParameterType):
    """
    Array parameter type containing repeated telemetry elements.

    Used for vector data, multi-channel sensors, memory dumps, etc.
    """

    array_type_ref: str = ""
    element_type: Optional[ParameterType] = None
    dimensions: list[tuple[int, bool, Optional[str]]] = field(default_factory=list)

    def get_total_elements(self) -> Optional[int]:
        """Calculate total number of elements for fixed-size arrays."""
        total = 1
        for size, is_dynamic, _ in self.dimensions:
            if is_dynamic:
                return None
            total *= size
        return total


@dataclass
class AbsoluteTimeParameterType(ParameterType):
    """
    Absolute time parameter type for telemetry timestamps.

    Used for packet timestamps, event times, correlation times, etc.
    """

    epoch: str = "UNIX"
    offset_from_epoch: Optional[float] = None
    scale: float = 1.0
    offset: float = 0.0
    reference_time_ref: Optional[str] = None


@dataclass
class RelativeTimeParameterType(ParameterType):
    """
    Relative time parameter type for duration telemetry.

    Used for uptime counters, elapsed times, intervals, etc.
    """

    offset_from: Optional[float] = None
    scale: float = 1.0
    offset: float = 0.0


@dataclass
class AggregateParameterType(ParameterType):
    """
    Aggregate (struct-like) parameter type for composite telemetry data.

    XTCE 1.3 formalizes aggregate types for representing structured data
    with multiple named members, similar to C structs or protocol buffers.

    Example uses:
    - GPS position (lat, lon, alt)
    - Quaternion (w, x, y, z)
    - Sensor reading with timestamp

    Example XTCE:
    <AggregateParameterType name="GPSPositionType">
      <MemberList>
        <Member name="Latitude" typeRef="Float64Type"/>
        <Member name="Longitude" typeRef="Float64Type"/>
        <Member name="Altitude" typeRef="Float32Type"/>
      </MemberList>
    </AggregateParameterType>
    """

    members: list[AggregateMember] = field(default_factory=list)

    def get_member(self, name: str) -> Optional[AggregateMember]:
        """Get member by name."""
        for member in self.members:
            if member.name == name:
                return member
        return None

    def get_total_size(self, type_registry: dict) -> int:
        """
        Calculate total size in bits if all member types are known.
        Returns 0 if any member type is not found.
        """
        total = 0
        for member in self.members:
            member_type = type_registry.get(member.type_ref)
            if member_type is None:
                return 0
            total += member_type.size_in_bits
        return total


# =============================================================================
# TELEMETRY STRUCTURES
# =============================================================================


@dataclass
class Parameter:
    """
    Telemetry parameter instance.
    References a ParameterType and provides the parameter name.
    """

    name: str
    parameter_type_ref: str
    parameter_type: Optional[ParameterType] = None  # Resolved type
    description: Optional[str] = None


@dataclass
class SequenceContainer:
    """
    XTCE SequenceContainer for telemetry packets.
    Defines the structure of a telemetry packet with its parameters.
    """

    name: str
    description: Optional[str] = None
    entries: list[str] = field(default_factory=list)  # List of parameter refs
    base_container_ref: Optional[str] = None
    restriction_criteria: Optional[dict] = None  # e.g., {"CCSDS_APID": 1}

    # Resolved references
    base_container: Optional["SequenceContainer"] = None

    def get_all_entries(self) -> list[str]:
        """All parameter refs, including those inherited from base containers.

        Walks the ``base_container`` chain (base first, then this container's
        own entries). This is the seam for full container-inheritance support.

        Note: the telemetry packet builder (``generate.build_packets``)
        deliberately does NOT use this — it takes only local ``entries``,
        because inherited base-container parameters are the CCSDS
        header/discriminator fields, which the sim synthesizes itself rather
        than reading from XTCE. Use this only if/when base containers need to
        contribute shared *payload* fields (see build_packets' docstring).
        """
        entries = []
        if self.base_container:
            entries.extend(self.base_container.get_all_entries())
        entries.extend(self.entries)
        return entries


# =============================================================================
# TOP-LEVEL DEFINITION
# =============================================================================


@dataclass
class XTCEDefinition:
    """
    Complete XTCE definition parsed from XML.
    Contains argument types, commands, parameter types, and telemetry containers.
    """

    space_system_name: str
    namespace: str

    # Command definitions
    argument_types: dict[str, ArgumentType] = field(default_factory=dict)
    meta_commands: dict[str, MetaCommand] = field(default_factory=dict)

    # Telemetry definitions
    parameter_types: dict[str, ParameterType] = field(default_factory=dict)
    parameters: dict[str, Parameter] = field(default_factory=dict)
    containers: dict[str, SequenceContainer] = field(default_factory=dict)

    def get_command(self, name: str) -> Optional[MetaCommand]:
        """Get command by name."""
        return self.meta_commands.get(name)

    def get_concrete_commands(self) -> list[MetaCommand]:
        """Get all non-abstract commands."""
        return [cmd for cmd in self.meta_commands.values() if not cmd.abstract]

    def get_telemetry_packets(self) -> list[SequenceContainer]:
        """Concrete telemetry packets: containers with a CCSDS_APID restriction.

        Abstract base containers (which exist only to be inherited from and
        carry no APID of their own) are intentionally excluded — they are not
        packets the sim serves, only a source of a shared APID discriminator.
        """
        return [
            c
            for c in self.containers.values()
            if c.restriction_criteria and "CCSDS_APID" in c.restriction_criteria
        ]

    def merge(self, other: "XTCEDefinition") -> None:
        """Merge another XTCEDefinition into this one (additive).

        Later files win on name collisions — this lets an alarms-only file
        override types from a base file, for example.
        """
        self.argument_types.update(other.argument_types)
        self.meta_commands.update(other.meta_commands)
        self.parameter_types.update(other.parameter_types)
        self.parameters.update(other.parameters)
        self.containers.update(other.containers)
