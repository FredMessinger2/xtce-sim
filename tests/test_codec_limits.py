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
DATA = Path(__file__).resolve().parent / "data"


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
    cmd = _cmd("string", 32)
    with pytest.raises(ValueError, match=r"P: value is 5 bytes.*field holds 4"):
        codec.encode_command(cmd, {"P": "ABCDE"})


def test_command_multibyte_counts_encoded_bytes():
    cmd = _cmd("string", 32)
    # "café" is 4 characters but 5 UTF-8 bytes — must be rejected by bytes.
    with pytest.raises(ValueError, match="5 bytes"):
        codec.encode_command(cmd, {"P": "café"})
    # 3 chars / 4 bytes fits a 4-byte field.
    assert codec.encode_command(_cmd("string", 32), {"P": "caé"}) == "caé".encode()


def test_command_binary_oversized_rejected():
    cmd = _cmd("bytes", 16)
    with pytest.raises(ValueError, match="field holds 2"):
        codec.encode_command(cmd, {"P": b"\x01\x02\x03"})


def test_command_zero_size_field_rejects_nonempty():
    # A 0-bit (variable-length placeholder) field holds nothing; empty is the
    # only value that fits — anything else must not silently vanish.
    cmd = _cmd("string", 0)
    assert codec.encode_command(cmd, {"P": ""}) == b""
    with pytest.raises(ValueError, match="field holds 0"):
        codec.encode_command(cmd, {"P": "X"})


def test_command_wrong_type_for_string_field_is_value_error():
    # A non-str/bytes value must raise a catchable ValueError (the CLI and
    # exerciser catch ValueError), not leak a TypeError from len().
    cmd = _cmd("string", 32)
    with pytest.raises(ValueError, match="expected str or bytes, got int"):
        codec.encode_command(cmd, {"P": 5})


def test_decode_command_stays_liberal():
    # Receiving stays lenient: an over-long payload truncates, short pads —
    # and string arguments come back as TEXT (NUL padding stripped), the
    # way they were commanded, so no log or console shows a hex blob.
    cmd = _cmd("string", 32)
    assert codec.decode_command(cmd, b"ABCDEFGH")["P"] == "ABCD"
    assert codec.decode_command(cmd, b"A")["P"] == "A"


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


def _int_pkt(python_type: str = "uint32", size_bits: int = 32, fmt: str = ">I") -> PacketDef:
    return PacketDef(
        name="PKT",
        apid=1,
        fields=[FieldInfo(name="N", size_bits=size_bits, python_type=python_type)],
        struct_format=fmt,
    )


def test_telemetry_out_of_range_int_saturates_and_warns_once(caplog):
    # The real case: a uint32 epoch field handed a post-2106 deadline.
    # Whichever layer produced the value, the packet must pack (saturated),
    # not die in struct.pack and vanish from the downlink.
    codec._oversize_warned.clear()
    pkt = _int_pkt()
    over = {"N": 4_922_553_600}  # 2126-01-01 as epoch seconds
    with caplog.at_level(logging.WARNING, logger="xtce_sim.codec"):
        assert codec.pack_telemetry(pkt, over) == b"\xff\xff\xff\xff"
        codec.pack_telemetry(pkt, over)  # repeat offender
    warnings = [r for r in caplog.records if "saturating" in r.message]
    assert len(warnings) == 1


def test_telemetry_signed_int_saturates_at_both_ends():
    codec._oversize_warned.clear()
    pkt = _int_pkt("int8", 8, ">b")
    assert codec.pack_telemetry(pkt, {"N": 999}) == b"\x7f"
    assert codec.pack_telemetry(pkt, {"N": -999}) == b"\x80"


def test_telemetry_in_range_int_is_untouched(caplog):
    codec._oversize_warned.clear()
    with caplog.at_level(logging.WARNING, logger="xtce_sim.codec"):
        assert codec.pack_telemetry(_int_pkt(), {"N": 42}) == b"\x00\x00\x00\x2a"
    assert not caplog.records


# ---- CLI: the reject surfaces as a clean error ------------------------------


def test_send_oversized_filename_is_clean_error():
    # encode_command raises before any socket is opened, so no server needed;
    # the CLI turns it into a one-line error instead of a traceback.
    result = CliRunner().invoke(
        main,
        ["send", "--def", str(DATA / "my_vehicle/my_vehicle.xml"), "--port", "1",
         "FILE_DOWNLOAD", f"Filename={'x' * 70}"],  # 70 bytes into 64
    )
    assert result.exit_code != 0
    assert "70 bytes" in result.output and "64" in result.output
    assert "Traceback" not in result.output


# ---- ValidRange / enum-membership enforcement (strict uplink) ---------------


def _ranged_cmd():
    return CommandDef(
        name="MOVE",
        opcode=0x10,
        params=[
            ParamInfo("Speed", 16, "int16", valid_min=-6000, valid_max=6000),
            ParamInfo("Axis", 8, "uint8", enumerations={"X": 0, "Y": 1, "Z": 2}),
            ParamInfo("Tag", 8, "uint8"),  # no declared range
        ],
    )


def test_range_violations_inclusive_bounds():
    cmd = _ranged_cmd()
    assert codec.range_violations(cmd, {"Speed": 6000}) == []   # max is legal
    assert codec.range_violations(cmd, {"Speed": -6000}) == []  # min is legal
    v = codec.range_violations(cmd, {"Speed": 6001})
    assert v and "outside ValidRange [-6000, 6000]" in v[0]


def test_range_violations_enum_membership():
    cmd = _ranged_cmd()
    assert codec.range_violations(cmd, {"Axis": "Y"}) == []
    assert codec.range_violations(cmd, {"Axis": 2}) == []
    assert codec.range_violations(cmd, {"Axis": 9})       # raw non-member
    assert codec.range_violations(cmd, {"Axis": "Warp"})  # unknown label


def test_range_violations_unranged_param_passes_anything():
    cmd = _ranged_cmd()
    assert codec.range_violations(cmd, {"Tag": 255}) == []


def test_encode_command_enforces_ranges_and_force_bypasses():
    cmd = _ranged_cmd()
    with pytest.raises(ValueError, match="outside ValidRange"):
        codec.encode_command(cmd, {"Speed": 7000})
    # The deliberate override still packs the wire bytes.
    payload = codec.encode_command(cmd, {"Speed": 5000}, validate=False)
    assert len(payload) == 4
    codec.encode_command(cmd, {"Speed": 7000}, validate=False)  # must not raise


def test_encode_validates_defaulted_arguments_too():
    """An omitted argument packs as zero; if zero violates its range the
    ground must refuse — not transmit and let the vehicle's rejection
    surprise the operator."""
    cmd = CommandDef(
        name="SETPOINT",
        opcode=0x22,
        params=[
            ParamInfo("HeaterId", 8, "uint8", valid_min=1, valid_max=2),
            ParamInfo("Setpoint", 16, "int16"),
        ],
    )
    with pytest.raises(ValueError, match="HeaterId=0 is outside ValidRange"):
        codec.encode_command(cmd, {"Setpoint": 25})
    codec.encode_command(cmd, {"HeaterId": 1, "Setpoint": 25})  # fine when supplied


def test_nan_cannot_satisfy_a_declared_range():
    cmd = _ranged_cmd()
    v = codec.range_violations(cmd, {"Speed": float("nan")})
    assert v and "nan cannot satisfy" in v[0]
    # No declared range -> NaN is not our problem at this layer.
    assert codec.range_violations(cmd, {"Tag": float("nan")}) == []


def test_float32_boundary_decides_identically_on_both_ends():
    """0.1 is not float32-exact: the wire value decodes slightly above it.
    Ground and vehicle must reach the same verdict on the boundary."""
    import struct as _struct

    cmd = CommandDef(
        name="TRIM",
        opcode=0x23,
        params=[ParamInfo("Gain", 32, "float32", valid_min=-0.1, valid_max=0.1)],
    )
    # Ground accepts the declared boundary...
    payload = codec.encode_command(cmd, {"Gain": 0.1})
    # ...and the decoded (float32-rounded) wire value still passes.
    decoded = codec.decode_command(cmd, payload)
    assert decoded["Gain"] == _struct.unpack(">f", _struct.pack(">f", 0.1))[0]
    assert codec.range_violations(cmd, decoded) == []


def test_bool_argument_range_checked_as_int():
    cmd = CommandDef(
        name="LEVEL",
        opcode=0x24,
        params=[ParamInfo("Mode", 8, "uint8", valid_min=2, valid_max=5)],
    )
    with pytest.raises(ValueError, match="Mode=1 is outside ValidRange"):
        codec.encode_command(cmd, {"Mode": True})
