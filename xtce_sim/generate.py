"""
Build a resolved `SimDefinition` from a parsed XTCE definition, and dump it.

Instead of emitting importable Python modules, this builds plain in-memory
objects the simulator consumes directly. XTCE is the single source of truth:
opcodes come from FixedValueEntry elements (synthetic opcodes are assigned from
0xC0 upward for commands that lack one), and telemetry APIDs come from container
RestrictionCriteria.

Three output formats are provided:
- `format_text`   — human-readable summary (cmd_tlm.txt)
- `format_json`   — machine-readable dump (cmd_tlm.json)
- `emit_python`   — optional standalone importable snapshot (generated.py)
"""

from __future__ import annotations

import json
import math
from typing import Optional

from xtce_sim.definition import (
    CommandDef,
    FieldInfo,
    PacketDef,
    ParamInfo,
    SimDefinition,
)
from xtce_sim.models import (
    AbsoluteTimeArgumentType,
    AbsoluteTimeParameterType,
    AggregateParameterType,
    ArgumentType,
    BinaryArgumentType,
    BinaryParameterType,
    BooleanArgumentType,
    BooleanParameterType,
    DataEncoding,
    EnumeratedArgumentType,
    EnumeratedParameterType,
    FloatArgumentType,
    FloatParameterType,
    IntegerArgumentType,
    IntegerParameterType,
    MetaCommand,
    ParameterType,
    RelativeTimeArgumentType,
    RelativeTimeParameterType,
    StringArgumentType,
    StringParameterType,
    XTCEDefinition,
)

# Synthetic opcodes are handed out from here upward for commands whose XTCE
# has no FixedValueEntry opcode, so a given XTCE always yields the same
# opcode assignment.
SYNTHETIC_OPCODE_BASE = 0xC0
MAX_OPCODE = 0xFF  # opcode is a single byte

# Integer encodings that represent signed values.
_SIGNED_ENCODINGS = (
    DataEncoding.TWOS_COMPLEMENT,
    DataEncoding.ONES_COMPLEMENT,
    DataEncoding.SIGN_MAGNITUDE,
)
# All integer wire encodings (a calibrated float may carry one of these).
_INTEGER_ENCODINGS = (DataEncoding.UNSIGNED,) + _SIGNED_ENCODINGS

# Big-endian struct format characters by python_type.
_STRUCT_FORMAT_MAP = {
    "uint8": "B",
    "int8": "b",
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
    "int32": "i",
    "uint64": "Q",
    "int64": "q",
    "float32": "f",
    "float64": "d",
}


class GeneratorError(Exception):
    """Raised when a definition can't be turned into a valid SimDefinition."""


# =============================================================================
# TYPE MAPPING
# =============================================================================


def _is_signed(encoding: DataEncoding, signed_flag: bool = False) -> bool:
    return signed_flag or encoding in _SIGNED_ENCODINGS


def _int_type(size_in_bits: int, signed: bool) -> str:
    """Map a bit size to the smallest python_type that holds it (up to 64-bit)."""
    if size_in_bits <= 8:
        return "int8" if signed else "uint8"
    elif size_in_bits <= 16:
        return "int16" if signed else "uint16"
    elif size_in_bits <= 32:
        return "int32" if signed else "uint32"
    return "int64" if signed else "uint64"


def python_type_for_param(ptype: ParameterType) -> str:
    """Map an XTCE parameter type to a struct-friendly python_type string."""
    if isinstance(ptype, IntegerParameterType):
        return _int_type(ptype.size_in_bits, _is_signed(ptype.encoding, ptype.signed))
    if isinstance(ptype, FloatParameterType):
        # Calibrated types carry an integer wire encoding; keep the raw int type.
        if ptype.encoding in _INTEGER_ENCODINGS:
            return _int_type(ptype.size_in_bits, _is_signed(ptype.encoding))
        return "float32" if ptype.size_in_bits <= 32 else "float64"
    if isinstance(ptype, EnumeratedParameterType):
        return _int_type(ptype.size_in_bits, signed=False)
    if isinstance(ptype, BooleanParameterType):
        return _int_type(ptype.size_in_bits, signed=False)
    if isinstance(ptype, (AbsoluteTimeParameterType, RelativeTimeParameterType)):
        # Parsed with an integer wire encoding (scale/offset applied downstream).
        return _int_type(ptype.size_in_bits, signed=False)
    if isinstance(ptype, StringParameterType):
        return "string"
    if isinstance(ptype, BinaryParameterType):
        return "bytes"  # fixed-size raw blob sized from size_in_bits
    return "uint8"


def python_type_for_arg(atype: ArgumentType) -> str:
    """Map an XTCE argument type to a struct-friendly python_type string."""
    if isinstance(atype, IntegerArgumentType):
        return _int_type(atype.size_in_bits, _is_signed(atype.encoding, atype.signed))
    if isinstance(atype, FloatArgumentType):
        return "float32" if atype.size_in_bits <= 32 else "float64"
    if isinstance(atype, EnumeratedArgumentType):
        return _int_type(atype.size_in_bits, signed=False)
    if isinstance(atype, BooleanArgumentType):
        return _int_type(atype.size_in_bits, signed=False)
    if isinstance(atype, (AbsoluteTimeArgumentType, RelativeTimeArgumentType)):
        return _int_type(atype.size_in_bits, signed=False)
    if isinstance(atype, StringArgumentType):
        return "string"
    if isinstance(atype, BinaryArgumentType):
        return "bytes"  # fixed-size raw blob sized from size_in_bits
    return "uint8"


def fields_to_struct_format(fields: list[FieldInfo]) -> str:
    """Build a big-endian struct format string for a list of telemetry fields."""
    fmt = ">"
    for f in fields:
        # Strings and raw binary blobs pack as N bytes ("Ns"); scalars use their
        # struct char. A 0-bit blob (variable-length) becomes "0s" — zero bytes
        # in the fixed layout; true variable-length handling is a later feature.
        if f.python_type in ("string", "bytes"):
            fmt += f"{f.size_bits // 8}s"
        else:
            fmt += _STRUCT_FORMAT_MAP.get(f.python_type, "B")
    return fmt


# =============================================================================
# OPCODE EXTRACTION
# =============================================================================

# CCSDS primary-header field names that are also FixedValueEntry elements but
# are framing, not the command opcode.
_CCSDS_HEADER_NAMES = {
    "versiontype",
    "packettype",
    "sechdrflag",
    "seqflags",
    "seqcount",
    "packetlength",
    "version",
    "type",
    "apid",
}


def _fixed_hex(entry) -> Optional[int]:
    """Parse a fixed entry's ``binary_value`` as hex; None if it isn't valid hex."""
    try:
        return int(entry.binary_value, 16)
    except ValueError:
        return None


def _first_fixed_hex(entries, matches) -> Optional[int]:
    """First valid hex value among fixed entries (with a binary value) that
    satisfy the *matches* predicate; None if none do."""
    for entry in entries:
        if entry.entry_type != "fixed" or not entry.binary_value:
            continue
        if not matches(entry):
            continue
        value = _fixed_hex(entry)
        if value is not None:
            return value
    return None


def extract_opcode(cmd: MetaCommand) -> Optional[int]:
    """Extract a command's opcode from its container's FixedValueEntry.

    Prefers an entry named 'opcode' (case-insensitive), then falls back to any
    8-bit fixed value that is not a CCSDS header field. Returns None if the
    command has no explicit opcode (a synthetic one will be assigned).
    """
    if not cmd.container:
        return None
    entries = cmd.container.entries

    # Prefer an entry explicitly named 'opcode'.
    named = _first_fixed_hex(entries, lambda e: "opcode" in (e.name or "").lower())
    if named is not None:
        return named

    # Fall back to any non-header 8-bit fixed value.
    return _first_fixed_hex(
        entries,
        lambda e: e.size_in_bits == 8
        and (e.name or "").lower().replace("_", "") not in _CCSDS_HEADER_NAMES,
    )


# =============================================================================
# BUILD
# =============================================================================


def _param_info_for_arg(arg) -> ParamInfo:
    """Build a ParamInfo from a resolved command Argument."""
    atype = arg.argument_type
    if atype is None:
        # Unresolved type — minimal fallback.
        return ParamInfo(name=arg.name, size_bits=8, python_type="uint8")

    valid_min = atype.valid_range.min_inclusive if atype.valid_range else None
    valid_max = atype.valid_range.max_inclusive if atype.valid_range else None

    enums = None
    if isinstance(atype, EnumeratedArgumentType) and atype.enumerations:
        enums = {e.label: e.value for e in atype.enumerations}

    desc = atype.description or None
    if desc == arg.name:
        desc = None

    return ParamInfo(
        name=arg.name,
        size_bits=atype.size_in_bits,
        python_type=python_type_for_arg(atype),
        unit=atype.unit or None,
        description=desc,
        valid_min=valid_min,
        valid_max=valid_max,
        enumerations=enums,
    )


def build_commands(xtce_def: XTCEDefinition) -> list[CommandDef]:
    """Build the concrete command list with opcodes and user parameters.

    Real opcodes come from each command's FixedValueEntry. Commands that lack
    one get a synthetic opcode from SYNTHETIC_OPCODE_BASE upward, skipping any
    value already claimed by a real opcode (or an earlier synthetic) so every
    command dispatches to a distinct opcode. Result is sorted by opcode.
    """
    concrete = xtce_def.get_concrete_commands()

    # First pass: resolve real opcodes and reserve them, rejecting out-of-range
    # values and collisions between two real commands (either would make one
    # command undispatchable).
    real_opcodes: dict[str, Optional[int]] = {}
    taken: set[int] = set()
    for cmd in concrete:
        opcode = extract_opcode(cmd)
        real_opcodes[cmd.name] = opcode
        if opcode is not None:
            if not 0 <= opcode <= MAX_OPCODE:
                raise GeneratorError(
                    f"command {cmd.name!r} has opcode 0x{opcode:X} outside 0x00–0xFF"
                )
            if opcode in taken:
                raise GeneratorError(
                    f"duplicate opcode 0x{opcode:02X} (command {cmd.name!r} collides "
                    "with an earlier command)"
                )
            taken.add(opcode)

    # Second pass: assign collision-free synthetic opcodes in definition order.
    next_synthetic = SYNTHETIC_OPCODE_BASE
    commands: list[CommandDef] = []
    for cmd in concrete:
        opcode = real_opcodes[cmd.name]
        synthetic = opcode is None
        if synthetic:
            while next_synthetic in taken:
                next_synthetic += 1
            if next_synthetic > MAX_OPCODE:
                raise GeneratorError(
                    "ran out of synthetic opcodes (0x{:02X}–0xFF exhausted); too many "
                    "commands without an explicit opcode".format(SYNTHETIC_OPCODE_BASE)
                )
            opcode = next_synthetic
            taken.add(opcode)
            next_synthetic += 1

        params = [_param_info_for_arg(arg) for arg in cmd.get_user_arguments()]

        commands.append(
            CommandDef(
                name=cmd.name,
                opcode=opcode,
                description=cmd.description or None,
                synthetic=synthetic,
                params=params,
            )
        )

    commands.sort(key=lambda c: c.opcode)
    return commands


def _fields_for_param(param, xtce_def: XTCEDefinition) -> list[FieldInfo]:
    """Build FieldInfo(s) for one telemetry parameter.

    Aggregate (struct-like) parameters are flattened into one field per member,
    with names prefixed by the aggregate name (e.g. GPSPosition_Latitude).
    """
    ptype = param.parameter_type
    if ptype is None:
        return [FieldInfo(name=param.name, size_bits=32, python_type="uint32")]

    if isinstance(ptype, AggregateParameterType):
        fields: list[FieldInfo] = []
        for member in ptype.members:
            member_type = xtce_def.parameter_types.get(member.type_ref)
            if member_type is None:
                fields.append(
                    FieldInfo(name=f"{param.name}_{member.name}", size_bits=32, python_type="uint32")
                )
                continue
            fields.append(
                FieldInfo(
                    name=f"{param.name}_{member.name}",
                    size_bits=member_type.size_in_bits,
                    python_type=python_type_for_param(member_type),
                    unit=member_type.unit or None,
                    description=member.description or member_type.description or None,
                )
            )
        return fields

    desc = ptype.description or None
    if desc == param.name:
        desc = None
    return [
        FieldInfo(
            name=param.name,
            size_bits=ptype.size_in_bits,
            python_type=python_type_for_param(ptype),
            unit=ptype.unit or None,
            description=desc,
        )
    ]


def build_packets(xtce_def: XTCEDefinition) -> list[PacketDef]:
    """Build the telemetry packet list (payload fields + struct format).

    Two packets sharing an APID are rejected: the sim dispatches telemetry by
    APID, so a duplicate makes one packet unreachable.
    """
    packets: list[PacketDef] = []
    seen_apids: dict[int, str] = {}

    for container in xtce_def.get_telemetry_packets():
        apid = container.restriction_criteria.get("CCSDS_APID", 0)
        if apid in seen_apids:
            raise GeneratorError(
                f"duplicate APID 0x{apid:X}: packets {seen_apids[apid]!r} and "
                f"{container.name!r} collide"
            )
        seen_apids[apid] = container.name

        fields: list[FieldInfo] = []
        for param_ref in container.entries:
            param = xtce_def.parameters.get(param_ref)
            if param:
                fields.extend(_fields_for_param(param, xtce_def))

        packets.append(
            PacketDef(
                name=container.name,
                apid=apid,
                description=container.description or None,
                fields=fields,
                struct_format=fields_to_struct_format(fields),
            )
        )

    packets.sort(key=lambda p: p.apid)
    return packets


def build_sim_definition(xtce_def: XTCEDefinition) -> SimDefinition:
    """Build a fully resolved SimDefinition from a parsed XTCE definition."""
    return SimDefinition(
        space_system_name=xtce_def.space_system_name,
        commands=build_commands(xtce_def),
        packets=build_packets(xtce_def),
    )


# =============================================================================
# OUTPUT: TEXT
# =============================================================================


def format_text(simdef: SimDefinition) -> str:
    """Render a human-readable summary of the definition."""
    lines: list[str] = []
    lines.append("XTCE Simulator Definition")
    lines.append("=" * 60)
    lines.append(f"Space system : {simdef.space_system_name}")
    lines.append(f"Commands     : {len(simdef.commands)}")
    lines.append(f"Telemetry    : {len(simdef.packets)} packet(s)")
    lines.append("")

    lines.append("COMMANDS")
    lines.append("-" * 60)
    for cmd in simdef.commands:
        tag = " (synthetic opcode)" if cmd.synthetic else ""
        header = f"0x{cmd.opcode:02X}  {cmd.name}{tag}"
        if cmd.description:
            header += f"  — {cmd.description}"
        lines.append(header)
        for p in cmd.params:
            detail = f"      {p.name:<24} {p.python_type:<8} {p.size_bits}b"
            if p.unit:
                detail += f"  [{p.unit}]"
            if p.valid_min is not None or p.valid_max is not None:
                detail += f"  range({p.valid_min}..{p.valid_max})"
            if p.enumerations:
                detail += f"  enum={p.enumerations}"
            if p.description:
                detail += f"  — {p.description}"
            lines.append(detail)
        if not cmd.params:
            lines.append("      (no parameters)")
        lines.append("")

    lines.append("TELEMETRY")
    lines.append("-" * 60)
    for pkt in simdef.packets:
        header = f"APID 0x{pkt.apid:02X}  {pkt.name}"
        if pkt.description:
            header += f"  — {pkt.description}"
        lines.append(header)
        lines.append(f"      struct: {pkt.struct_format}")
        for f in pkt.fields:
            detail = f"      {f.name:<24} {f.python_type:<8} {f.size_bits}b"
            if f.unit:
                detail += f"  [{f.unit}]"
            if f.description:
                detail += f"  — {f.description}"
            lines.append(detail)
        if not pkt.fields:
            lines.append("      (no fields)")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# OUTPUT: JSON
# =============================================================================


def _json_num(value):
    """Drop non-finite numbers so the JSON stays valid (json emits bare NaN/inf)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def to_dict(simdef: SimDefinition) -> dict:
    """Build a JSON-serializable dict for the definition."""
    return {
        "space_system": simdef.space_system_name,
        "commands": [
            {
                "name": c.name,
                "opcode": c.opcode,
                "opcode_hex": f"0x{c.opcode:02X}",
                "synthetic_opcode": c.synthetic,
                "description": c.description,
                "params": [
                    {
                        "name": p.name,
                        "size_bits": p.size_bits,
                        "python_type": p.python_type,
                        "unit": p.unit,
                        "description": p.description,
                        "valid_min": _json_num(p.valid_min),
                        "valid_max": _json_num(p.valid_max),
                        "enumerations": p.enumerations,
                    }
                    for p in c.params
                ],
            }
            for c in simdef.commands
        ],
        "telemetry": [
            {
                "name": pkt.name,
                "apid": pkt.apid,
                "apid_hex": f"0x{pkt.apid:02X}",
                "description": pkt.description,
                "struct_format": pkt.struct_format,
                "fields": [
                    {
                        "name": f.name,
                        "size_bits": f.size_bits,
                        "python_type": f.python_type,
                        "unit": f.unit,
                        "description": f.description,
                    }
                    for f in pkt.fields
                ],
            }
            for pkt in simdef.packets
        ],
    }


def format_json(simdef: SimDefinition) -> str:
    """Render the definition as pretty-printed JSON."""
    return json.dumps(to_dict(simdef), indent=2) + "\n"


# =============================================================================
# OUTPUT: PYTHON SNAPSHOT (optional, --emit-py)
# =============================================================================


def _is_enum_reserved(ident: str) -> bool:
    """Names Enum forbids as members: 'mro' and single-underscore 'sunder' forms."""
    if ident == "mro":
        return True
    # _sunder_ : one leading and one trailing underscore (but not __dunder__).
    return (
        len(ident) > 2
        and ident[0] == "_"
        and ident[-1] == "_"
        and ident[1] != "_"
        and ident[-2] != "_"
    )


def _py_identifier(name: str, used: set[str]) -> str:
    """Coerce an XTCE name into a unique, valid Python identifier.

    Non-identifier characters become underscores; leading digits, Python
    keywords, and Enum-reserved forms are suffixed; collisions get a numeric
    suffix. Keeps the emitted snapshot importable regardless of how exotic the
    source names are.
    """
    import keyword

    ident = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not ident or ident[0].isdigit():
        ident = f"_{ident}"
    if keyword.iskeyword(ident) or _is_enum_reserved(ident):
        ident = f"{ident}_"

    candidate = ident
    n = 2
    while candidate in used:
        candidate = f"{ident}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def _comment_safe(text: str) -> str:
    """Flatten a description to a single safe inline-comment line."""
    return " ".join(text.split()).replace('"', "'")


def emit_python(simdef: SimDefinition) -> str:
    """Render a standalone importable snapshot module (generated.py).

    This is an inspection artifact only — the simulator never imports it. It
    provides CommandOpcode / TelemetryAPID IntEnums plus COMMAND_PARAMS,
    PACKET_FIELDS and PACKET_FORMATS mappings for scripting against the sim.
    Command/packet names are sanitized to valid, unique identifiers so the
    module always compiles.
    """
    # Stable name -> identifier maps shared by the enum defs and the dict keys.
    cmd_ident: dict[str, str] = {}
    _used_cmd: set[str] = set()
    for c in simdef.commands:
        cmd_ident[c.name] = _py_identifier(c.name, _used_cmd)
    pkt_ident: dict[str, str] = {}
    _used_pkt: set[str] = set()
    for pkt in simdef.packets:
        pkt_ident[pkt.name] = _py_identifier(pkt.name, _used_pkt)
    lines: list[str] = [
        '"""',
        f"Generated snapshot of the {simdef.space_system_name} XTCE definition.",
        "",
        "DO NOT EDIT — regenerate with `xtce-sim generate --emit-py`.",
        "This module is an inspection/scripting aid; the simulator does not import it.",
        '"""',
        "",
        "from dataclasses import dataclass",
        "from enum import IntEnum",
        "from typing import Optional",
        "",
        "",
        "@dataclass",
        "class ParamInfo:",
        "    name: str",
        "    size_bits: int",
        "    python_type: str",
        "    unit: Optional[str] = None",
        "    description: Optional[str] = None",
        "    valid_min: Optional[float] = None",
        "    valid_max: Optional[float] = None",
        "    enumerations: Optional[dict] = None",
        "",
        "",
        "@dataclass",
        "class FieldInfo:",
        "    name: str",
        "    size_bits: int",
        "    python_type: str",
        "    unit: Optional[str] = None",
        "    description: Optional[str] = None",
        "",
        "",
        "class CommandOpcode(IntEnum):",
        '    """Command opcodes from XTCE (synthetic opcodes from 0xC0 upward)."""',
    ]
    if simdef.commands:
        for c in simdef.commands:
            note = "synthetic" if c.synthetic else (c.description or "")
            safe = _comment_safe(note) if note else ""
            comment = f"  # {safe}" if safe else ""
            lines.append(f"    {cmd_ident[c.name]} = 0x{c.opcode:02X}{comment}")
    else:
        lines.append("    pass")
    lines.append("")
    lines.append("")
    lines.append("class TelemetryAPID(IntEnum):")
    lines.append('    """Telemetry APIDs from XTCE container restriction criteria."""')
    if simdef.packets:
        for pkt in simdef.packets:
            safe = _comment_safe(pkt.description or "")
            comment = f"  # {safe}" if safe else ""
            lines.append(f"    {pkt_ident[pkt.name]} = 0x{pkt.apid:02X}{comment}")
    else:
        lines.append("    pass")
    lines.append("")
    lines.append("")

    # COMMAND_PARAMS
    lines.append("COMMAND_PARAMS: dict[int, list[ParamInfo]] = {")
    for c in simdef.commands:
        if not c.params:
            lines.append(f"    CommandOpcode.{cmd_ident[c.name]}: [],")
            continue
        lines.append(f"    CommandOpcode.{cmd_ident[c.name]}: [")
        for p in c.params:
            lines.append(f"        {_param_info_repr(p)},")
        lines.append("    ],")
    lines.append("}")
    lines.append("")
    lines.append("")

    # PACKET_FIELDS
    lines.append("PACKET_FIELDS: dict[int, list[FieldInfo]] = {")
    for pkt in simdef.packets:
        if not pkt.fields:
            lines.append(f"    TelemetryAPID.{pkt_ident[pkt.name]}: [],")
            continue
        lines.append(f"    TelemetryAPID.{pkt_ident[pkt.name]}: [")
        for f in pkt.fields:
            lines.append(f"        {_field_info_repr(f)},")
        lines.append("    ],")
    lines.append("}")
    lines.append("")
    lines.append("")

    # PACKET_FORMATS
    lines.append("PACKET_FORMATS: dict[int, str] = {")
    for pkt in simdef.packets:
        lines.append(f"    TelemetryAPID.{pkt_ident[pkt.name]}: {pkt.struct_format!r},")
    lines.append("}")
    lines.append("")

    return "\n".join(lines)


def _param_info_repr(p: ParamInfo) -> str:
    args = [repr(p.name), str(p.size_bits), repr(p.python_type)]
    if p.unit is not None:
        args.append(f"unit={p.unit!r}")
    if p.description is not None:
        args.append(f"description={p.description!r}")
    # Guard NaN/inf, which would emit bare `nan`/`inf` — a NameError on import.
    if p.valid_min is not None and math.isfinite(p.valid_min):
        args.append(f"valid_min={p.valid_min}")
    if p.valid_max is not None and math.isfinite(p.valid_max):
        args.append(f"valid_max={p.valid_max}")
    if p.enumerations is not None:
        args.append(f"enumerations={p.enumerations!r}")
    return f"ParamInfo({', '.join(args)})"


def _field_info_repr(f: FieldInfo) -> str:
    args = [repr(f.name), str(f.size_bits), repr(f.python_type)]
    if f.unit is not None:
        args.append(f"unit={f.unit!r}")
    if f.description is not None:
        args.append(f"description={f.description!r}")
    return f"FieldInfo({', '.join(args)})"
