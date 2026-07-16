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
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from xtce_sim import ccsds, codec
from xtce_sim.definition import CommandDef, SimDefinition
from xtce_sim.fileservice import FileService
from xtce_sim.seqservice import SequenceCommandError, SequenceService, steady_view

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
        behavior_engine: Optional[object] = None,
        file_service: Optional[FileService] = None,
        sequence_service: Optional[SequenceService] = None,
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
        # Optional behavior.BehaviorEngine: commands mutate its overlay, and
        # the overlay wins over telemetry_source values at pack time.
        self.behavior_engine = behavior_engine
        # Optional fileservice.FileService: receives file-uplink frames and
        # FILE_* commands, and owns the FILE_RECEIPT packet's beacon values
        # (single-writer, like a dynamics model owns its output fields).
        self.file_service = file_service
        # Optional seqservice.SequenceService: claims the ATS/RTS commands
        # and owns the two status packets' values (single-writer). Fired
        # commands re-enter _dispatch as real packets via the executor bound
        # here, so a sequence fire is byte-identical to a ground command.
        self.sequence_service = sequence_service
        if sequence_service is not None:
            sequence_service.bind_executor(self._sequence_execute)
        self.logger = logger or logging.getLogger("xtce_sim")
        self._uplink_warned = False

        self._clients: dict[asyncio.StreamWriter, _ClientConn] = {}
        self._seq = ccsds.SequenceCounter()
        self._server: Optional[asyncio.AbstractServer] = None
        self._beacon_task: Optional[asyncio.Task] = None
        self._sequencer_task: Optional[asyncio.Task] = None
        # Nudged whenever the sequencer's schedule may have changed (a
        # LOAD/START/STOP/ABORT arrived), so the waiter recomputes its sleep.
        self._seq_wake = asyncio.Event()
        # Last steady view emitted per status APID — the change detector
        # behind event-driven status downlinks.
        self._seq_status_sent: dict[int, dict] = {}

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind the port and begin accepting connections and beaconing."""
        # The generator refuses definitions that claim a reserved link APID,
        # but a hand-edited cmd_tlm.json can sneak one past it — warn loudly,
        # since that packet's beacons would masquerade as link protocol.
        for apid, purpose in ccsds.RESERVED_APIDS.items():
            claimed = self.simdef.packet_by_apid(apid)
            if claimed is not None:
                self.logger.warning(
                    "packet %r uses APID 0x%X, reserved for the %s — "
                    "ground tools will misread it",
                    claimed.name,
                    apid,
                    purpose,
                )
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self._beacon_task = asyncio.create_task(self._beacon_loop())
        # Surface an unexpected beacon death instead of losing it to GC.
        self._beacon_task.add_done_callback(self._on_beacon_done)
        if self.sequence_service is not None:
            self._sequencer_task = asyncio.create_task(self._sequence_loop())
            self._sequencer_task.add_done_callback(self._on_sequencer_done)
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
        if self._sequencer_task:
            self._sequencer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sequencer_task
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

    def _on_sequencer_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.error("sequencer loop terminated unexpectedly: %r", exc)

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

    def send_packet(
        self,
        apid: int,
        values: Optional[dict] = None,
        *,
        writer: Optional[asyncio.StreamWriter] = None,
    ) -> None:
        """Send one telemetry packet to a single client, or broadcast to all.

        Synchronous: it only builds the packet and enqueues it (the per-client
        writer task does the actual awaiting I/O), so it never blocks or yields.
        """
        packet_def = self.simdef.packet_by_apid(apid)
        if packet_def is None:
            self.logger.warning("send_packet: unknown APID 0x%X", apid)
            return
        if values is None:
            values = self._packet_values(packet_def)
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

    def _packet_values(self, packet_def) -> Optional[dict]:
        """Field values for one packet: service overlays over synthetic base.

        The synthetic layer (``--live`` values, or nothing) supplies defaults;
        any field the behavior engine holds wins over it, and the sequence
        service wins over both for the status fields it is the single writer
        of. Returns None when no layer has anything, so the packet packs as
        zeros exactly as before.
        """
        base = (self.telemetry_source(packet_def) if self.telemetry_source else {}) or {}
        overlay = (
            self.behavior_engine.values_for(packet_def) if self.behavior_engine else {}
        )
        seq = (
            self.sequence_service.values_for(packet_def)
            if self.sequence_service is not None
            else {}
        )
        merged = {**base, **overlay, **seq}
        return merged or None

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
            # Physics advance regardless of connected clients: the vehicle
            # keeps warming/cooling whether or not anyone is watching. Guarded
            # like the sends below — a behavior bug must not kill the beacon.
            if self.behavior_engine is not None:
                try:
                    self.behavior_engine.tick(self.beacon_interval)
                except Exception:
                    self.logger.exception("behavior tick failed")
            if self._clients:
                self._beacon_packets()
            await asyncio.sleep(self.beacon_interval)

    def _beacon_packets(self) -> None:
        """One beacon pass over every periodically-downlinked packet."""
        for packet_def in self.simdef.packets:
            if self._event_only(packet_def):
                continue
            # One packet failing (e.g. a bad telemetry_source value)
            # must not kill the whole beacon loop.
            try:
                self.send_packet(packet_def.apid)
            except Exception:
                self.logger.exception(
                    "beacon: failed to send APID 0x%X", packet_def.apid
                )

    def _event_only(self, packet_def) -> bool:
        """Whether this packet downlinks on events rather than the beacon.

        The FILE_RECEIPT packet is the file service's event report: beaconing
        it would erase the last event from every console a second after it
        happened, and repeat stale verdicts at any ground still listening —
        real flight software downlinks file receipts on event and answers
        storage queries on demand (FILE_STATUS)."""
        return (
            self.file_service is not None
            and packet_def.apid == self.file_service.receipt_apid
        )

    async def _client_writer(self, conn: _ClientConn) -> None:
        """Drain one client's outbound queue, one write at a time."""
        writer = conn.writer
        try:
            while True:
                data = await conn.queue.get()
                try:
                    writer.write(data)
                    await asyncio.wait_for(writer.drain(), self.write_timeout)
                except OSError as exc:
                    # OSError covers ConnectionError and TimeoutError (the
                    # wait_for timeout); all are subclasses on Python 3.11+.
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
                    if _is_file_uplink(packet):
                        # Link protocol, not a vehicle command: file chunks
                        # never enter command dispatch (their payloads are
                        # not opcode + arguments). Keyed by the writer — the
                        # same hashable identity the client dict uses.
                        self._handle_file_uplink(writer, packet)
                    else:
                        await self._dispatch(packet)
        except OSError as exc:
            self.logger.debug("client %s read error: %s", peer, exc)
        finally:
            self._clients.pop(writer, None)
            conn.task.cancel()
            writer.close()
            if self.file_service is not None:
                # A dropped link ends its in-progress transfer, receipt and all.
                try:
                    self._send_file_receipts(self.file_service.connection_closed(writer))
                except Exception:
                    self.logger.exception("file transfer cleanup failed")
            self.logger.info("client disconnected: %s (%d total)", peer, len(self._clients))

    # ---- file uplink ---------------------------------------------------------

    def _handle_file_uplink(self, source: object, packet: bytes) -> None:
        """Feed one file-uplink frame to the file service; broadcast the
        receipts it answers with. Guarded: a file bug must not drop the link."""
        if self.file_service is None:
            if not self._uplink_warned:
                # Warn once, not once per chunk — an upload is many frames.
                self._uplink_warned = True
                self.logger.warning(
                    "file uplink received but no file store is configured — "
                    "dropped (further occurrences suppressed)"
                )
            return
        try:
            self._send_file_receipts(self.file_service.handle_uplink(source, packet))
        except Exception:
            self.logger.exception("file uplink handling failed")

    def _send_file_receipts(self, receipts: list[dict]) -> None:
        """Broadcast each receipt the file service produced (guarded per
        packet, like the beacon: one bad receipt must not drop the rest)."""
        apid = self.file_service.receipt_apid
        if apid is None:
            return  # log-only vehicle: the service already told the story
        for values in receipts:
            try:
                self.send_packet(apid, values)
            except Exception:
                self.logger.exception("failed to send FILE_RECEIPT")

    def _echo_command(self, packet: bytes, status: int) -> None:
        """Broadcast a command echo (see ccsds.py) — the ground's view of
        what arrived and what became of it. Guarded: echo failure must not
        affect command handling."""
        try:
            pkt = ccsds.build_command_echo(
                packet, status, self._seq.next(ccsds.CMD_ECHO_APID)
            )
            wire = ccsds.frame(pkt)
            # Snapshot first: _enqueue drops clients that can't keep up,
            # which would mutate the dict mid-iteration.
            conns = list(self._clients.values())
            for conn in conns:
                self._enqueue(conn, wire)
        except Exception:
            self.logger.exception("command echo failed")

    async def _dispatch(self, packet: bytes) -> int:
        """Validate and execute one command packet; returns the echo status.

        Both entrances converge here — a client's uplink and the sequencer's
        executor — so a sequence-fired command is validated, applied, and
        echoed exactly like a ground one. The return value is how the
        executor learns the verdict it reports back to the sequencer.
        """
        opcode, payload = ccsds.parse_command_packet(packet)
        if opcode is None:
            self.logger.warning("received undecodable command packet (%d bytes)", len(packet))
            self._echo_command(packet, ccsds.ECHO_FAILED)
            return ccsds.ECHO_FAILED

        command = self.simdef.command_by_opcode(opcode)
        if command is None:
            self.logger.warning("received unknown opcode 0x%02X", opcode)
            self._echo_command(packet, ccsds.ECHO_UNKNOWN_OPCODE)
            return ccsds.ECHO_UNKNOWN_OPCODE

        # Decoding, behavior effects, and the (arbitrary) command handler all
        # run under one guard: a failure on one command must not tear down the
        # client's connection.
        try:
            args = codec.decode_command(command, payload)
            # The vehicle validates for itself — it does not trust the ground.
            # Out-of-range arguments reject the command before any effect
            # applies; a truncated payload whose zero-padding lands outside a
            # declared range rejects here too.
            violations = codec.range_violations(command, args)
            if violations:
                self.logger.warning(
                    "rejected 0x%02X %s: %s", opcode, command.name, "; ".join(violations)
                )
                self._echo_command(packet, ccsds.ECHO_REJECTED)
                return ccsds.ECHO_REJECTED
            self.logger.info("command 0x%02X %s args=%s", opcode, command.name, args)
            if self.behavior_engine is not None:
                applied = self.behavior_engine.apply_command(command, args)
                if applied:
                    self.logger.info("  effects: %s", ", ".join(applied))
                # Effects marked emit = "immediate" push their packet out now,
                # as an extra emission; the beacon keeps its own schedule.
                # Guarded per packet like the beacon: one bad packet must not
                # drop the others or the command handler.
                for apid in sorted(self.behavior_engine.pop_immediate_apids()):
                    try:
                        self.send_packet(apid)
                        self.logger.info("  immediate: APID 0x%X emitted", apid)
                    except Exception:
                        self.logger.exception(
                            "immediate: failed to send APID 0x%X", apid
                        )
            if self.file_service is not None and self.file_service.handles(command.name):
                # File-management commands act on the real store; a raise
                # lands in the guard below and echoes FAILED like any command.
                self._send_file_receipts(self.file_service.handle_command(command.name, args))
            if self.sequence_service is not None and self.sequence_service.handles(command.name):
                # Sequence commands act on the sequencer; the waiter is
                # nudged so it recomputes its sleep and downlinks the
                # changed status packet.
                verdict = self.sequence_service.handle_command(command.name, args)
                self.logger.info("  sequence: %s", verdict)
                self._seq_wake.set()
            if self.command_handler is not None:
                await self.command_handler(self, command, args)
        except SequenceCommandError as exc:
            # An operational refusal (nothing loaded, missing file, bad
            # plan), not a software fault: no traceback, but the slot state
            # may have changed (a failed LOAD lands in ERROR) — wake the
            # waiter so the console sees it.
            self.logger.warning("refused %s: %s", command.name, exc)
            self._seq_wake.set()
            self._echo_command(packet, ccsds.ECHO_FAILED)
            return ccsds.ECHO_FAILED
        except Exception:
            self.logger.exception("error handling command 0x%02X %s", opcode, command.name)
            self._echo_command(packet, ccsds.ECHO_FAILED)
            return ccsds.ECHO_FAILED
        self._echo_command(packet, ccsds.ECHO_EXECUTED)
        return ccsds.ECHO_EXECUTED

    # ---- sequencing ----------------------------------------------------------

    async def _sequence_execute(self, name: str, args: dict) -> bool:
        """The sequencer's executor: encode the fired entry as a REAL command
        packet and push it through ``_dispatch``, byte-identical to the same
        command sent from the ground — range validation, behavior effects,
        immediate emissions, and the command echo all happen exactly once,
        exactly as they would for an uplink. Success is the echo verdict.
        """
        command = self.simdef.command_by_name(name)
        if command is None:
            # LOAD validated every entry against the definition, so this
            # means the definition itself changed underneath a loaded plan.
            self.logger.error("sequence fired %s, which this vehicle does not define", name)
            return False
        try:
            payload = codec.encode_command(command, args)
        except (ValueError, struct.error) as exc:
            self.logger.error("sequence entry %s failed to encode: %s", name, exc)
            return False
        packet = (
            ccsds.CCSDSHeader(packet_type=int(ccsds.PacketType.COMMAND), apid=1).pack()
            + bytes([command.opcode])
            + payload
        )
        return await self._dispatch(packet) == ccsds.ECHO_EXECUTED

    async def _sequence_loop(self) -> None:
        """The waiter: sleep until the sequencer's next deadline, fire, repeat.

        Event-driven, not polled — with nothing RUNNING it sleeps until a
        sequence command nudges ``_seq_wake``. While a deadline is pending
        the sleep is capped (``_SEQ_SLEEP_CAP``) so a wall-clock jump under
        a long sleep is noticed within a second: the sleep DURATION is
        computed once at bedtime, but the deadline is judged against the
        real clock on every pass.
        """
        service = self.sequence_service
        while True:
            try:
                fired = await service.tick(service.clock())
            except Exception:
                # An executor fault is handled inside the sequencer; this
                # catches bugs in the machinery itself. Don't spin hot on a
                # persistent one — the deadline that triggered it is likely
                # still due.
                self.logger.exception("sequencer tick failed")
                fired = []
                await asyncio.sleep(_SEQ_SLEEP_CAP)
            if fired:
                self.logger.info(
                    "sequencer fired %d command(s) this pass", len(fired)
                )
            self._emit_sequence_status()
            deadline = service.next_deadline()
            timeout = None
            if deadline is not None:
                timeout = max(0.0, min(deadline - service.clock(), _SEQ_SLEEP_CAP))
            try:
                await asyncio.wait_for(self._seq_wake.wait(), timeout)
            except TimeoutError:
                pass
            self._seq_wake.clear()

    def _emit_sequence_status(self) -> None:
        """Downlink each status packet the moment its steady view changes.

        The beacon re-sends these on its own schedule regardless; this is
        the event edge — a LOAD, START, fire, completion, or failure shows
        up on the console immediately instead of a beacon later. Guarded
        per packet like the beacon: one bad packet must not stall the rest.
        """
        for apid in sorted(self.sequence_service.status_apids):
            packet_def = self.simdef.packet_by_apid(apid)
            values = self.sequence_service.values_for(packet_def)
            steady = steady_view(values)
            if self._seq_status_sent.get(apid) == steady:
                continue
            self._seq_status_sent[apid] = steady
            try:
                self.send_packet(apid, values)
            except Exception:
                self.logger.exception("sequence status: failed to send APID 0x%X", apid)


#: While a sequence deadline is pending, never sleep longer than this — the
#: re-check is what bounds how long an NTP step or suspend/resume can fool
#: the waiter's precomputed sleep.
_SEQ_SLEEP_CAP = 1.0


def _is_file_uplink(packet: bytes) -> bool:
    """Whether this CCSDS packet rides the reserved file-uplink APID."""
    if len(packet) < 6:
        return False
    return ccsds.CCSDSHeader.unpack(packet[:6]).apid == ccsds.FILE_UPLINK_APID


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
