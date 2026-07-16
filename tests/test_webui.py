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


def test_telemetry_message_string_field_renders_text(simdef):
    """A string field's engineering value is its text; the raw view keeps
    hex. Surfaced by FILE_RECEIPT — the first packet to downlink a real
    string — which the console showed as a hex blob."""
    packet_def = simdef.packet_by_name("FILE_RECEIPT")
    packet = _tlm_packet(packet_def, {"FR_FILENAME": b"plan.ats"})
    msg = telemetry_message(simdef, packet)
    name = msg["fields"]["FR_FILENAME"]
    assert name["eu"] == "plan.ats"
    assert name["raw"].startswith("706c616e2e617473")  # hex, NUL-padded


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
    bridge._broadcast({"type": "link", "up": True})
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
        bridge._broadcast({"type": "link", "up": True})
    assert stalled not in bridge.clients
    assert task.cancelled() or task.done() or task.cancelling()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# command echo -> command-log message
# ---------------------------------------------------------------------------


def _echo_of(command, args=None, status=ccsds.ECHO_EXECUTED) -> bytes:
    payload = codec.encode_command(command, args)
    cmd_packet = (
        ccsds.CCSDSHeader(packet_type=int(ccsds.PacketType.COMMAND), apid=1).pack()
        + bytes([command.opcode])
        + payload
    )
    return ccsds.build_command_echo(cmd_packet, status)


def test_command_message_decodes_name_args_and_enum_labels(simdef):
    from xtce_sim.webui import command_message

    cmd = simdef.command_by_name("ADCS_SET_MODE")
    msg = command_message(simdef, _echo_of(cmd, {"Mode": "NADIR"}))
    assert msg["type"] == "command"
    assert msg["name"] == "ADCS_SET_MODE"
    assert msg["args"] == {"Mode": "NADIR"}  # label, as commanded
    assert msg["status"] == "executed"


def test_command_message_numeric_args(simdef):
    from xtce_sim.webui import command_message

    cmd = simdef.command_by_name("ADCS_WHEEL_SET_SPEED")
    msg = command_message(simdef, _echo_of(cmd, {"WheelId": 2, "Speed": -2200.0}))
    assert msg["args"]["WheelId"] == 2
    assert msg["args"]["Speed"] == -2200.0


def test_command_message_unknown_opcode(simdef):
    from xtce_sim.webui import command_message

    cmd_packet = (
        ccsds.CCSDSHeader(packet_type=int(ccsds.PacketType.COMMAND), apid=1).pack()
        + bytes([0xEE])
        + b"\x01\x02"
    )
    echo = ccsds.build_command_echo(cmd_packet, ccsds.ECHO_UNKNOWN_OPCODE)
    msg = command_message(simdef, echo)
    assert msg["name"] == "OPCODE_0xEE"
    assert msg["status"] == "unknown_opcode"
    assert msg["raw"] == "0102"


def test_command_message_undecodable(simdef):
    from xtce_sim.webui import command_message

    echo = ccsds.build_command_echo(b"\x00\x01", ccsds.ECHO_FAILED)
    msg = command_message(simdef, echo)
    assert msg["name"] == "<undecodable>"
    assert msg["status"] == "failed"


async def test_bridge_pushes_command_to_browser(simdef):
    """End to end: a command uplinked to the sim appears in the browser
    stream as a command message, decoded with its arguments."""
    from xtce_sim import client

    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.05)
    await server.start()
    bridge = Bridge(simdef, "127.0.0.1", server.bound_port)
    runner, http_port = await _start_bridge(bridge)
    downlink = asyncio.create_task(bridge.downlink_loop())
    try:
        async with ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{http_port}/ws") as ws:
                for _ in range(2):  # definition + link
                    await ws.receive(timeout=5)
                cmd = simdef.command_by_name("ADCS_SET_MODE")
                await asyncio.to_thread(
                    client.send_command,
                    "127.0.0.1", server.bound_port, cmd, {"Mode": "SUNSAFE"},
                )
                for _ in range(120):
                    msg = json.loads((await ws.receive(timeout=5)).data)
                    if msg["type"] == "command":
                        assert msg["name"] == "ADCS_SET_MODE"
                        assert msg["args"] == {"Mode": "SUNSAFE"}
                        assert msg["status"] == "executed"
                        return
                raise AssertionError("command message never arrived")
    finally:
        downlink.cancel()
        await asyncio.gather(downlink, return_exceptions=True)
        await runner.cleanup()
        await server.stop()


def test_definition_message_carries_significance(simdef):
    msg = definition_message(simdef)
    cmds = {c["name"]: c for c in msg["commands"]}
    assert cmds["ADCS_DESATURATE"]["significance"] == "critical"
    assert "momentum" in cmds["ADCS_DESATURATE"]["significance_reason"].lower()
    assert cmds["TAKE_IMAGE"]["significance"] is None


def test_command_message_rejected_status(simdef):
    from xtce_sim.webui import command_message

    cmd = simdef.command_by_name("ADCS_WHEEL_SET_SPEED")
    payload = codec.encode_command(cmd, {"WheelId": 7, "Speed": 0}, validate=False)
    cmd_packet = (
        ccsds.CCSDSHeader(packet_type=int(ccsds.PacketType.COMMAND), apid=1).pack()
        + bytes([cmd.opcode])
        + payload
    )
    msg = command_message(simdef, ccsds.build_command_echo(cmd_packet, ccsds.ECHO_REJECTED))
    assert msg["status"] == "rejected"
    assert msg["args"]["WheelId"] == 7  # the offending value is visible


# ---------------------------------------------------------------------------
# Command entry: the page's uplink through the bridge
# ---------------------------------------------------------------------------


def test_definition_message_carries_event_only_apids(simdef):
    msg = definition_message(simdef)
    receipt = simdef.packet_by_name("FILE_RECEIPT")
    assert msg["event_only"] == [receipt.apid]


class _WireTap:
    """A fake StreamWriter collecting what the bridge uplinks."""

    def __init__(self):
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False


async def test_uplink_refuses_before_the_wire(simdef):
    bridge = Bridge(simdef, "127.0.0.1", 1)
    tap = _WireTap()
    bridge._sim_writer = tap

    ok, error = await bridge.uplink("NO_SUCH_COMMAND", {})
    assert not ok and "unknown command" in error

    ok, error = await bridge.uplink("HEATER_ON", {"HeaterId": "9"})
    assert not ok and "ValidRange" in error

    ok, error = await bridge.uplink("HEATER_ON", {"Typo": "1"})
    assert not ok and "unknown argument" in error

    ok, error = await bridge.uplink("HEATER_ON", "not-a-dict")
    assert not ok and "NAME=VALUE" in error

    assert tap.data == b""  # every refusal above: NOTHING was transmitted


async def test_uplink_refuses_when_the_link_is_down(simdef):
    bridge = Bridge(simdef, "127.0.0.1", 1)  # never connected
    ok, error = await bridge.uplink("NOOP", {})
    assert not ok and "link is down" in error


async def test_uplink_transmits_a_wire_identical_command(simdef):
    # The bridge builds through the same ccsds owner as client.send_command,
    # so what leaves here is byte-identical to a CLI send.
    bridge = Bridge(simdef, "127.0.0.1", 1)
    tap = _WireTap()
    bridge._sim_writer = tap
    ok, detail = await bridge.uplink("HEATER_ON", {"HeaterId": "1"})
    assert ok, detail
    packets, rest = ccsds.deframe(tap.data)
    assert rest == b"" and len(packets) == 1
    command = simdef.command_by_name("HEATER_ON")
    expected = ccsds.build_command_packet(
        command.opcode, codec.encode_command(command, {"HeaterId": "1"})
    )
    assert packets[0] == expected


async def test_page_send_end_to_end(simdef):
    """A browser send travels: WS -> bridge -> sim -> executes -> echo -> WS."""
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=30.0)
    await server.start()
    bridge = Bridge(simdef, "127.0.0.1", server.bound_port)
    runner, http_port = await _start_bridge(bridge)
    downlink = asyncio.create_task(bridge.downlink_loop())
    try:
        async with ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{http_port}/ws") as ws:

                async def next_of(wanted_type):
                    for _ in range(50):
                        msg = json.loads((await ws.receive(timeout=5)).data)
                        if msg["type"] == wanted_type:
                            return msg
                    raise AssertionError(f"no {wanted_type} message arrived")

                await next_of("definition")
                # Wait for the bridge's sim link before commanding.
                for _ in range(100):
                    if bridge.link_up:
                        break
                    await asyncio.sleep(0.02)

                # Garbage inbound must not kill the socket.
                await ws.send_str("this is not json")
                await ws.send_str(json.dumps(["not", "a", "dict"]))

                await ws.send_str(json.dumps({
                    "type": "send", "id": 1,
                    "name": "SET_POWER",
                    "args": {"SubsystemId": "3", "PowerState": "ON"},
                }))
                result = await next_of("send_result")
                assert result == {"type": "send_result", "id": 1, "name": "SET_POWER", "ok": True}
                echo = await next_of("command")
                assert echo["name"] == "SET_POWER"
                assert echo["status"] == "executed"
                assert echo["args"]["PowerState"] == "ON"

                # A ground refusal answers the asker and never reaches the sim.
                await ws.send_str(json.dumps({
                    "type": "send", "id": 2,
                    "name": "HEATER_ON", "args": {"HeaterId": "9"},
                }))
                refused = await next_of("send_result")
                assert refused["id"] == 2 and not refused["ok"]
                assert "ValidRange" in refused["error"]
    finally:
        downlink.cancel()
        await asyncio.gather(downlink, return_exceptions=True)
        await runner.cleanup()
        await server.stop()


async def test_uplink_refuses_json_shaped_garbage(simdef):
    # JSON can deliver null/arrays/objects where the codec expects numbers;
    # every one must cost a refusal, never the socket (TypeError included).
    bridge = Bridge(simdef, "127.0.0.1", 1)
    tap = _WireTap()
    bridge._sim_writer = tap
    for bad in (None, [1, 2], {"nested": 1}):
        ok, error = await bridge.uplink("SET_ATTITUDE_TARGET", {"Q1": bad})
        assert not ok, f"{bad!r} must refuse"
    assert tap.data == b""


async def test_handle_send_falsy_args_is_refused_not_defaulted(simdef):
    # args=[] must NOT become "send with every argument zero".
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=30.0)
    await server.start()
    bridge = Bridge(simdef, "127.0.0.1", server.bound_port)
    runner, http_port = await _start_bridge(bridge)
    downlink = asyncio.create_task(bridge.downlink_loop())
    try:
        async with ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{http_port}/ws") as ws:
                await ws.send_str(json.dumps(
                    {"type": "send", "id": 9, "name": "SET_POWER", "args": []}
                ))
                for _ in range(50):
                    msg = json.loads((await ws.receive(timeout=5)).data)
                    if msg["type"] == "send_result":
                        break
                assert not msg["ok"]
                assert "NAME=VALUE" in msg["error"]
    finally:
        downlink.cancel()
        await asyncio.gather(downlink, return_exceptions=True)
        await runner.cleanup()
        await server.stop()


async def test_ws_refuses_foreign_origins(simdef):
    """Browsers do not enforce same-origin on WebSockets — the bridge must.

    A page from any other origin (an internet site open in the operator's
    browser) is refused; the console's own origin and non-browser clients
    (no Origin header) pass.
    """
    bridge = Bridge(simdef, "127.0.0.1", 1)
    runner, http_port = await _start_bridge(bridge)
    try:
        async with ClientSession() as session:
            for origin in ("http://evil.example", f"http://127.0.0.1:{http_port + 1}"):
                async with session.get(
                    f"http://127.0.0.1:{http_port}/ws",
                    headers={
                        "Origin": origin,
                        "Connection": "Upgrade",
                        "Upgrade": "websocket",
                        "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    },
                ) as resp:
                    assert resp.status == 403, origin
            async with session.ws_connect(
                f"http://127.0.0.1:{http_port}/ws",
                origin=f"http://127.0.0.1:{http_port}",
            ) as ws:
                hello = json.loads((await ws.receive(timeout=5)).data)
                assert hello["type"] == "definition"  # own origin passes
    finally:
        await runner.cleanup()


async def test_host_guard_refuses_rebound_names(simdef):
    """DNS rebinding arrives with a foreign name in the Host header; the
    middleware refuses it for the page AND the WebSocket, while the
    loopback aliases the console is legitimately browsed at all pass."""
    import socket as socketlib

    from xtce_sim.webui import _host_guard

    bridge = Bridge(simdef, "127.0.0.1", 1)
    # Bind the socket FIRST (no release-and-rebind race), then build the
    # guard with the port it actually holds.
    sock = socketlib.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = web.Application(middlewares=[_host_guard("127.0.0.1", port)])
    app.router.add_get("/", bridge.handle_index)
    app.router.add_get("/ws", bridge.handle_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        async with ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/", headers={"Host": f"evil.example:{port}"}
            ) as resp:
                assert resp.status == 403  # the rebound name is refused
            async with session.get(
                f"http://127.0.0.1:{port}/", headers={"Host": f"localhost:{port}"}
            ) as resp:
                assert resp.status == 200  # loopback aliases pass
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
            # Well-formed foreign hosts get the guard's own 403; case games
            # don't slip past the (lowercased) comparison.
            for foreign in (f"evil.example:{port}", f"EVIL.EXAMPLE:{port}"):
                async with session.get(
                    f"http://127.0.0.1:{port}/", headers={"Host": foreign}
                ) as resp:
                    assert resp.status == 403, foreign
            # A MALFORMED Host must be refused without a traceback-500 —
            # whether by the guard (403) or by a stricter future aiohttp
            # parser (400) is an internal detail, so pin the property.
            for bad in ("evil.example:99999", "evil:abc"):
                async with session.get(
                    f"http://127.0.0.1:{port}/", headers={"Host": bad}
                ) as resp:
                    assert resp.status in (400, 403), bad
            async with session.get(
                f"http://127.0.0.1:{port}/", headers={"Host": f"LOCALHOST:{port}"}
            ) as resp:
                assert resp.status == 200  # our own alias, any spelling
    finally:
        await runner.cleanup()
