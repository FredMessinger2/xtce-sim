"""
CCSDS packet framing — pure, no I/O.

Wire format on the TCP port (both directions) — a 2-byte length-prefixed frame
with a trailing CRC:

    [2-byte length][CCSDS packet][2-byte CRC-16-CCITT]

The length field counts the whole frame *including itself*. The CCSDS packet is
a 6-byte primary header followed by the payload; for commands the payload's
first byte is the opcode.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

import crcmod

# CRC-16-CCITT: poly=0x1021, init=0xFFFF, xorOut=0x0000.
_CRC16 = crcmod.mkCrcFun(0x11021, initCrc=0xFFFF, rev=False, xorOut=0x0000)


def crc16(data: bytes) -> int:
    """CRC-16-CCITT over ``data``."""
    return _CRC16(data)


class PacketType(IntEnum):
    """CCSDS packet type (1-bit field in the primary header)."""

    TELEMETRY = 0
    COMMAND = 1


@dataclass
class CCSDSHeader:
    """CCSDS primary header (6 bytes / 48 bits)."""

    version: int = 0  # 3 bits
    packet_type: int = 0  # 1 bit (0=TLM, 1=CMD)
    sec_hdr_flag: int = 0  # 1 bit
    apid: int = 0  # 11 bits
    seq_flags: int = 3  # 2 bits (3 = standalone)
    seq_count: int = 0  # 14 bits
    packet_length: int = 0  # 16 bits (payload byte count - 1)

    def pack(self) -> bytes:
        word1 = (
            ((self.version & 0x7) << 13)
            | ((self.packet_type & 0x1) << 12)
            | ((self.sec_hdr_flag & 0x1) << 11)
            | (self.apid & 0x7FF)
        )
        word2 = ((self.seq_flags & 0x3) << 14) | (self.seq_count & 0x3FFF)
        word3 = self.packet_length & 0xFFFF
        return struct.pack(">HHH", word1, word2, word3)

    @classmethod
    def unpack(cls, data: bytes) -> "CCSDSHeader":
        if len(data) < 6:
            raise ValueError(f"CCSDS header needs 6 bytes, got {len(data)}")
        word1, word2, word3 = struct.unpack(">HHH", data[:6])
        return cls(
            version=(word1 >> 13) & 0x7,
            packet_type=(word1 >> 12) & 0x1,
            sec_hdr_flag=(word1 >> 11) & 0x1,
            apid=word1 & 0x7FF,
            seq_flags=(word2 >> 14) & 0x3,
            seq_count=word2 & 0x3FFF,
            packet_length=word3,
        )


def build_telemetry_packet(apid: int, payload: bytes, seq_count: int = 0) -> bytes:
    """Assemble a CCSDS telemetry packet (header + payload)."""
    header = CCSDSHeader(
        version=0,
        packet_type=PacketType.TELEMETRY,
        sec_hdr_flag=0,
        apid=apid,
        seq_flags=3,
        seq_count=seq_count,
        packet_length=len(payload) - 1 if payload else 0,
    )
    return header.pack() + payload


# =============================================================================
# COMMAND ECHO (protocol infrastructure)
# =============================================================================
#
# On every command it processes — executed or not — the sim broadcasts a
# command-echo telemetry packet so the ground can see exactly what arrived
# and what became of it. Real systems verify commanding the same way
# (command counters, PUS Service 1 acknowledgment, literal command echo);
# this is the echo flavor, collapsed to one packet with a status byte.
#
# The echo rides a RESERVED APID, documented here rather than declared in
# any satellite's XTCE: like the length-prefix framing above, it is part of
# this simulator's link protocol, not part of a vehicle's payload telemetry.
# (0x7FF is the CCSDS idle-packet APID; 0x7FE carries the file uplink,
# below.) A definition that tries to claim a reserved APID for its own
# telemetry is rejected at build time.
#
#     payload = [1-byte status][the received command packet]
#
# The embedded command is verbatim except at the extreme: an echo larger
# than the 16-bit wire frame allows has its embed truncated to fit — the
# status byte still tells the story.

CMD_ECHO_APID = 0x7FD

# Status semantics, honestly stated: EXECUTED means the command decoded and
# the whole dispatch completed (individual behavior effects may still have
# been skipped-with-a-warning). REJECTED means an argument violated its
# declared ValidRange or enum — rejection precedes dispatch, so NO effect
# applied and no immediate emission fired. FAILED means the packet was
# undecodable, or effect application / the command handler raised — in the
# handler case, behavior effects and their immediate emissions may ALREADY
# have applied before the failure. A one-byte status cannot express
# "partially landed"; the sim's own log carries the detail.
ECHO_EXECUTED = 0
ECHO_UNKNOWN_OPCODE = 1  # no command in the definition has this opcode
ECHO_FAILED = 2
ECHO_REJECTED = 3  # decoded fine, but an argument violates its ValidRange/enum

ECHO_STATUS_NAMES = {
    ECHO_EXECUTED: "executed",
    ECHO_UNKNOWN_OPCODE: "unknown_opcode",
    ECHO_FAILED: "failed",
    ECHO_REJECTED: "rejected",
}

# Largest embeddable command: 65535 (16-bit frame length) - 2 (length field)
# - 2 (CRC) - 6 (echo header) - 1 (status byte).
_ECHO_EMBED_MAX = 65524


def build_command_echo(command_packet: bytes, status: int, seq_count: int = 0) -> bytes:
    """A command-echo telemetry packet wrapping the received command.

    An embed too large for the wire frame is truncated rather than letting
    the echo (and the ground's visibility of an anomalous command) vanish.
    """
    return build_telemetry_packet(
        CMD_ECHO_APID, bytes([status]) + command_packet[:_ECHO_EMBED_MAX], seq_count
    )


def parse_command_echo(packet: bytes) -> tuple[int | None, bytes]:
    """Split an echo packet into (status, embedded_command_packet).

    Returns ``(None, b"")`` if the packet has no status byte.
    """
    payload = packet[6:]
    if not payload:
        return None, b""
    return payload[0], payload[1:]


# =============================================================================
# FILE UPLINK (protocol infrastructure)
# =============================================================================
#
# The ground moves a file to the vehicle by chopping it into chunks and
# sending them over the same TCP link commands ride, on the second reserved
# APID. Flight systems layer a file-transfer protocol over their command
# link the same way (CFDP over CCSDS); this is that idea reduced to its
# honest minimum for a reliable, ordered transport: TCP already guarantees
# delivery and order, so the protocol only has to name the file, declare
# its size and CRC-32 up front, and let the vehicle verify what actually
# arrived before anything lands in storage.
#
# One transfer per connection at a time, three packet shapes (uplink
# direction, packet type COMMAND), payload first byte is the chunk type:
#
#     START  = [0x00][1-byte name length][name, UTF-8][4-byte size][4-byte CRC-32]
#     DATA   = [0x01][4-byte offset][chunk bytes]
#     FINISH = [0x02]
#
# The offset is redundant on TCP but cheap, and it turns a reassembly bug
# into a detected, refused transfer instead of a corrupt file. The vehicle
# answers every outcome on the downlink as a FILE_RECEIPT packet (see
# fileservice.py); this module only builds and parses the frames.

FILE_UPLINK_APID = 0x7FE

#: Link-protocol APIDs no satellite definition may claim: the generator
#: refuses them at build time and the server warns if one sneaks past it.
RESERVED_APIDS = {
    CMD_ECHO_APID: "command echo",
    FILE_UPLINK_APID: "file uplink",
}

FILE_START = 0
FILE_DATA = 1
FILE_FINISH = 2

# Ceiling on a DATA chunk: 65535 (16-bit frame length) - 2 (length field)
# - 2 (CRC) - 6 (CCSDS header) - 1 (chunk type) - 4 (offset).
FILE_CHUNK_MAX = 65520


def _build_uplink_packet(payload: bytes, seq_count: int) -> bytes:
    header = CCSDSHeader(
        packet_type=PacketType.COMMAND,
        apid=FILE_UPLINK_APID,
        seq_count=seq_count,
        packet_length=len(payload) - 1,
    )
    return header.pack() + payload


def build_file_start(filename: str, size: int, crc: int, seq_count: int = 0) -> bytes:
    """The START packet opening a transfer: name, declared size, declared CRC-32."""
    name = filename.encode("utf-8")
    if not 1 <= len(name) <= 255:
        raise ValueError(f"filename must encode to 1..255 bytes, got {len(name)}")
    if not 0 <= size <= 0xFFFFFFFF:
        raise ValueError(f"file size {size} does not fit the 32-bit size field")
    payload = (
        bytes([FILE_START, len(name)]) + name + struct.pack(">II", size, crc & 0xFFFFFFFF)
    )
    return _build_uplink_packet(payload, seq_count)


def build_file_data(offset: int, chunk: bytes, seq_count: int = 0) -> bytes:
    """One DATA packet carrying ``chunk`` at byte ``offset`` of the file."""
    if len(chunk) > FILE_CHUNK_MAX:
        raise ValueError(f"chunk is {len(chunk)} bytes; the wire frame holds {FILE_CHUNK_MAX}")
    payload = bytes([FILE_DATA]) + struct.pack(">I", offset) + chunk
    return _build_uplink_packet(payload, seq_count)


def build_file_finish(seq_count: int = 0) -> bytes:
    """The FINISH packet closing a transfer; verification data rode in START."""
    return _build_uplink_packet(bytes([FILE_FINISH]), seq_count)


def parse_file_uplink(packet: bytes) -> tuple[int, dict]:
    """Split a file-uplink packet into ``(chunk_type, fields)``.

    START yields ``{"filename", "size", "crc"}``; DATA yields ``{"offset",
    "chunk"}``; FINISH yields ``{}``. Raises ``ValueError`` on any malformed
    payload — the receiver treats that as a protocol violation, not a crash.
    """
    payload = packet[6:]
    if not payload:
        raise ValueError("file uplink packet has no payload")
    chunk_type = payload[0]
    body = payload[1:]
    if chunk_type == FILE_START:
        return FILE_START, _parse_file_start(body)
    if chunk_type == FILE_DATA:
        if len(body) < 4:
            raise ValueError("DATA packet too short for its offset field")
        return FILE_DATA, {"offset": struct.unpack(">I", body[:4])[0], "chunk": body[4:]}
    if chunk_type == FILE_FINISH:
        if body:
            raise ValueError(f"FINISH packet carries {len(body)} unexpected byte(s)")
        return FILE_FINISH, {}
    raise ValueError(f"unknown file uplink chunk type {chunk_type}")


def _parse_file_start(body: bytes) -> dict:
    if not body:
        raise ValueError("START packet has no name length")
    name_len = body[0]
    if name_len == 0:
        raise ValueError("START packet declares an empty filename")
    if len(body) != 1 + name_len + 8:
        raise ValueError(
            f"START packet is {len(body)} byte(s) after the type; "
            f"a {name_len}-byte name needs exactly {1 + name_len + 8}"
        )
    try:
        filename = body[1 : 1 + name_len].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"START filename is not valid UTF-8: {exc}") from exc
    size, crc = struct.unpack(">II", body[1 + name_len :])
    return {"filename": filename, "size": size, "crc": crc}


def parse_command_packet(packet: bytes) -> tuple[int | None, bytes]:
    """Split a CCSDS command packet into (opcode, argument_payload).

    Returns ``(None, b"")`` if the packet is too short to contain a header and
    an opcode byte.
    """
    if len(packet) < 7:  # 6-byte header + at least the opcode
        return None, b""
    payload = packet[6:]
    if not payload:
        return None, b""
    return payload[0], payload[1:]


class SequenceCounter:
    """Per-APID CCSDS sequence counter (wraps at 14 bits)."""

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}

    def next(self, apid: int) -> int:
        count = self._counts.get(apid, 0)
        self._counts[apid] = (count + 1) & 0x3FFF
        return count


# =============================================================================
# WIRE FRAMING (length prefix + CRC)
# =============================================================================


def frame(packet: bytes) -> bytes:
    """Wrap a CCSDS packet as a wire frame: ``[len][packet][crc]``.

    The length prefix counts the entire frame including its own 2 bytes.
    """
    body = packet + struct.pack(">H", crc16(packet))
    length = len(body) + 2  # + the 2-byte length field itself
    return struct.pack(">H", length) + body


class FrameError(Exception):
    """Raised when a received frame fails CRC or length validation."""


def deframe(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Extract complete, CRC-validated CCSDS packets from a byte buffer.

    Returns ``(packets, remaining)`` where ``packets`` are the CCSDS packets
    (CRC stripped) that were fully present, and ``remaining`` is the trailing
    bytes of an incomplete frame to be retained for the next read.

    Raises ``FrameError`` on a malformed length prefix or CRC mismatch; the
    caller decides whether to drop the connection or resynchronize.
    """
    packets: list[bytes] = []
    while len(buffer) >= 2:
        length = struct.unpack(">H", buffer[:2])[0]
        if length < 4:  # len(2) + crc(2) minimum, empty CCSDS packet excluded
            raise FrameError(f"invalid frame length {length}")
        if len(buffer) < length:
            break  # incomplete; wait for more bytes
        body = buffer[2:length]  # packet + crc
        buffer = buffer[length:]

        packet, received_crc = body[:-2], struct.unpack(">H", body[-2:])[0]
        computed = crc16(packet)
        if computed != received_crc:
            raise FrameError(
                f"CRC mismatch: computed 0x{computed:04X} received 0x{received_crc:04X}"
            )
        packets.append(packet)
    return packets, buffer
