"""
The sequence service: the wire between the ICD's eight ATS/RTS commands,
the file store, the Sequencer, and the two status packets.

Division of labor, so nothing is owned twice:

- ``sequences``    parses plan text (strict-and-total, codec-validated).
- ``fileservice``  stores what the ground uplinked (the directory jail).
- ``sequencer``    is the state machine — slots, deadlines, firing.
- THIS module      routes commands to the machine, reads LOAD's file out of
                   the store, and turns ``Sequencer.status()`` into
                   pack-ready values for the vehicle's own status packets.
- ``server``       supplies the executor (a fired command re-enters the
                   normal dispatch path as a real packet) and the waiter
                   task that calls ``tick`` when the next deadline is due.

The status packets are found by name (``ATS_STATUS`` / ``RTS_STATUS``),
exactly as the file service finds ``FILE_RECEIPT``: a vehicle that does not
declare them still sequences — its lifecycle is visible in the log only.
The sequencer is the single writer of every field it maps; the mapping is
driven by the packet's own field list, so a leaner declaration (fewer
status fields) is a valid configuration, not an error.

A refused command — START with nothing loaded, STOP of an idle slot, LOAD
of a missing or unparseable file — raises ``SequenceCommandError``, which
dispatch's guard turns into a FAILED command echo, the same honesty the
file service practices. (A bad SeqId never gets this far: the ICD declares
ValidRange 1..1 and dispatch's range validation rejects it first.) A
failed LOAD additionally lands the slot in ERROR, naming the plan that
refused.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Optional

from xtce_sim import sequences
from xtce_sim.fileservice import FileStore, decode_filename_arg
from xtce_sim.sequencer import Fired, Sequencer
from xtce_sim.sequences import Sequence

#: Command name -> (slot kind, sequencer verb). ``handles`` claims exactly
#: these; everything else in the ICD passes the service untouched.
SEQUENCE_COMMANDS = {
    "LOAD_ATS": ("ats", "load"),
    "START_ATS": ("ats", "start"),
    "STOP_ATS": ("ats", "stop"),
    "ABORT_ATS": ("ats", "abort"),
    "LOAD_RTS": ("rts", "load"),
    "START_RTS": ("rts", "start"),
    "STOP_RTS": ("rts", "stop"),
    "ABORT_RTS": ("rts", "abort"),
}

_STATUS_PACKET_NAMES = {"ats": "ATS_STATUS", "rts": "RTS_STATUS"}
_PARSERS = {"ats": sequences.parse_ats, "rts": sequences.parse_rts}

#: Executor type re-exported for the server: fired commands travel it.
Executor = Callable[[str, dict], Awaitable[bool]]


class SequenceCommandError(Exception):
    """A sequence command the vehicle refused; dispatch echoes FAILED."""


#: Field-name suffixes that move on their own while a sequence runs (the
#: packet timestamp, the elapsed counter). Everything else only moves when
#: something HAPPENED — a load, a start, a fire, a completion.
_STEADY_EXCLUDES = ("TIMESTAMP", "ELAPSED_SEC")


def steady_view(values: dict) -> dict:
    """The subset of status values whose change warrants an immediate
    downlink. The beacon carries the moving clock readouts on its own
    schedule; pushing a packet for every elapsed-second tick would be
    noise, but pushing one the instant a state or counter changes is how
    the console reflects sequencer activity without waiting a beacon."""
    return {
        name: value
        for name, value in values.items()
        if not name.endswith(_STEADY_EXCLUDES)
    }


class SequenceService:
    """Owns one vehicle's Sequencer and its command/telemetry plumbing.

    ``clock`` is injected like the file service's (wall-clock UTC is the
    vehicle's ATS time base for v1). ``bind_executor`` is called by the
    server before any sequence can run, closing the construction cycle
    (the service needs the server's dispatch, the server needs the
    service).
    """

    def __init__(
        self,
        store: FileStore,
        simdef,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.store = store
        self.simdef = simdef
        # Public: the server's waiter task computes its sleep from the same
        # clock the deadlines are judged by (wall-clock UTC in v1).
        self.clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._saturation_warned: set[str] = set()
        self._execute: Optional[Executor] = None
        self.sequencer = Sequencer(self._run_fired)
        self._packets = {
            kind: simdef.packet_by_name(name)
            for kind, name in _STATUS_PACKET_NAMES.items()
        }
        for kind, name in _STATUS_PACKET_NAMES.items():
            if self._packets[kind] is None:
                self._logger.info(
                    "definition declares no %s packet; %s lifecycle will be log-only",
                    name,
                    kind.upper(),
                )

    def bind_executor(self, execute: Executor) -> None:
        """Install the dispatch path fired commands travel (server-owned)."""
        self._execute = execute

    async def _run_fired(self, name: str, args: dict) -> bool:
        if self._execute is None:
            raise RuntimeError("sequence executor is not bound — nothing can dispatch")
        return await self._execute(name, args)

    # -- commands ----------------------------------------------------------------

    def handles(self, command_name: str) -> bool:
        """Whether this command belongs to the sequence service."""
        return command_name in SEQUENCE_COMMANDS

    def handle_command(self, command_name: str, args: dict) -> str:
        """Route one decoded sequence command; returns the human verdict.

        Raises ``SequenceCommandError`` on refusal.
        """
        kind, verb = SEQUENCE_COMMANDS[command_name]
        if verb == "load":
            return self._cmd_load(kind, args)
        if verb == "start":
            ok, msg = self.sequencer.start(kind, self.clock())
        elif verb == "stop":
            ok, msg = self.sequencer.stop(kind)
        else:
            ok, msg = self.sequencer.abort(kind)
        if not ok:
            raise SequenceCommandError(msg)
        return msg

    def _cmd_load(self, kind: str, args: dict) -> str:
        """LOAD: read the plan out of the vehicle's own store and install it.

        A failure at any step lands the slot in ERROR — the operator asked
        to replace the slot's content, and 'the replacement was bad' is slot
        state, not a shrug (the XTCE declares the state; this is the only
        path that reaches it). The parsed plan is held in memory: deleting
        or re-uploading the file afterwards cannot touch a loaded sequence.
        The one exception is a LOAD against a RUNNING slot, refused before
        the file is even considered — a bad load attempt must not tear down
        the plan currently executing.
        """
        name = decode_filename_arg(args.get("Filename"))
        plan, problem = self._read_plan(kind, name)
        if problem is None:
            ok, msg = self.sequencer.load(plan)
            if not ok:
                raise SequenceCommandError(msg)
            return msg
        _, msg = self.sequencer.load_failed(kind, name or "(no filename)", problem)
        raise SequenceCommandError(msg)

    def _read_plan(self, kind: str, name: str) -> tuple[Optional[Sequence], Optional[str]]:
        """The named plan parsed for ``kind``, or why it cannot load."""
        if not name:
            return None, "no Filename argument"
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        ext_kind = sequences.KINDS.get(ext)
        if ext_kind is not None and ext_kind != kind:
            return None, f"{name} is an {ext_kind.upper()} plan — this slot takes .{kind} files"
        try:
            data = self.store.read(name)
        except FileNotFoundError:
            return None, "no such file in the store — upload it first"
        except ValueError as exc:  # a name the jail refuses
            return None, str(exc)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, f"not a text file ({exc})"
        try:
            return _PARSERS[kind](text, name, self.simdef), None
        except sequences.SequenceError as exc:
            return None, str(exc)

    # -- time --------------------------------------------------------------------

    async def tick(self, now: float) -> list[Fired]:
        """Fire everything due; the server's waiter calls this."""
        return await self.sequencer.tick(now)

    def next_deadline(self) -> Optional[float]:
        """When the waiter must wake next, or None to sleep until nudged."""
        return self.sequencer.next_deadline()

    # -- telemetry ---------------------------------------------------------------

    @property
    def status_apids(self) -> set[int]:
        """APIDs of the status packets this service is the single writer of."""
        return {p.apid for p in self._packets.values() if p is not None}

    def values_for(self, packet_def) -> dict:
        """Pack-ready values for one status packet ({} if not ours).

        Field names are mapped from the packet's own declaration: strip the
        kind prefix (``ATS_``/``RTS_``), lowercase, and look the key up in
        ``Sequencer.status()`` — so ATS_CMD_SKIPPED finds ``cmd_skipped``,
        and a vehicle that declares fewer fields simply maps fewer. Values
        are made pack-ready here (bytes for strings, wire ints for enums)
        because ``pack_telemetry`` maps no labels. The packet's timestamp
        field is stamped from the service clock.
        """
        kind = next(
            (k for k, p in self._packets.items() if p is not None and p.apid == packet_def.apid),
            None,
        )
        if kind is None:
            return {}
        status = self.sequencer.status(kind, self.clock())
        prefix = kind.upper() + "_"
        values: dict = {}
        for field in packet_def.fields:
            key = field.name.removeprefix(prefix).lower()
            if key == "timestamp":
                values[field.name] = self._saturate(field, int(self.clock()))
                continue
            if key not in status:
                continue
            value = status[key]
            if field.enumerations and isinstance(value, str):
                value = field.enumerations[value]
            elif isinstance(value, str):
                value = value.encode("utf-8")
            if isinstance(value, int):
                value = self._saturate(field, value)
            values[field.name] = value
        return values

    def _saturate(self, field, value: int) -> int:
        """Clamp an integer to the field's wire range so packing never
        raises — the status downlink must survive any legal plan. The real
        case: NEXT_CMD_TIME is a uint32 epoch, and an ATS entry beyond 2106
        is a valid schedule the field simply cannot express. Saturating
        (with a warning, once per field) beats losing the whole packet.
        """
        bits = field.size_bits or 8
        if field.python_type.startswith("u"):
            lo, hi = 0, (1 << bits) - 1
        else:
            lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        if lo <= value <= hi:
            return value
        if field.name not in self._saturation_warned:
            self._saturation_warned.add(field.name)
            self._logger.warning(
                "%s: value %d exceeds the field's wire range [%d, %d] — "
                "saturating (further occurrences suppressed)",
                field.name,
                value,
                lo,
                hi,
            )
        return min(max(value, lo), hi)
