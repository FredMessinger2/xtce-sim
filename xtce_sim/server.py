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
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from xtce_sim import ccsds, codec
from xtce_sim.definition import CommandDef, SimDefinition, resolve_enum_arg
from xtce_sim.fileservice import FileService
from xtce_sim.seqservice import SequenceCommandError, SequenceService

# A command handler receives the server, the decoded command, and its argument
# values, and may send telemetry via the server. It returns nothing.
CommandHandler = Callable[["SimServer", CommandDef, dict], Awaitable[None]]

#: Conventional command names that gate the autonomous beacon, the same
#: opt-in-by-declaration pattern as FILE_COMMANDS and SEQUENCE_COMMANDS: a
#: vehicle whose XTCE declares ENABLE_BEACON (with an ENABLE/DISABLE
#: ``BeaconState`` argument) gets real beacon control; a vehicle that
#: doesn't is untouched. DISABLE stops the periodic beacon only — physics
#: keep ticking, and command-caused emissions (echo, immediate effects,
#: file receipts, sequence status) still flow, whether the command came
#: from the ground or from a RUNNING ATS/RTS: beacon-off silences the
#: beacon, it does not pause the vehicle's stored program, so a running
#: sequence keeps transmitting its fires' echoes and effects on its own
#: clock.
BEACON_COMMANDS = ("ENABLE_BEACON",)

#: Conventional command names that request an immediate telemetry snapshot:
#: one on-demand pass over every periodically-downlinked packet, exactly
#: what a beacon pass sends. Deliberately independent of the beacon gate —
#: this is how the ground polls a vehicle whose beacon is disabled. Same
#: opt-in-by-declaration rule as BEACON_COMMANDS.
STATUS_COMMANDS = ("GET_STATUS",)

#: Conventional command names that retime one periodic packet's beacon
#: period in flight — the command-side of the XTCE DefaultRateInStream
#: declarations, PUS-style (per-packet periods, never one global rate).
#: Contract: a ``Packet`` argument whose enumerated labels name the
#: periodic packets (label value = APID) and a ``PeriodMs`` duration.
#: Same opt-in-by-declaration rule as BEACON_COMMANDS.
TLM_RATE_COMMANDS = ("SET_TLM_RATE",)

#: Hard floor on a commanded beacon period. The ICD's ValidRange guards
#: wire commands where one is declared; this guards every vehicle against
#: a period faster than the scheduler's own wake clamp (a downlink flood).
_TLM_PERIOD_FLOOR_S = 0.05


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
        # Autonomous beacon mode: ENABLE_BEACON (BEACON_COMMANDS) flips this.
        # False silences the periodic beacon only; command-caused emissions
        # still flow and the behavior engine keeps ticking.
        self.beacon_enabled = True
        # Per-packet beacon periods (seconds): each packet's declared XTCE
        # DefaultRateInStream period, falling back to --interval where the
        # ICD declares none. SET_TLM_RATE (TLM_RATE_COMMANDS) retimes one
        # packet live. The schedule itself (next-due times) lives with the
        # beacon loop; _beacon_wake nudges it out of a long sleep when a
        # period changes or the beacon re-enables.
        self._tlm_periods: dict[int, float] = {
            p.apid: (p.period_s or beacon_interval) for p in simdef.packets
        }
        self._next_due: dict[int, float] = dict.fromkeys(self._tlm_periods, 0.0)
        self._beacon_wake = asyncio.Event()
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
        # Nudged whenever the sequencer's state may have changed (a
        # LOAD/START/STOP/ABORT arrived, accepted or refused), so the waiter
        # recomputes its sleep and pushes the status packets.
        self._seq_wake = asyncio.Event()

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
        """Beacon every periodic packet on its own declared period.

        Each packet paces on ``_tlm_periods`` — its XTCE DefaultRateInStream
        period, or the --interval fallback — and SET_TLM_RATE retimes one
        live. Schedules advance whether or not anyone listens and whether or
        not the beacon is enabled: the vehicle's clock does not stop, the
        gate and the empty-client check only decide if a due packet
        transmits. ``_beacon_wake`` nudges the sleep when a period changes
        or the beacon re-enables, so a retime never waits out the old period.

        On cancellation (server stop) the CancelledError raised inside the
        wait propagates out and marks the task cancelled — it is not
        swallowed. ``stop()`` awaits the task and suppresses it there.
        """
        last_tick = time.monotonic()
        while True:
            now = time.monotonic()
            # Physics advance regardless of connected clients: the vehicle
            # keeps warming/cooling whether or not anyone is watching. Guarded
            # like the sends below — a behavior bug must not kill the beacon.
            if self.behavior_engine is not None:
                try:
                    self.behavior_engine.tick(now - last_tick)
                except Exception:
                    self.logger.exception("behavior tick failed")
            last_tick = now
            self._emit_due_packets(now)
            # A commands-only definition has nothing to schedule; keep the
            # loop alive at the fallback interval so physics still tick.
            if self._next_due:
                delay = max(0.01, min(self._next_due.values()) - time.monotonic())
            else:
                delay = self.beacon_interval
            # asyncio.timeout, NOT wait_for: on Python 3.11, wait_for can
            # swallow a task cancellation that races a completing wait (the
            # wake fires as stop() cancels) — the loop would then never
            # unwind and stop() would hang awaiting it forever.
            try:
                async with asyncio.timeout(delay):
                    await self._beacon_wake.wait()
            except TimeoutError:
                pass
            self._beacon_wake.clear()

    def _emit_due_packets(self, now: float) -> None:
        """Send every schedule-due packet and re-arm its period."""
        for apid in sorted(a for a, t in self._next_due.items() if t <= now):
            # Pace from the DUE time, not the wake time, so loop latency
            # never accumulates into the delivered rate (the declared rate
            # is the ICD's guaranteed minimum). If we fell more than one
            # period behind (retime-now sentinel, suspended host), re-anchor
            # to now instead of bursting the backlog.
            nxt = self._next_due[apid] + self._tlm_periods[apid]
            if nxt <= now:
                nxt = now + self._tlm_periods[apid]
            self._next_due[apid] = nxt
            packet_def = self.simdef.packet_by_apid(apid)
            if packet_def is None or self._event_only(packet_def):
                continue  # event packets downlink on events, never on time
            if not (self._clients and self.beacon_enabled):
                continue  # schedule advanced; a quiet/unwatched vehicle sends nothing
            # One packet failing (e.g. a bad telemetry_source value) must
            # not kill the beacon loop.
            try:
                self.send_packet(apid)
            except Exception:
                self.logger.exception("beacon: failed to send APID 0x%X", apid)

    def _beacon_packets(self) -> int:
        """One pass over every periodically-downlinked packet.

        Returns how many packets actually sent — each is guarded
        individually, so callers reporting the pass (GET_STATUS) can say
        what really left the vehicle instead of assuming all of it did.
        """
        sent = 0
        for packet_def in self.simdef.packets:
            if self._event_only(packet_def):
                continue
            # One packet failing (e.g. a bad telemetry_source value)
            # must not kill the whole beacon loop.
            try:
                self.send_packet(packet_def.apid)
                sent += 1
            except Exception:
                self.logger.exception(
                    "beacon: failed to send APID 0x%X", packet_def.apid
                )
        return sent

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
                    # asyncio.timeout, NOT wait_for — see _beacon_loop's note
                    # on the 3.11 cancellation-swallowing race; a drain that
                    # completes as stop() cancels would leave this task stuck
                    # in queue.get() forever, hanging shutdown.
                    async with asyncio.timeout(self.write_timeout):
                        await writer.drain()
                except OSError as exc:
                    # OSError covers ConnectionError and TimeoutError (the
                    # asyncio.timeout expiry); all are subclasses on Python 3.11+.
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
            await self._apply_command(command, args)
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

    async def _apply_command(self, command: CommandDef, args: dict) -> None:
        """Hand one validated command to every consumer that claims it."""
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
                    self.logger.exception("immediate: failed to send APID 0x%X", apid)
        if self.file_service is not None and self.file_service.handles(command.name):
            # File-management commands act on the real store; a raise lands
            # in _dispatch's guard and echoes FAILED like any command.
            self._send_file_receipts(self.file_service.handle_command(command.name, args))
        if self.sequence_service is not None and self.sequence_service.handles(command.name):
            # Sequence commands act on the sequencer; the waiter is nudged
            # so it recomputes its sleep and downlinks the changed status.
            verdict = self.sequence_service.handle_command(command.name, args)
            self.logger.info("  sequence: %s", verdict)
            self._seq_wake.set()
        self._apply_link_conventions(command, args)
        if self.command_handler is not None:
            await self.command_handler(self, command, args)

    def _apply_link_conventions(self, command: CommandDef, args: dict) -> None:
        """The link-control conventions: beacon gate, packet retiming, and
        the commanded snapshot (BEACON/TLM_RATE/STATUS_COMMANDS).

        Runs AFTER behavior effects on purpose: the sidecar's emit=immediate
        mirrors have already pushed their packets — for ENABLE_BEACON
        DISABLE, that push is the link's last autonomous packet before the
        beacon goes quiet. (Known limit while quiet: the beacon pass is also
        the only periodic write, so abruptly-vanished clients are not reaped
        until the next transmission — see backlog.)
        """
        if command.name in BEACON_COMMANDS:
            self._set_beacon_enabled(command, args)
        if command.name in TLM_RATE_COMMANDS:
            self._set_tlm_period(command, args)
        if command.name in STATUS_COMMANDS:
            # A commanded snapshot bypasses the beacon gate on purpose:
            # answering the ground is command-caused transmission, and this
            # is the one downlink path an operator has to a quiet vehicle.
            # It transmits whether or not anyone is listening — a commanded
            # emission advances sequence counts between ground contacts
            # exactly as a real vehicle would (the beacon loop's client
            # check is an idle-sim optimization, not a rule).
            if command.params:
                self.logger.warning(
                    "%s declares argument(s) the snapshot convention does not honor "
                    "(%s); emitting the full snapshot",
                    command.name,
                    ", ".join(p.name for p in command.params),
                )
            sent = self._beacon_packets()
            self.logger.info("  status: telemetry snapshot emitted (%d packet(s))", sent)

    def _set_beacon_enabled(self, command: CommandDef, args: dict) -> None:
        """Apply a beacon-gating command's BeaconState argument.

        The convention is label-driven: whatever raw values the vehicle's
        ICD assigns, the declared label ENABLE means on and DISABLE means
        off. A raw wire value resolves through the command's own
        enumeration first (definition.label_for). An argument that is
        missing, unlabeled, or labeled anything else leaves the gate
        alone — guessing about RF silence is worse than ignoring a
        malformed command.
        """
        value = args.get("BeaconState")
        param = next((p for p in command.params if p.name == "BeaconState"), None)
        resolved = (
            resolve_enum_arg(param.enumerations, value)
            if param is not None and param.enumerations
            else None
        )
        wanted = {"ENABLE": True, "DISABLE": False}.get(resolved[0]) if resolved else None
        if wanted is None:
            self.logger.warning(
                "beacon command without a usable BeaconState=%r; ignored", value
            )
            return
        self.beacon_enabled = wanted
        if wanted:
            # Nudge the scheduler out of its sleep so overdue packets flow
            # now, not up to a full period later.
            self._beacon_wake.set()
        self.logger.info(
            "  beacon %s",
            "enabled" if wanted else "disabled (periodic telemetry quiet)",
        )

    def _set_tlm_period(self, command: CommandDef, args: dict) -> None:
        """Apply a telemetry-retiming command: one packet, one new period.

        Label-driven like the beacon gate: the ``Packet`` argument's
        declared label names the packet and its enum value is the APID (the
        ICD carries the mapping — PeriodicPacketIdType). ``PeriodMs`` is a
        duration; range validation already enforced the ICD's bounds for
        wire commands. Anything missing or unresolvable leaves every
        schedule alone.
        """
        packet_arg = args.get("Packet")
        param = next((p for p in command.params if p.name == "Packet"), None)
        resolved = (
            resolve_enum_arg(param.enumerations, packet_arg)
            if param is not None and param.enumerations
            else None
        )
        apid = resolved[1] if resolved else None
        period_ms = args.get("PeriodMs")
        usable_period = (
            isinstance(period_ms, (int, float))
            and not isinstance(period_ms, bool)
            and period_ms > 0
        )
        if apid is None or apid not in self._tlm_periods or not usable_period:
            self.logger.warning(
                "telemetry-rate command without a usable Packet/PeriodMs "
                "(%r, %r); ignored",
                packet_arg,
                period_ms,
            )
            return
        if period_ms / 1000.0 < _TLM_PERIOD_FLOOR_S:
            # The ICD's ValidRange is the real guard for wire commands, but
            # nothing forces a vehicle to declare one — refuse a period the
            # scheduler would turn into a downlink flood.
            self.logger.warning(
                "telemetry-rate period %sms is below the %.0fms floor; ignored",
                period_ms,
                _TLM_PERIOD_FLOOR_S * 1000,
            )
            return
        packet_def = self.simdef.packet_by_apid(apid)
        if packet_def is not None and self._event_only(packet_def):
            # Accepting would log success for a packet the scheduler never
            # sends — refuse loudly instead, like every other unusable arg.
            self.logger.warning(
                "%s is event-only telemetry with no beacon period; ignored",
                packet_def.name,
            )
            return
        self._tlm_periods[apid] = period_ms / 1000.0
        # Due immediately: the retimed packet announces its new cadence now
        # rather than waiting out the old period, and the wake recomputes
        # the loop's sleep.
        self._next_due[apid] = 0.0
        self._beacon_wake.set()
        packet_def = self.simdef.packet_by_apid(apid)
        self.logger.info(
            "  telemetry period: %s every %.3g s",
            packet_def.name if packet_def else f"APID 0x{apid:X}",
            period_ms / 1000.0,
        )

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
        packet = ccsds.build_command_packet(command.opcode, payload)
        return await self._dispatch(packet) == ccsds.ECHO_EXECUTED

    async def _sequence_loop(self) -> None:
        """The waiter: sleep until the sequencer's next deadline, fire, repeat.

        Event-driven, not polled — with nothing RUNNING it sleeps until a
        sequence command nudges ``_seq_wake``. While a deadline is pending
        the sleep is capped (``_SEQ_SLEEP_CAP``) so a wall-clock jump under
        a long sleep is noticed within a second: the sleep DURATION is
        computed once at bedtime, but the deadline is judged against the
        real clock on every pass.

        Status packets push on the event edge: a pass that fired something,
        or one a command woke (accepted or refused — a refused LOAD lands a
        slot in ERROR). Quiet cap-wakes push nothing; the beacon carries
        the moving clock readouts on its own schedule.
        """
        service = self.sequence_service
        while True:
            nudged = self._seq_wake.is_set()
            self._seq_wake.clear()
            try:
                fired = await service.tick(service.clock())
            except Exception:
                # An executor fault is handled inside the sequencer; this
                # catches bugs in the machinery itself. Don't spin hot on a
                # persistent one — the deadline that triggered it is likely
                # still due.
                self.logger.exception("sequencer tick failed")
                await asyncio.sleep(_SEQ_SLEEP_CAP)
                continue
            if fired or nudged:
                self._emit_sequence_status()
            deadline = service.next_deadline()
            timeout = None
            if deadline is not None:
                timeout = max(0.0, min(deadline - service.clock(), _SEQ_SLEEP_CAP))
            # asyncio.timeout, NOT wait_for — same cancellation-swallowing
            # race as the beacon loop's wait (see _beacon_loop): a command
            # nudging _seq_wake as stop() cancels would leave this task
            # unkillable and hang shutdown.
            try:
                async with asyncio.timeout(timeout):
                    await self._seq_wake.wait()
            except TimeoutError:
                pass

    def _emit_sequence_status(self) -> None:
        """Push both status packets now, through the normal packing path.

        The beacon re-sends these on its own schedule regardless; this is
        the event edge — a LOAD, START, fire, completion, or failure shows
        up on the console immediately instead of a beacon later. Sending
        with no explicit values routes through ``_packet_values``, so the
        event edge and the beacon pack a byte-identical packet. Guarded per
        packet like the beacon: one bad packet must not stall the rest.
        """
        for packet_def in self.sequence_service.status_packets:
            try:
                self.send_packet(packet_def.apid)
            except Exception:
                self.logger.exception(
                    "sequence status: failed to send APID 0x%X", packet_def.apid
                )


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
