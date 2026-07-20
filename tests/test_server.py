"""End-to-end tests for the live asyncio simulator server."""

import asyncio
import contextlib
import logging
import zlib
from pathlib import Path

import pytest

from xtce_sim import ccsds, codec
from xtce_sim import client as sim_client
from xtce_sim.definition import SimDefinition
from xtce_sim.fileservice import FileService, FileStore
from xtce_sim.server import SimServer, _ClientConn

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(
        [DATA / "my_vehicle/my_vehicle_commands.xml", DATA / "my_vehicle/my_vehicle_telemetry.xml"]
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


# ---- file service (uplink + FILE_* commands, see fileservice.py) -------------


@pytest.fixture(scope="module")
def imaging() -> SimDefinition:
    return SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")


def _file_server(imaging, tmp_path, **kwargs) -> SimServer:
    service = FileService(FileStore(tmp_path / "files"), imaging)
    return SimServer(
        imaging,
        host="127.0.0.1",
        port=0,
        file_service=service,
        **kwargs,
    )


async def _read_receipt(reader, imaging, *, want_name: bytes, skip_status: int = 2):
    """Frames until a FILE_RECEIPT naming ``want_name`` with a terminal status."""
    receipt_def = imaging.packet_by_name("FILE_RECEIPT")
    buffer = b""
    for _ in range(400):
        buffer += await asyncio.wait_for(reader.read(4096), timeout=2.0)
        packets, buffer = ccsds.deframe(buffer)
        for pkt in packets:
            if ccsds.CCSDSHeader.unpack(pkt[:6]).apid != receipt_def.apid:
                continue
            values = codec.unpack_telemetry(receipt_def, pkt[6:])
            name = values["FR_FILENAME"].rstrip(b"\x00")
            if name == want_name and values["FR_TRANSFER_STATUS"] != skip_status:
                return values
    raise AssertionError(f"no terminal receipt for {want_name!r} arrived")


async def test_upload_end_to_end(imaging, tmp_path):
    """The whole path: client chunks the file up, the vehicle lands it and
    answers with a receipt the client returns as its verdict."""
    server = _file_server(imaging, tmp_path, beacon_interval=10.0)
    await server.start()
    try:
        data = b"2026-07-12T00:00:00Z NOOP\n" * 100
        receipt = await asyncio.to_thread(
            sim_client.upload_file,
            "127.0.0.1",
            server.bound_port,
            "plan.ats",
            data,
            simdef=imaging,
            chunk_size=64,  # force many DATA frames
        )
        assert receipt is not None
        assert receipt["FR_FILE_SIZE"] == len(data)
        assert receipt["FR_CHECKSUM"] == zlib.crc32(data) & 0xFFFFFFFF
        assert (tmp_path / "files/plan.ats").read_bytes() == data
    finally:
        await server.stop()


async def test_upload_refused_raises_for_the_ground(imaging, tmp_path):
    """A CRC the content does not match is refused, and the ground client
    turns the FAILED receipt into an error instead of quiet success."""
    server = _file_server(imaging, tmp_path, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(ccsds.frame(ccsds.build_file_start("bad.bin", 3, 0xBAD)))
        writer.write(ccsds.frame(ccsds.build_file_data(0, b"abc")))
        writer.write(ccsds.frame(ccsds.build_file_finish()))
        await writer.drain()
        values = await _read_receipt(reader, imaging, want_name=b"bad.bin")
        assert values["FR_TRANSFER_STATUS"] == 1  # FAILED
        assert not (tmp_path / "files/bad.bin").exists()
        writer.close()
    finally:
        await server.stop()


async def test_disconnect_mid_transfer_fails_it(imaging, tmp_path):
    """Dropping the link half-way through a transfer ends it honestly: the
    remaining clients see the FAILED receipt on the downlink."""
    server = _file_server(imaging, tmp_path, beacon_interval=10.0)
    await server.start()
    try:
        watcher_reader, watcher_writer = await asyncio.open_connection(
            "127.0.0.1", server.bound_port
        )
        _, uploader_writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        uploader_writer.write(ccsds.frame(ccsds.build_file_start("half.bin", 100, 0)))
        await uploader_writer.drain()
        await asyncio.sleep(0.05)  # let the START land before the hangup
        uploader_writer.close()
        values = await _read_receipt(watcher_reader, imaging, want_name=b"half.bin")
        assert values["FR_TRANSFER_STATUS"] == 1  # FAILED
        watcher_writer.close()
    finally:
        await server.stop()


async def test_file_commands_act_on_the_real_store(imaging, tmp_path):
    """FILE_LIST answers one receipt per stored file; FILE_DELETE removes the
    file from disk and is echoed EXECUTED like any command."""
    server = _file_server(imaging, tmp_path, beacon_interval=10.0)
    await server.start()
    server.file_service.store.write("a.ats", b"AA")
    server.file_service.store.write("b.ats", b"BBB")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        list_cmd = imaging.command_by_name("FILE_LIST")
        await asyncio.to_thread(
            sim_client.send_command, "127.0.0.1", server.bound_port, list_cmd, {}
        )
        values = await _read_receipt(reader, imaging, want_name=b"b.ats")
        assert values["FR_FILE_SIZE"] == 3
        writer.close()

        # A fresh watcher for the delete: the LIST receipts already carried
        # a.ats with SUCCESS, and a stale one must not pass as the verdict.
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        delete_cmd = imaging.command_by_name("FILE_DELETE")
        await asyncio.to_thread(
            sim_client.send_command,
            "127.0.0.1",
            server.bound_port,
            delete_cmd,
            {"Filename": "a.ats"},
        )
        values = await _read_receipt(reader, imaging, want_name=b"a.ats")
        assert values["FR_TRANSFER_STATUS"] == 0  # SUCCESS
        assert not (tmp_path / "files/a.ats").exists()
        writer.close()
    finally:
        await server.stop()


async def test_uplink_without_file_service_is_dropped_not_fatal(imaging):
    """No store configured: file frames are dropped with a warning, and the
    connection keeps working for ordinary commands."""
    server = SimServer(imaging, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(ccsds.frame(ccsds.build_file_start("f", 1, 0)))
        cmd = imaging.command_by_name("NOOP")
        packet = ccsds.CCSDSHeader(packet_type=1, apid=1).pack() + bytes([cmd.opcode])
        writer.write(ccsds.frame(packet))
        await writer.drain()
        status, _ = ccsds.parse_command_echo(await _read_echo(reader))
        assert status == ccsds.ECHO_EXECUTED  # the link survived the file frame
        writer.close()
    finally:
        await server.stop()


async def test_receipt_packet_is_never_beaconed(imaging, tmp_path):
    """FILE_RECEIPT is event telemetry: the beacon skips it, so the last
    event stays on every console instead of being erased a second later
    (and stale verdicts are never repeated at the ground)."""
    server = _file_server(imaging, tmp_path, beacon_interval=0.02)
    await server.start()
    try:
        reader, _writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        receipt_def = imaging.packet_by_name("FILE_RECEIPT")
        seen: set[int] = set()
        buffer = b""
        # Several full beacon cycles: every packet APID shows up except 0x15.
        while len(seen) < len(imaging.packets) - 1:
            buffer += await asyncio.wait_for(reader.read(4096), timeout=2.0)
            packets, buffer = ccsds.deframe(buffer)
            seen.update(ccsds.CCSDSHeader.unpack(p[:6]).apid for p in packets)
            assert receipt_def.apid not in seen
    finally:
        await server.stop()

async def test_upload_verdict_survives_interfering_file_list(imaging, tmp_path):
    """THE correlation attack from review: while a replacement upload is in
    flight, a FILE_LIST from another ground broadcasts a SUCCESS receipt
    naming the same file (the OLD copy). The uploader's matcher must skip it
    and wait for its own transfer's verdict."""
    server = _file_server(imaging, tmp_path, beacon_interval=10.0)
    server.file_service.store.write("plan.ats", b"the old version, longer")
    await server.start()
    try:
        new_data = b"v2"
        crc = zlib.crc32(new_data) & 0xFFFFFFFF
        receipt_def = imaging.packet_by_name("FILE_RECEIPT")
        matcher = sim_client._ReceiptMatcher(receipt_def, "plan.ats", len(new_data), crc)

        # Open the transfer but do NOT finish it yet.
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(ccsds.frame(ccsds.build_file_start("plan.ats", len(new_data), crc)))
        writer.write(ccsds.frame(ccsds.build_file_data(0, new_data)))
        await writer.drain()

        # Another ground lists the store: a SUCCESS receipt naming plan.ats
        # (old size/CRC 23 bytes) is broadcast to everyone, uploader included.
        list_cmd = imaging.command_by_name("FILE_LIST")
        await asyncio.to_thread(
            sim_client.send_command, "127.0.0.1", server.bound_port, list_cmd, {}
        )

        async def pump(until):
            """Feed every downlinked packet through the client's matcher, in
            arrival order, until ``until(packets)`` is satisfied or a verdict
            appears; returns (verdict, satisfied)."""
            buffer = b""
            seen: list[bytes] = []
            for _ in range(400):
                verdict = None
                buffer += await asyncio.wait_for(reader.read(4096), timeout=2.0)
                packets, buffer = ccsds.deframe(buffer)
                for pkt in packets:
                    got = matcher.verdict([pkt])
                    if got is not None:
                        verdict = got
                seen.extend(packets)
                if verdict is not None or until(seen):
                    return verdict, until(seen)
            raise AssertionError("pump never satisfied")

        def stale_list_receipt_arrived(seen: list[bytes]) -> bool:
            for pkt in seen:
                if ccsds.CCSDSHeader.unpack(pkt[:6]).apid != receipt_def.apid:
                    continue
                values = codec.unpack_telemetry(receipt_def, pkt[6:])
                if values["FR_FILE_SIZE"] == 23:  # the old copy, listed
                    return True
            return False

        # Phase 1: the stale LIST receipt is provably on the wire before
        # FINISH, and the matcher must NOT have taken it as the verdict.
        verdict, satisfied = await pump(stale_list_receipt_arrived)
        assert satisfied and verdict is None

        writer.write(ccsds.frame(ccsds.build_file_finish()))
        await writer.drain()

        # Phase 2: the true verdict arrives and describes OUR transfer.
        verdict, _ = await pump(lambda seen: False)
        assert verdict is not None
        assert matcher.is_success(verdict)
        assert verdict["FR_FILE_SIZE"] == len(new_data)
        assert (tmp_path / "files/plan.ats").read_bytes() == new_data
        writer.close()
    finally:
        await server.stop()
