"""Tests for CCSDS packet framing and the payload codec."""

import struct

import pytest

from xtce_sim import ccsds, codec
from xtce_sim.definition import CommandDef, FieldInfo, PacketDef, ParamInfo
from xtce_sim.generate import fields_to_struct_format


def test_header_roundtrip():
    hdr = ccsds.CCSDSHeader(apid=0x123, seq_count=42, packet_length=9)
    back = ccsds.CCSDSHeader.unpack(hdr.pack())
    assert back == hdr
    assert len(hdr.pack()) == 6


def test_frame_deframe_roundtrip():
    pkt = ccsds.build_telemetry_packet(0x64, b"\x01\x02\x03\x04", seq_count=7)
    wire = ccsds.frame(pkt)
    packets, remaining = ccsds.deframe(wire)
    assert packets == [pkt]
    assert remaining == b""


def test_deframe_handles_partial_and_multiple():
    a = ccsds.frame(ccsds.build_telemetry_packet(1, b"aaaa"))
    b = ccsds.frame(ccsds.build_telemetry_packet(2, b"bbbbbb"))
    stream = a + b
    # Feed everything but the last 3 bytes; second frame stays buffered.
    packets, remaining = ccsds.deframe(stream[:-3])
    assert len(packets) == 1
    packets2, remaining2 = ccsds.deframe(remaining + stream[-3:])
    assert len(packets2) == 1
    assert remaining2 == b""


def test_deframe_rejects_bad_crc():
    wire = bytearray(ccsds.frame(ccsds.build_telemetry_packet(1, b"data")))
    wire[-1] ^= 0xFF  # corrupt the CRC
    with pytest.raises(ccsds.FrameError):
        ccsds.deframe(bytes(wire))


def test_parse_command_packet():
    # 6-byte header + opcode + args
    packet = ccsds.CCSDSHeader(apid=1).pack() + bytes([0x2A, 0x01, 0x02])
    opcode, payload = ccsds.parse_command_packet(packet)
    assert opcode == 0x2A
    assert payload == b"\x01\x02"


def test_parse_command_packet_too_short():
    assert ccsds.parse_command_packet(b"\x00" * 4) == (None, b"")


def test_codec_telemetry_roundtrip():
    pkt = PacketDef(
        name="HK",
        apid=0x10,
        fields=[
            FieldInfo("VOLTAGE", 16, "uint16"),
            FieldInfo("TEMP", 32, "float32"),
        ],
    )
    pkt.struct_format = fields_to_struct_format(pkt.fields)
    payload = codec.pack_telemetry(pkt, {"VOLTAGE": 3300, "TEMP": 21.5})
    out = codec.unpack_telemetry(pkt, payload)
    assert out["VOLTAGE"] == 3300
    assert out["TEMP"] == pytest.approx(21.5)


def test_codec_decode_command_with_enum():
    cmd = CommandDef(
        name="SET_POWER",
        opcode=0x10,
        params=[
            ParamInfo("SubsystemId", 8, "uint8"),
            ParamInfo("PowerState", 8, "uint8", enumerations={"OFF": 0, "ON": 1}),
        ],
    )
    payload = struct.pack(">BB", 3, 1)
    args = codec.decode_command(cmd, payload)
    assert args == {"SubsystemId": 3, "PowerState": "ON"}


def test_codec_decode_command_pads_short_payload():
    cmd = CommandDef(
        name="SET_TIME", opcode=0x02, params=[ParamInfo("Timestamp", 32, "uint32")]
    )
    # Empty payload should still decode (padded to zero) rather than raise.
    assert codec.decode_command(cmd, b"") == {"Timestamp": 0}


def test_command_echo_round_trip():
    cmd_packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([0x41, 0x02])
    echo = ccsds.build_command_echo(cmd_packet, ccsds.ECHO_EXECUTED, seq_count=7)
    header = ccsds.CCSDSHeader.unpack(echo[:6])
    assert header.apid == ccsds.CMD_ECHO_APID
    assert header.seq_count == 7
    status, embedded = ccsds.parse_command_echo(echo)
    assert status == ccsds.ECHO_EXECUTED
    assert embedded == cmd_packet
    ccsds.frame(echo)  # fits the wire frame


def test_command_echo_truncates_oversized_embed():
    # A command packet too big for the 16-bit wire frame must not make the
    # echo (and the ground's visibility of the anomaly) vanish: the embed is
    # truncated to fit and the frame still builds.
    huge = b"\xab" * 70_000
    echo = ccsds.build_command_echo(huge, ccsds.ECHO_FAILED)
    status, embedded = ccsds.parse_command_echo(echo)
    assert status == ccsds.ECHO_FAILED
    assert len(embedded) == 65524  # _ECHO_EMBED_MAX
    ccsds.frame(echo)  # must not raise struct.error


def test_parse_command_echo_empty():
    bare = ccsds.build_telemetry_packet(ccsds.CMD_ECHO_APID, b"")
    assert ccsds.parse_command_echo(bare) == (None, b"")
