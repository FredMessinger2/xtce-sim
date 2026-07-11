"""End-to-end tests for the live asyncio simulator server."""

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest

from xtce_sim import ccsds
from xtce_sim.definition import SimDefinition
from xtce_sim.server import SimServer, _ClientConn

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(
        [EXAMPLES / "my_vehicle/my_vehicle_commands.xml", EXAMPLES / "my_vehicle/my_vehicle_telemetry.xml"]
    )


def _register_fake(server: SimServer, writer, maxsize: int = 256) -> _ClientConn:
    """Attach a fake writer as a client with its own queue + writer task,
    mirroring what _handle_client does for a real connection."""
    conn = _ClientConn(writer=writer, queue=asyncio.Queue(maxsize=maxsize))
    conn.task = asyncio.create_task(server._client_writer(conn))
    server._clients[writer] = conn
    return conn


async def _wait_dropped(server: SimServer, writer, tries: int = 100) -> None:
    for _ in range(tries):
        if writer not in server._clients:
            return
        await asyncio.sleep(0.01)


async def _read_one_frame(reader: asyncio.StreamReader) -> bytes:
    """Read until at least one full wire frame is available; return the packet."""
    buffer = b""
    while True:
        buffer += await asyncio.wait_for(reader.read(4096), timeout=2.0)
        packets, buffer = ccsds.deframe(buffer)
        if packets:
            return packets[0]


async def test_client_receives_telemetry_beacon(simdef: SimDefinition):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.05)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        packet = await _read_one_frame(reader)
        header = ccsds.CCSDSHeader.unpack(packet[:6])
        # APID must be one the definition knows about.
        assert simdef.packet_by_apid(header.apid) is not None
        assert header.packet_type == ccsds.PacketType.TELEMETRY
        writer.close()
    finally:
        await server.stop()


async def test_server_dispatches_command(simdef: SimDefinition):
    received: list = []

    async def handler(srv, command, args):
        received.append((command.name, args))

    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=10.0, command_handler=handler
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)

        # Pick a real command and send it (opcode byte + zeroed args).
        cmd = simdef.command_by_name("NOOP") or simdef.commands[0]
        packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([cmd.opcode])
        writer.write(ccsds.frame(packet))
        await writer.drain()

        # Give the event loop a moment to dispatch.
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)

        assert received and received[0][0] == cmd.name
        writer.close()
    finally:
        await server.stop()


async def test_two_instances_serve_independently(simdef: SimDefinition):
    """A fleet: two servers on distinct ports each beacon to their own client."""
    a = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.05)
    b = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.05)
    await a.start()
    await b.start()
    try:
        assert a.bound_port != b.bound_port

        ra, wa = await asyncio.open_connection("127.0.0.1", a.bound_port)
        rb, wb = await asyncio.open_connection("127.0.0.1", b.bound_port)
        pkt_a = await _read_one_frame(ra)
        pkt_b = await _read_one_frame(rb)

        assert simdef.packet_by_apid(ccsds.CCSDSHeader.unpack(pkt_a[:6]).apid) is not None
        assert simdef.packet_by_apid(ccsds.CCSDSHeader.unpack(pkt_b[:6]).apid) is not None
        # Each server only sees its own client.
        assert a.client_count == 1
        assert b.client_count == 1

        wa.close()
        wb.close()
    finally:
        await a.stop()
        await b.stop()


async def test_send_packet_unknown_apid_is_ignored(simdef: SimDefinition):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        server.send_packet(0x7FF)  # not a real APID; logs a warning, no raise
    finally:
        await server.stop()


async def test_write_error_drops_client(simdef: SimDefinition):
    """A writer whose write raises is dropped by its writer task, not fatal."""

    class BrokenWriter:
        def get_extra_info(self, _):
            return ("broken", 0)

        def write(self, data):
            raise OSError("broken pipe")

        async def drain(self):
            pass

        def close(self):
            pass

    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        broken = BrokenWriter()
        conn = _register_fake(server, broken)
        server.send_packet(simdef.packets[0].apid)  # enqueues; writer task errors
        await _wait_dropped(server, broken)
        assert broken not in server._clients  # dropped on write error
        # The writer task handled the error gracefully (didn't propagate it).
        assert conn.task.done() and conn.task.exception() is None
    finally:
        await server.stop()


async def test_undecodable_command_is_ignored(simdef: SimDefinition):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        # A CCSDS header with no opcode byte -> parse returns None.
        writer.write(ccsds.frame(ccsds.CCSDSHeader(packet_type=1, apid=1).pack()))
        await writer.drain()
        await asyncio.sleep(0.05)
        assert server.client_count == 1  # still connected, packet just ignored
        writer.close()
    finally:
        await server.stop()


async def test_framing_error_drops_connection(simdef: SimDefinition):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        bad = bytearray(ccsds.frame(ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + b"\x00"))
        bad[-1] ^= 0xFF  # corrupt the CRC -> FrameError -> server drops the connection
        writer.write(bytes(bad))
        await writer.drain()
        await asyncio.sleep(0.05)
        assert server.client_count == 0
        writer.close()
    finally:
        await server.stop()


async def test_beacon_runs_with_no_clients(simdef: SimDefinition):
    """The beacon loop's no-clients path runs without error before anyone connects."""
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        await asyncio.sleep(0.05)  # a couple of idle beacon cycles
        assert server.client_count == 0
    finally:
        await server.stop()


async def test_broadcast_reaches_multiple_real_clients(simdef: SimDefinition):
    """Two connected clients each receive the beacon (concurrent broadcast)."""
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.03)
    await server.start()
    try:
        ra, wa = await asyncio.open_connection("127.0.0.1", server.bound_port)
        rb, wb = await asyncio.open_connection("127.0.0.1", server.bound_port)
        pkt_a = await _read_one_frame(ra)
        pkt_b = await _read_one_frame(rb)
        assert pkt_a[:1] and pkt_b[:1]
        assert server.client_count == 2
        wa.close()
        wb.close()
    finally:
        await server.stop()


async def test_slow_client_dropped_without_blocking_others(simdef: SimDefinition):
    """A writer that never drains is dropped on timeout; a healthy peer keeps flowing."""

    class StalledWriter:
        """drain() hangs forever, simulating a client that never reads."""

        def get_extra_info(self, _):
            return ("stalled", 0)

        def write(self, data):
            pass

        async def drain(self):
            await asyncio.sleep(3600)

        def close(self):
            pass

    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=10.0, write_timeout=0.1
    )
    await server.start()
    try:
        stalled = StalledWriter()
        conn = _register_fake(server, stalled)
        server.send_packet(simdef.packets[0].apid)  # enqueue; drain will hang
        await _wait_dropped(server, stalled)  # writer task times out after 0.1s
        assert stalled not in server._clients
        # Timeout was handled gracefully, not propagated out of the task.
        assert conn.task.done() and conn.task.exception() is None
    finally:
        await server.stop()


async def test_full_queue_drops_client_without_blocking_broadcast(simdef: SimDefinition):
    """A client that can't keep up fills its bounded queue and is dropped."""

    class StalledWriter:
        def get_extra_info(self, _):
            return ("stalled", 0)

        def write(self, data):
            pass

        async def drain(self):
            await asyncio.sleep(3600)  # never drains -> queue backs up

        def close(self):
            pass

    # Large write_timeout so the drop is due to the FULL QUEUE, not a timeout.
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0, write_timeout=30.0)
    await server.start()
    try:
        stalled = StalledWriter()
        _register_fake(server, stalled, maxsize=3)
        apid = simdef.packets[0].apid
        # send_packet enqueues without yielding, so the queue fills before the
        # writer task can drain it; the overflow enqueue drops the client.
        for _ in range(6):
            server.send_packet(apid)
        assert stalled not in server._clients
    finally:
        await server.stop()


async def test_serve_forever_cleans_up_on_cancel(simdef: SimDefinition):
    """serve_forever's finally runs stop(): beacon stopped, listener closed."""
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.1)  # let it bind + beacon
    assert server.bound_port > 0
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # stop() ran: beacon task is finished and the listener is closed.
    assert server._beacon_task is not None and server._beacon_task.done()


def test_on_beacon_done_logs_unexpected_exception(simdef: SimDefinition, caplog):
    """The done-callback logs a genuine (non-cancel) beacon failure."""
    server = SimServer(simdef, host="127.0.0.1", port=0)

    async def boom():
        raise RuntimeError("beacon exploded")

    async def drive():
        task = asyncio.create_task(boom())
        task.add_done_callback(server._on_beacon_done)
        with contextlib.suppress(RuntimeError):
            await task

    with caplog.at_level(logging.ERROR):
        asyncio.run(drive())
    assert any("terminated unexpectedly" in r.message for r in caplog.records)


async def test_beacon_survives_a_failing_telemetry_source(simdef: SimDefinition):
    """A telemetry_source that raises for one packet doesn't kill beaconing."""
    calls = {"n": 0}

    def flaky_source(packet_def):
        calls["n"] += 1
        raise ValueError("boom")

    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=0.02, telemetry_source=flaky_source
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        await asyncio.sleep(0.1)  # several beacon cycles despite the source raising
        assert calls["n"] >= 2  # kept trying -> loop is alive
        assert not server._beacon_task.done()  # loop did not die
        writer.close()
    finally:
        await server.stop()


async def test_raising_command_handler_does_not_drop_client(simdef: SimDefinition):
    async def bad_handler(srv, command, args):
        raise RuntimeError("handler blew up")

    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=10.0, command_handler=bad_handler
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        cmd = simdef.command_by_name("NOOP") or simdef.commands[0]
        packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([cmd.opcode])
        writer.write(ccsds.frame(packet))
        await writer.drain()
        await asyncio.sleep(0.1)
        assert server.client_count == 1  # handler raised, but client stayed connected
        writer.close()
    finally:
        await server.stop()


async def test_unknown_opcode_is_ignored(simdef: SimDefinition):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([0xFE])
        writer.write(ccsds.frame(packet))
        await writer.drain()
        await asyncio.sleep(0.05)
        # Server stays up and the client is still connected.
        assert server.client_count == 1
        writer.close()
    finally:
        await server.stop()


# ---- command echo (protocol infrastructure, see ccsds.py) -------------------


async def _read_echo(reader: asyncio.StreamReader) -> bytes:
    """Read frames until the command-echo packet arrives (beacons skipped)."""
    buffer = b""
    for _ in range(200):
        buffer += await asyncio.wait_for(reader.read(4096), timeout=2.0)
        packets, buffer = ccsds.deframe(buffer)
        for pkt in packets:
            if ccsds.CCSDSHeader.unpack(pkt[:6]).apid == ccsds.CMD_ECHO_APID:
                return pkt
    raise AssertionError("no echo packet arrived")


async def test_executed_command_is_echoed_with_the_original_bytes(simdef):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        cmd = simdef.command_by_name("NOOP") or simdef.commands[0]
        packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([cmd.opcode])
        writer.write(ccsds.frame(packet))
        await writer.drain()
        echo = await _read_echo(reader)
        status, embedded = ccsds.parse_command_echo(echo)
        assert status == ccsds.ECHO_EXECUTED
        assert embedded == packet  # the command bytes come back verbatim
        writer.close()
    finally:
        await server.stop()


async def test_unknown_opcode_is_echoed_as_unknown(simdef):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([0xFE])
        writer.write(ccsds.frame(packet))
        await writer.drain()
        status, embedded = ccsds.parse_command_echo(await _read_echo(reader))
        assert status == ccsds.ECHO_UNKNOWN_OPCODE
        assert embedded == packet
        writer.close()
    finally:
        await server.stop()


async def test_execution_error_is_echoed_as_failed(simdef):
    # Short payloads decode as zeros by design (decode_command pads), so the
    # honest FAILED trigger is an execution error — here, a raising handler.
    async def bad_handler(server, command, args):
        raise RuntimeError("boom")

    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=10.0, command_handler=bad_handler
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        cmd = simdef.command_by_name("NOOP") or simdef.commands[0]
        packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([cmd.opcode])
        writer.write(ccsds.frame(packet))
        await writer.drain()
        status, _ = ccsds.parse_command_echo(await _read_echo(reader))
        assert status == ccsds.ECHO_FAILED
        writer.close()
    finally:
        await server.stop()


async def test_out_of_range_argument_is_rejected_with_echo(simdef):
    """The vehicle validates for itself: a non-member enum value arriving on
    the wire rejects the command with the REJECTED echo status."""
    from xtce_sim import codec

    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        cmd = simdef.command_by_name("SET_POWER")
        payload = codec.encode_command(
            cmd, {"SubsystemId": 0, "PowerState": 99}, validate=False
        )
        packet = (
            ccsds.CCSDSHeader(packet_type=1, apid=1).pack()
            + bytes([cmd.opcode])
            + payload
        )
        writer.write(ccsds.frame(packet))
        await writer.drain()
        status, embedded = ccsds.parse_command_echo(await _read_echo(reader))
        assert status == ccsds.ECHO_REJECTED
        assert embedded == packet
        assert server.client_count == 1  # rejection is not a connection error
        writer.close()
    finally:
        await server.stop()


async def test_truncated_payload_zero_padding_can_reject():
    """A short payload pads with zeros by design; when the padded zero falls
    outside a declared range (HeaterId is 1..2), the vehicle now rejects
    instead of executing with a nonsense argument."""
    from xtce_sim.definition import SimDefinition as SimDef

    imaging = SimDef.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    server = SimServer(imaging, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        cmd = imaging.command_by_name("SET_HEATER_SETPOINT")
        packet = (  # opcode only — no argument bytes at all
            ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([cmd.opcode])
        )
        writer.write(ccsds.frame(packet))
        await writer.drain()
        status, _ = ccsds.parse_command_echo(await _read_echo(reader))
        assert status == ccsds.ECHO_REJECTED
        writer.close()
    finally:
        await server.stop()
