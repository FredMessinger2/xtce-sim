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
# (0x7FF is the CCSDS idle-packet APID; 0x7FE is left free for future
# protocol packets.) A definition that tries to claim this APID for its own
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
# been skipped-with-a-warning). FAILED means the packet was undecodable, or
# effect application / the command handler raised — in the handler case,
# behavior effects and their immediate emissions may ALREADY have applied
# before the failure. A one-byte status cannot express "partially landed";
# the sim's own log carries the detail.
ECHO_EXECUTED = 0
ECHO_UNKNOWN_OPCODE = 1  # no command in the definition has this opcode
ECHO_FAILED = 2

ECHO_STATUS_NAMES = {
    ECHO_EXECUTED: "executed",
    ECHO_UNKNOWN_OPCODE: "unknown_opcode",
    ECHO_FAILED: "failed",
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
