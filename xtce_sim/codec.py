"""
Payload codec — turn field values into telemetry bytes and command bytes back
into argument values, driven by a resolved `SimDefinition`.

Telemetry payloads pack in packet-field order using each packet's big-endian
struct format. Command payloads (the bytes after the opcode) unpack in
parameter order. String/binary fields pack as fixed-size byte blobs.

Oversize policy (strict uplink, liberal downlink — the mission-control norm):
a command argument that does not fit its field is rejected at encode time,
measured in encoded bytes so multibyte UTF-8 is counted correctly; an
oversized telemetry value is truncated with a warning so the beacon keeps
running.
"""

from __future__ import annotations

import logging
import struct
from typing import Optional

from xtce_sim.definition import CommandDef, PacketDef
from xtce_sim.generate import fields_to_struct_format

logger = logging.getLogger(__name__)

_STRING_TYPES = ("string", "bytes")

# (packet, field) pairs already warned about an oversized telemetry value.
# The beacon repacks every interval, so a persistent offender would otherwise
# flood the log with one warning per field per tick.
_oversize_warned: set[tuple[str, str]] = set()


def _default_value(python_type: str):
    if python_type in _STRING_TYPES:
        return b""  # struct pads a short bytes value with NULs to the field size
    if python_type in ("float32", "float64"):
        return 0.0
    return 0


def default_field_values(packet: PacketDef) -> dict:
    """A zero-valued dict for every field in a packet."""
    return {f.name: _default_value(f.python_type) for f in packet.fields}


def _fit_telemetry_value(packet: PacketDef, field, value):
    """Truncate an oversized string/binary telemetry value to its field size.

    Liberal downlink: unlike command encoding this never raises — the beacon
    must keep running — but the loss is logged (once per packet/field, since
    the beacon repacks every interval).
    """
    if field.python_type not in _STRING_TYPES or not isinstance(value, (bytes, bytearray)):
        return value
    capacity = field.size_bits // 8
    if len(value) <= capacity:
        return value
    key = (packet.name, field.name)
    if key not in _oversize_warned:
        _oversize_warned.add(key)
        logger.warning(
            "%s.%s: telemetry value is %d bytes, field holds %d — truncating "
            "(further occurrences suppressed)",
            packet.name, field.name, len(value), capacity,
        )
    return value[:capacity]


def pack_telemetry(packet: PacketDef, values: Optional[dict] = None) -> bytes:
    """Pack a telemetry payload from a name→value dict (missing names default).

    Oversized string/binary values are truncated to their field size (with a
    warning) rather than raising — see ``_fit_telemetry_value``.
    """
    values = values or {}
    ordered = [
        _fit_telemetry_value(
            packet, f, values.get(f.name, _default_value(f.python_type))
        )
        for f in packet.fields
    ]
    return struct.pack(packet.struct_format, *ordered)


def unpack_telemetry(packet: PacketDef, payload: bytes) -> dict:
    """Unpack a telemetry payload into a name→value dict."""
    values = struct.unpack(packet.struct_format, payload)
    return {f.name: v for f, v in zip(packet.fields, values)}


def command_struct_format(command: CommandDef) -> str:
    """Big-endian struct format for a command's argument payload."""
    # ParamInfo is duck-compatible with FieldInfo (name/size_bits/python_type).
    return fields_to_struct_format(command.params)


def _coerce_enum_arg(param, value):
    """Map an enum label (or raw numeric string) to its integer wire value."""
    if isinstance(value, str):
        if value in param.enumerations:
            return param.enumerations[value]
        try:
            return int(value, 0)  # allow a raw numeric enum value too
        except ValueError:
            raise ValueError(
                f"{param.name}: unknown enum {value!r}; "
                f"valid: {sorted(param.enumerations)}"
            ) from None
    return value


def _coerce_arg(param, value):
    """Coerce a user-supplied argument value to the param's wire type.

    Accepts already-typed values or strings (as they arrive from the CLI):
    enum labels map to their integer value, numeric strings parse (ints allow
    ``0x`` hex), and string/binary fields become bytes. A string/binary value
    that does not fit its fixed-size field raises rather than silently
    truncating (strict uplink).
    """
    if param.enumerations:
        return _coerce_enum_arg(param, value)
    if param.python_type in ("float32", "float64"):
        return float(value)
    if param.python_type in _STRING_TYPES:
        encoded = value.encode() if isinstance(value, str) else value
        if not isinstance(encoded, (bytes, bytearray)):
            raise ValueError(
                f"{param.name}: expected str or bytes, got {type(value).__name__}"
            )
        capacity = param.size_bits // 8
        if len(encoded) > capacity:
            raise ValueError(
                f"{param.name}: value is {len(encoded)} bytes once encoded, "
                f"field holds {capacity}"
            )
        return encoded
    if isinstance(value, str):
        return int(value, 0)
    return value


def encode_command(command: CommandDef, args: Optional[dict] = None) -> bytes:
    """Encode a command's argument payload (the bytes *after* the opcode).

    Missing arguments default to zero/empty; unknown argument names raise so
    typos surface instead of being silently dropped.
    """
    args = args or {}
    known = {p.name for p in command.params}
    unknown = set(args) - known
    if unknown:
        raise ValueError(
            f"{command.name}: unknown argument(s) {sorted(unknown)}; "
            f"valid: {sorted(known)}"
        )

    values = []
    for param in command.params:
        if param.name in args:
            values.append(_coerce_arg(param, args[param.name]))
        else:
            values.append(_default_value(param.python_type))
    return struct.pack(command_struct_format(command), *values)


def decode_command(command: CommandDef, payload: bytes) -> dict:
    """Decode a command's argument payload into a name→value dict.

    The payload is the command packet bytes *after* the opcode. It is padded or
    truncated to the expected argument size so a short/over-long client frame
    still decodes rather than raising.
    """
    if not command.params:
        return {}
    fmt = command_struct_format(command)
    size = struct.calcsize(fmt)
    buf = payload[:size].ljust(size, b"\x00")
    values = struct.unpack(fmt, buf)

    result: dict = {}
    for param, value in zip(command.params, values):
        if param.enumerations:
            # Attach the enum label when the raw value matches one.
            label = next((k for k, v in param.enumerations.items() if v == value), None)
            result[param.name] = label if label is not None else value
        else:
            result[param.name] = value
    return result
