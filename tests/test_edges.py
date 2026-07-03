"""Small edge-case tests to cover remaining branches across modules."""

import asyncio
import logging
import struct
from pathlib import Path

import pytest

from xtce_sim import ccsds, client, codec, logs, models, render
from xtce_sim.definition import CommandDef, ParamInfo, SimDefinition
from xtce_sim.server import SimServer

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def simdef():
    return SimDefinition.from_xtce(
        [EXAMPLES / "my_vehicle_commands.xml", EXAMPLES / "my_vehicle_telemetry.xml"]
    )


# ---- render ----------------------------------------------------------------


def test_render_fmt_bool_and_single_prefix():
    assert render.fmt_value(True) == "True"
    assert render.common_prefix(["ONLY_ONE"]) == ""  # <2 names -> no prefix


# ---- logs ------------------------------------------------------------------


def test_logs_warning_color_and_exc_info():
    fmt = logs.InstanceFormatter("sat-a", color=True)
    warn = logging.LogRecord("t", logging.WARNING, __file__, 1, "careful", None, None)
    assert "\x1b[33m" in fmt.format(warn)  # yellow

    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    out = logs.InstanceFormatter("sat-a", color=False).format(rec)
    assert "Traceback" in out and "ValueError: boom" in out


def test_logs_no_color_env(monkeypatch):
    import io

    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    stream.isatty = lambda: True  # tty, but NO_COLOR wins
    assert logs._use_color("auto", stream) is False


# ---- codec -----------------------------------------------------------------


def test_codec_decode_enum_without_matching_label():
    cmd = CommandDef(
        name="C", opcode=1, params=[ParamInfo("S", 8, "uint8", enumerations={"OFF": 0})]
    )
    # Raw value 9 has no label -> decode returns the raw int.
    assert codec.decode_command(cmd, struct.pack(">B", 9)) == {"S": 9}


def test_codec_encode_string_and_float():
    cmd = CommandDef(
        name="C",
        opcode=1,
        params=[ParamInfo("NAME", 32, "string"), ParamInfo("GAIN", 32, "float32")],
    )
    payload = codec.encode_command(cmd, {"NAME": "hi", "GAIN": 1.5})
    out = codec.decode_command(cmd, payload)
    assert out["GAIN"] == pytest.approx(1.5)
    assert out["NAME"].startswith(b"hi")


# ---- ccsds -----------------------------------------------------------------


def test_ccsds_header_unpack_too_short():
    with pytest.raises(ValueError):
        ccsds.CCSDSHeader.unpack(b"\x00\x00\x00")


def test_ccsds_deframe_bad_length():
    with pytest.raises(ccsds.FrameError):
        ccsds.deframe(b"\x00\x02rest")  # length field 2 is below the minimum


# ---- definition ------------------------------------------------------------


def test_definition_from_dict_defaults():
    sd = SimDefinition.from_dict({})
    assert sd.space_system_name == "Unknown"
    assert sd.commands == [] and sd.packets == []


def test_definition_from_xtce_empty_list():
    with pytest.raises(ValueError):
        SimDefinition.from_xtce([])


# ---- models (argument-side aggregates/arrays) ------------------------------


def test_models_aggregate_argument_helpers():
    reg = {
        "F": models.FloatArgumentType(name="F", size_in_bits=32),
        "I": models.IntegerArgumentType(name="I", size_in_bits=16),
    }
    agg = models.AggregateArgumentType(
        name="V",
        members=[
            models.AggregateMember("x", "F"),
            models.AggregateMember("y", "I"),
        ],
    )
    assert agg.get_member("x").type_ref == "F"
    assert agg.get_member("nope") is None
    assert agg.get_total_size(reg) == 48
    assert agg.get_total_size({"F": reg["F"]}) == 0  # missing member type -> 0


def test_models_array_argument_dynamic_elements():
    arr = models.ArrayArgumentType(name="A", dimensions=[(0, True, "N")])
    assert arr.get_total_elements() is None  # dynamic


# ---- client (timeout + stream end) -----------------------------------------


async def test_client_stream_timeout_then_server_closes(simdef):
    """stream_packets honors the timeout arg and ends cleanly when the server closes."""
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    port = server.bound_port

    def read_until_closed():
        # timeout=2.0 exercises the settimeout branch; drain until EOF (b'') -> break.
        packets = []
        for pkt in client.stream_packets("127.0.0.1", port, timeout=2.0):
            packets.append(pkt)
            if len(packets) >= 2:
                break
        return packets

    task = asyncio.create_task(asyncio.to_thread(read_until_closed))
    await asyncio.sleep(0.2)
    await server.stop()  # must not hang now that stop() closes clients first
    packets = await task
    assert len(packets) >= 1
