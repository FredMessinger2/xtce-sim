"""
Asyncio TCP simulator server.

Serves one satellite `SimDefinition` on a single bidirectional TCP port:

- Periodically beacons every telemetry packet to all connected clients.
- Reads length-prefixed command frames, validates CRC, decodes the opcode and
  arguments, and dispatches them to an optional command handler.

Framing is shared with the (later) QUIC transport via `xtce_sim.ccsds`. Command
behaviour is intentionally pluggable — the default handler just logs — so richer
cmd→tlm effects can be layered on without touching the transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from xtce_sim import ccsds, codec
from xtce_sim.definition import CommandDef, SimDefinition

# A command handler receives the server, the decoded command, and its argument
# values, and may send telemetry via the server. It returns nothing.
CommandHandler = Callable[["SimServer", CommandDef, dict], Awaitable[None]]


@dataclass
class _ClientConn:
    """A connected client: its writer, a bounded outbound queue, and the single
    task that drains that queue. All writes to a given client go through its one
    task, so a beacon and a command-response never `drain()` the same writer
    concurrently, and a slow client only backs up its own queue."""

    writer: asyncio.StreamWriter
    queue: "asyncio.Queue[bytes]"
    task: Optional[asyncio.Task] = None


class SimServer:
    """A running simulator instance bound to one TCP port."""

    def __init__(
        self,
        simdef: SimDefinition,
        *,
        host: str = "127.0.0.1",
        port: int,
        beacon_interval: float = 1.0,
        write_timeout: float = 5.0,
        queue_maxsize: int = 256,
        command_handler: Optional[CommandHandler] = None,
        telemetry_source: Optional[Callable[[object], dict]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.simdef = simdef
        self.host = host
        self.port = port
        self.beacon_interval = beacon_interval
        # A client whose write doesn't drain within this many seconds is dropped,
        # so one unresponsive client can't stall telemetry for others.
        self.write_timeout = write_timeout
        # Max packets buffered per client; a client that can't keep up past this
        # is dropped rather than growing memory without bound.
        self.queue_maxsize = queue_maxsize
        self.command_handler = command_handler
        # Optional source of telemetry field values: source(packet_def) -> dict.
        # When None, packets beacon zeros.
        self.telemetry_source = telemetry_source
        self.logger = logger or logging.getLogger("xtce_sim")

        self._clients: dict[asyncio.StreamWriter, _ClientConn] = {}
        self._seq = ccsds.SequenceCounter()
        self._server: Optional[asyncio.AbstractServer] = None
        self._beacon_task: Optional[asyncio.Task] = None

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind the port and begin accepting connections and beaconing."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self._beacon_task = asyncio.create_task(self._beacon_loop())
        # Surface an unexpected beacon death instead of losing it to GC.
        self._beacon_task.add_done_callback(self._on_beacon_done)
        self.logger.info(
            "listening on %s:%d — %d command(s), %d packet(s)",
            self.host,
            self.port,
            len(self.simdef.commands),
            len(self.simdef.packets),
        )

    async def serve_forever(self) -> None:
        """Start and serve until cancelled, always cleaning up on the way out."""
        if self._server is None:
            await self.start()
        assert self._server is not None
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop beaconing, drop all clients, and close the listener.

        The beacon task is cancelled *and awaited* so it fully unwinds before we
        return. Clients are closed *before* awaiting ``wait_closed()``: on Python
        3.12+ the server waits for active connections to finish, so an idle
        connected client would otherwise deadlock shutdown (e.g. Ctrl-C on
        ``run``). Safe to call more than once.
        """
        if self._beacon_task:
            self._beacon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beacon_task
        conns = list(self._clients.values())
        for conn in conns:
            if conn.task is not None:
                conn.task.cancel()
            conn.writer.close()
        for conn in conns:
            if conn.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await conn.task
        self._clients.clear()
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except (TimeoutError, asyncio.TimeoutError):
                pass  # a stuck handler shouldn't block shutdown

    def _on_beacon_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.error("beacon loop terminated unexpectedly: %r", exc)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def bound_port(self) -> int:
        """The actual port the listener is bound to (useful with port=0)."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("server not started")
        return self._server.sockets[0].getsockname()[1]

    # ---- telemetry ---------------------------------------------------------

    async def send_packet(
        self,
        apid: int,
        values: Optional[dict] = None,
        *,
        writer: Optional[asyncio.StreamWriter] = None,
    ) -> None:
        """Send one telemetry packet to a single client, or broadcast to all."""
        packet_def = self.simdef.packet_by_apid(apid)
        if packet_def is None:
            self.logger.warning("send_packet: unknown APID 0x%X", apid)
            return
        if values is None and self.telemetry_source is not None:
            values = self.telemetry_source(packet_def)
        payload = codec.pack_telemetry(packet_def, values)
        pkt = ccsds.build_telemetry_packet(apid, payload, self._seq.next(apid))
        wire = ccsds.frame(pkt)

        # Enqueue is non-blocking: the beacon never waits on a slow client, and
        # each client's single writer task serializes its own drains.
        if writer is not None:
            conn = self._clients.get(writer)
            if conn is not None:
                self._enqueue(conn, wire)
        else:
            for conn in list(self._clients.values()):
                self._enqueue(conn, wire)

    def _enqueue(self, conn: _ClientConn, data: bytes) -> None:
        try:
            conn.queue.put_nowait(data)
        except asyncio.QueueFull:
            # Client can't keep up — drop it rather than grow memory unbounded.
            peer = conn.writer.get_extra_info("peername")
            self.logger.debug("client %s can't keep up (queue full), dropping", peer)
            self._clients.pop(conn.writer, None)
            if conn.task is not None:
                conn.task.cancel()
            conn.writer.close()

    async def _beacon_loop(self) -> None:
        """Broadcast every telemetry packet on a fixed interval.

        On cancellation (server stop) the CancelledError raised inside the
        ``sleep`` propagates out and marks the task cancelled — it is not
        swallowed. ``stop()`` awaits the task and suppresses it there.
        """
        while True:
            if self._clients:
                for packet_def in self.simdef.packets:
                    # One packet failing (e.g. a bad telemetry_source value)
                    # must not kill the whole beacon loop.
                    try:
                        await self.send_packet(packet_def.apid)
                    except Exception:
                        self.logger.exception(
                            "beacon: failed to send APID 0x%X", packet_def.apid
                        )
            await asyncio.sleep(self.beacon_interval)

    async def _client_writer(self, conn: _ClientConn) -> None:
        """Drain one client's outbound queue, one write at a time."""
        writer = conn.writer
        try:
            while True:
                data = await conn.queue.get()
                try:
                    writer.write(data)
                    await asyncio.wait_for(writer.drain(), self.write_timeout)
                except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
                    self.logger.debug("dropping unresponsive client: %s", exc)
                    break
                except Exception:
                    # Never let the task complete with an unexpected exception —
                    # stop() awaits these tasks and would otherwise abort cleanup.
                    self.logger.exception("unexpected client write error; dropping")
                    break
        finally:
            # Runs on normal exit, on drop-via-break, and on cancellation.
            # CancelledError is not caught here, so it propagates and the task
            # is properly marked cancelled; stop() suppresses it when awaiting.
            self._clients.pop(writer, None)
            writer.close()

    # ---- commands ----------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        conn = _ClientConn(writer=writer, queue=asyncio.Queue(maxsize=self.queue_maxsize))
        conn.task = asyncio.create_task(self._client_writer(conn))
        self._clients[writer] = conn
        self.logger.info("client connected: %s (%d total)", peer, len(self._clients))

        buffer = b""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data
                try:
                    packets, buffer = ccsds.deframe(buffer)
                except ccsds.FrameError as exc:
                    # Corrupt stream — we can't resync reliably, so drop it.
                    self.logger.warning("framing error from %s: %s", peer, exc)
                    break
                for packet in packets:
                    await self._dispatch(packet, writer)
        except (ConnectionError, OSError) as exc:
            self.logger.debug("client %s read error: %s", peer, exc)
        finally:
            self._clients.pop(writer, None)
            conn.task.cancel()
            writer.close()
            self.logger.info("client disconnected: %s (%d total)", peer, len(self._clients))

    async def _dispatch(self, packet: bytes, writer: asyncio.StreamWriter) -> None:
        opcode, payload = ccsds.parse_command_packet(packet)
        if opcode is None:
            self.logger.warning("received undecodable command packet (%d bytes)", len(packet))
            return

        command = self.simdef.command_by_opcode(opcode)
        if command is None:
            self.logger.warning("received unknown opcode 0x%02X", opcode)
            return

        # Decoding and the (arbitrary) command handler both run under one guard:
        # a failure on one command must not tear down the client's connection.
        try:
            args = codec.decode_command(command, payload)
            self.logger.info("command 0x%02X %s args=%s", opcode, command.name, args)
            if self.command_handler is not None:
                await self.command_handler(self, command, args)
        except Exception:
            self.logger.exception("error handling command 0x%02X %s", opcode, command.name)


async def run(
    simdef: SimDefinition,
    *,
    host: str = "127.0.0.1",
    port: int,
    beacon_interval: float = 1.0,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Convenience entrypoint: serve ``simdef`` until cancelled/interrupted."""
    server = SimServer(
        simdef,
        host=host,
        port=port,
        beacon_interval=beacon_interval,
        logger=logger,
    )
    await server.serve_forever()
