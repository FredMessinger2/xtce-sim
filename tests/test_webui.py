"""Tests for the web console bridge (xtce_sim.webui).

The bridge is the ground-station side of the WebSocket UI: it decodes CCSDS
packets against the definition and re-publishes them as JSON. These tests
cover the JSON views (definition + telemetry messages, JSON safety) and the
live path end to end: a real SimServer beaconing into a real Bridge serving
a real WebSocket client.
"""

import asyncio
import json
import math
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from xtce_sim import ccsds, codec
from xtce_sim.definition import CalibratorInfo, SimDefinition
from xtce_sim.server import SimServer
from xtce_sim.webui import Bridge, definition_message, telemetry_message

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")


def _tlm_packet(packet_def, values=None) -> bytes:
    payload = codec.pack_telemetry(packet_def, values)
    header = ccsds.CCSDSHeader(
        packet_type=int(ccsds.PacketType.TELEMETRY),
        apid=packet_def.apid,
        packet_length=len(payload) - 1,
    )
    return header.pack() + payload


# ---------------------------------------------------------------------------
# definition message
# ---------------------------------------------------------------------------


def test_definition_message_carries_packets_and_commands(simdef):
    msg = definition_message(simdef)
    assert msg["type"] == "definition"
    assert msg["system"] == "ImagingSat"
    by_name = {p["name"]: p for p in msg["packets"]}
    attitude = by_name["ADCS_ATTITUDE"]
    q4 = next(f for f in attitude["fields"] if f["name"] == "ADCS_ATT_QUAT_Q4")
    assert q4["calibrated"] is True
    status = by_name["ADCS_STATUS"]
    mode = next(f for f in status["fields"] if f["name"] == "ADCS_MODE")
    assert "NADIR" in mode["enumerations"]
    cmds = {c["name"]: c for c in msg["commands"]}
    slew = cmds["ADCS_SLEW_TO_QUATERNION"]
    q1 = next(p for p in slew["params"] if p["name"] == "Q1")
    assert q1["min"] == -1.0 and q1["max"] == 1.0


def test_definition_message_is_valid_json(simdef):
    text = json.dumps(definition_message(simdef), allow_nan=False)
    assert json.loads(text)["type"] == "definition"


# ---------------------------------------------------------------------------
# telemetry message
# ---------------------------------------------------------------------------


def test_telemetry_message_calibrated_field_has_raw_and_eu(simdef):
    packet_def = simdef.packet_by_name("ADCS_ATTITUDE")
    packet = _tlm_packet(packet_def, {"ADCS_ATT_QUAT_Q4": 32767})
    msg = telemetry_message(simdef, packet)
    q4 = msg["fields"]["ADCS_ATT_QUAT_Q4"]
    assert q4["raw"] == 32767
    assert math.isclose(q4["eu"], 1.0, rel_tol=1e-3)


def test_telemetry_message_enum_label(simdef):
    packet_def = simdef.packet_by_name("ADCS_STATUS")
    nadir = 2  # AdcsModeType: NADIR
    packet = _tlm_packet(packet_def, {"ADCS_MODE": nadir})
    msg = telemetry_message(simdef, packet)
    assert msg["fields"]["ADCS_MODE"]["label"] == "NADIR"


def test_telemetry_message_runt_and_unknown_apid(simdef):
    assert telemetry_message(simdef, b"\x00\x01") is None
    header = ccsds.CCSDSHeader(
        packet_type=int(ccsds.PacketType.TELEMETRY), apid=0x7FE, packet_length=1
    )
    msg = telemetry_message(simdef, header.pack() + b"\xab\xcd")
    assert msg["packet"] == "APID_0x7FE"
    assert msg["undecoded"] == "abcd"


def test_telemetry_message_torn_payload_degrades(simdef):
    packet_def = simdef.packet_by_name("ADCS_ATTITUDE")
    packet = _tlm_packet(packet_def)[: 6 + 4]  # truncate mid-payload
    msg = telemetry_message(simdef, packet)
    assert msg["packet"] == "ADCS_ATTITUDE"
    assert "fields" not in msg and "undecoded" in msg


def test_non_finite_calibrated_value_is_json_null(simdef):
    packet_def = simdef.packet_by_name("ADCS_ATTITUDE")
    field = next(f for f in packet_def.fields if f.name == "ADCS_ATT_QUAT_Q4")
    original = field.calibrator
    # A spline clamps its ends and can yield NaN only via pathological
    # definitions; force one to prove the JSON layer guards it.
    field.calibrator = CalibratorInfo(spline_points=[(0.0, float("nan")), (1.0, 1.0)])
    try:
        packet = _tlm_packet(packet_def, {"ADCS_ATT_QUAT_Q4": 0})
        msg = telemetry_message(simdef, packet)
        text = json.dumps(msg, allow_nan=False)  # must not raise
        assert json.loads(text)["fields"]["ADCS_ATT_QUAT_Q4"]["eu"] is None
    finally:
        field.calibrator = original


# ---------------------------------------------------------------------------
# live end to end: SimServer -> Bridge -> WebSocket client
# ---------------------------------------------------------------------------


async def _start_bridge(bridge: Bridge) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_get("/", bridge.handle_index)
    app.router.add_get("/ws", bridge.handle_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def test_bridge_end_to_end(simdef):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.05)
    await server.start()
    bridge = Bridge(simdef, "127.0.0.1", server.bound_port)
    runner, http_port = await _start_bridge(bridge)
    downlink = asyncio.create_task(bridge.downlink_loop())
    try:
        async with ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{http_port}/") as resp:
                assert resp.status == 200
                assert "xtce-sim console" in await resp.text()
            async with session.ws_connect(f"http://127.0.0.1:{http_port}/ws") as ws:
                hello = json.loads((await ws.receive(timeout=5)).data)
                assert hello["type"] == "definition"
                assert len(hello["packets"]) == len(simdef.packets)
                link = json.loads((await ws.receive(timeout=5)).data)
                assert link["type"] == "link"
                seen = set()
                for _ in range(60):
                    msg = json.loads((await ws.receive(timeout=5)).data)
                    if msg["type"] == "telemetry":
                        seen.add(msg["packet"])
                    if len(seen) == len(simdef.packets):
                        break
                assert seen == {p.name for p in simdef.packets}
    finally:
        downlink.cancel()
        await asyncio.gather(downlink, return_exceptions=True)
        await runner.cleanup()
        await server.stop()


async def test_bridge_reports_link_down_when_sim_absent():
    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    # Point the bridge at a port nothing listens on.
    bridge = Bridge(simdef, "127.0.0.1", 1)
    runner, http_port = await _start_bridge(bridge)
    downlink = asyncio.create_task(bridge.downlink_loop())
    try:
        async with ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{http_port}/ws") as ws:
                hello = json.loads((await ws.receive(timeout=5)).data)
                assert hello["type"] == "definition"
                link = json.loads((await ws.receive(timeout=5)).data)
                assert link == {"type": "link", "up": False}
    finally:
        downlink.cancel()
        await asyncio.gather(downlink, return_exceptions=True)
        await runner.cleanup()


async def test_writer_failure_drops_client(simdef):
    bridge = Bridge(simdef, "127.0.0.1", 1)

    class DeadWS:
        async def send_str(self, text):
            raise ConnectionError("gone")

    dead = DeadWS()
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    task = asyncio.create_task(bridge._client_writer(dead, queue))
    bridge.clients[dead] = (queue, task)
    await bridge._broadcast({"type": "link", "up": True})
    await asyncio.wait_for(task, timeout=2)  # writer ends on the send failure
    assert dead not in bridge.clients


async def test_broadcast_drops_stalled_client_instead_of_blocking(simdef):
    """A browser that stops draining must not stall the downlink: its queue
    fills, it gets dropped, and _broadcast returns without ever awaiting a
    send."""
    bridge = Bridge(simdef, "127.0.0.1", 1)

    class StalledWS:
        async def send_str(self, text):
            await asyncio.Event().wait()  # never completes

    stalled = StalledWS()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    task = asyncio.create_task(bridge._client_writer(stalled, queue))
    bridge.clients[stalled] = (queue, task)
    # 2 fills the queue (the writer holds one more in-flight); the next put
    # overflows and drops the client.
    for _ in range(4):
        await bridge._broadcast({"type": "link", "up": True})
    assert stalled not in bridge.clients
    assert task.cancelled() or task.done() or task.cancelling()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
