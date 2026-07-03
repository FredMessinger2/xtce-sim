"""
Payload codec — turn field values into telemetry bytes and command bytes back
into argument values, driven by a resolved `SimDefinition`.

Telemetry payloads pack in packet-field order using each packet's big-endian
struct format. Command payloads (the bytes after the opcode) unpack in
parameter order. String/binary fields pack as fixed-size byte blobs.
"""

from __future__ import annotations

import struct
from typing import Optional

from xtce_sim.definition import CommandDef, PacketDef
from xtce_sim.generate import fields_to_struct_format

_STRING_TYPES = ("string", "bytes")


def _default_value(python_type: str):
    if python_type in _STRING_TYPES:
        return b""  # struct pads a short bytes value with NULs to the field size
    if python_type in ("float32", "float64"):
        return 0.0
    return 0


def default_field_values(packet: PacketDef) -> dict:
    """A zero-valued dict for every field in a packet."""
    return {f.name: _default_value(f.python_type) for f in packet.fields}


def pack_telemetry(packet: PacketDef, values: Optional[dict] = None) -> bytes:
    """Pack a telemetry payload from a name→value dict (missing names default)."""
    values = values or {}
    ordered = [
        values.get(f.name, _default_value(f.python_type)) for f in packet.fields
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


def _coerce_arg(param, value):
    """Coerce a user-supplied argument value to the param's wire type.

    Accepts already-typed values or strings (as they arrive from the CLI):
    enum labels map to their integer value, numeric strings parse (ints allow
    ``0x`` hex), and string/binary fields become bytes.
    """
    if param.enumerations:
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
    if param.python_type in ("float32", "float64"):
        return float(value)
    if param.python_type in _STRING_TYPES:
        return value.encode() if isinstance(value, str) else value
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
