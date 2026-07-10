"""Oversize policy tests: strict command encode, truncate-and-warn telemetry.

Strict uplink / liberal downlink: a command argument that doesn't fit its
fixed-size field is rejected at encode time (measured in encoded bytes, so
multibyte UTF-8 counts correctly); an oversized telemetry value is truncated
with a once-per-field warning so the beacon keeps running.
"""

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim import codec
from xtce_sim.cli import main
from xtce_sim.definition import CommandDef, FieldInfo, PacketDef, ParamInfo

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _cmd(python_type: str, size_bits: int) -> CommandDef:
    return CommandDef(
        name="C",
        opcode=1,
        params=[ParamInfo(name="P", size_bits=size_bits, python_type=python_type)],
    )


def _pkt(size_bits: int = 32) -> PacketDef:
    return PacketDef(
        name="PKT",
        apid=1,
        fields=[FieldInfo(name="MSG", size_bits=size_bits, python_type="string")],
        struct_format=f">{size_bits // 8}s",
    )


# ---- command encode: strict ------------------------------------------------


def test_command_string_exact_fit_passes():
    payload = codec.encode_command(_cmd("string", 32), {"P": "ABCD"})  # 4 bytes into 4
    assert payload == b"ABCD"


def test_command_string_oversized_rejected():
    with pytest.raises(ValueError, match=r"P: value is 5 bytes.*field holds 4"):
        codec.encode_command(_cmd("string", 32), {"P": "ABCDE"})


def test_command_multibyte_counts_encoded_bytes():
    # "café" is 4 characters but 5 UTF-8 bytes — must be rejected by bytes.
    with pytest.raises(ValueError, match="5 bytes"):
        codec.encode_command(_cmd("string", 32), {"P": "café"})
    # 3 chars / 4 bytes fits a 4-byte field.
    assert codec.encode_command(_cmd("string", 32), {"P": "caé"}) == "caé".encode()


def test_command_binary_oversized_rejected():
    with pytest.raises(ValueError, match="field holds 2"):
        codec.encode_command(_cmd("bytes", 16), {"P": b"\x01\x02\x03"})


def test_command_zero_size_field_rejects_nonempty():
    # A 0-bit (variable-length placeholder) field holds nothing; empty is the
    # only value that fits — anything else must not silently vanish.
    assert codec.encode_command(_cmd("string", 0), {"P": ""}) == b""
    with pytest.raises(ValueError, match="field holds 0"):
        codec.encode_command(_cmd("string", 0), {"P": "X"})


def test_command_wrong_type_for_string_field_is_value_error():
    # A non-str/bytes value must raise a catchable ValueError (the CLI and
    # exerciser catch ValueError), not leak a TypeError from len().
    with pytest.raises(ValueError, match="expected str or bytes, got int"):
        codec.encode_command(_cmd("string", 32), {"P": 5})


def test_decode_command_stays_liberal():
    # Receiving stays lenient: an over-long payload truncates, short pads.
    cmd = _cmd("string", 32)
    assert codec.decode_command(cmd, b"ABCDEFGH")["P"] == b"ABCD"
    assert codec.decode_command(cmd, b"A")["P"] == b"A\x00\x00\x00"


# ---- telemetry pack: liberal, but loud once --------------------------------


def test_telemetry_oversized_truncates_and_warns_once(caplog):
    codec._oversize_warned.clear()
    pkt = _pkt(32)
    big = {"MSG": b"ABCDEFGH"}  # 8 bytes into a 4-byte field
    with caplog.at_level(logging.WARNING, logger="xtce_sim.codec"):
        assert codec.pack_telemetry(pkt, big) == b"ABCD"  # truncated, no raise
        codec.pack_telemetry(pkt, big)  # repeat offender
    warnings = [r for r in caplog.records if "truncating" in r.message]
    assert len(warnings) == 1  # warned exactly once per packet/field
    assert "8 bytes" in warnings[0].getMessage()


def test_telemetry_fitting_value_no_warning(caplog):
    codec._oversize_warned.clear()
    with caplog.at_level(logging.WARNING, logger="xtce_sim.codec"):
        assert codec.pack_telemetry(_pkt(32), {"MSG": b"OK"}) == b"OK\x00\x00"
    assert not caplog.records


# ---- CLI: the reject surfaces as a clean error ------------------------------


def test_send_oversized_filename_is_clean_error():
    # encode_command raises before any socket is opened, so no server needed;
    # the CLI turns it into a one-line error instead of a traceback.
    result = CliRunner().invoke(
        main,
        ["send", "--def", str(EXAMPLES / "my_vehicle/my_vehicle.xml"), "--port", "1",
         "FILE_DOWNLOAD", f"Filename={'x' * 70}"],  # 70 bytes into 64
    )
    assert result.exit_code != 0
    assert "70 bytes" in result.output and "64" in result.output
    assert "Traceback" not in result.output
