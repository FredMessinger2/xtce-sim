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
status fields, a subset enumeration) is a valid configuration, not an
error. Fields and labels that map to nothing are reported at load/emit
time so a typo in an ICD reads as a warning, not as a mystery zero.

A refused command — a SeqId other than 1 (one ATS slot and one RTS slot is
this simulator's contract, whatever ranges a vehicle's ICD declares), START
with nothing loaded, STOP of an idle slot, LOAD of a missing or unparseable
file — raises ``SequenceCommandError``, which dispatch's guard turns into a
FAILED command echo. A failed LOAD additionally lands the slot in ERROR,
naming the plan that refused.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Optional

from xtce_sim import sequences
from xtce_sim.fileservice import FileStore, decode_filename_arg
from xtce_sim.sequencer import CmdResult, Fired, SeqState, Sequencer
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

#: Every label the sequencer can ever emit, per status key — what a status
#: packet's enumerations must cover to tell the whole story on the wire.
_LABEL_SETS = {
    "state": {s.name for s in SeqState},
    "last_cmd_result": {r.name for r in CmdResult},
}

#: Executor type re-exported for the server: fired commands travel it.
Executor = Callable[[str, dict], Awaitable[bool]]


class SequenceCommandError(Exception):
    """A sequence command the vehicle refused; dispatch echoes FAILED."""


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
        self._warned: set[tuple[str, str]] = set()
        self._execute: Optional[Executor] = None
        self.sequencer = Sequencer(self._run_fired, logger_=self._logger)
        self._packets = {
            kind: simdef.packet_by_name(name)
            for kind, name in _STATUS_PACKET_NAMES.items()
        }
        self._kind_by_apid = {
            p.apid: kind for kind, p in self._packets.items() if p is not None
        }
        #: The status packets this service is the single writer of.
        self.status_packets = tuple(p for p in self._packets.values() if p is not None)
        self._report_declaration()

    def _report_declaration(self) -> None:
        """Say up front what the ICD's status packets do and don't map.

        A field whose name matches no sequencer status key would otherwise
        downlink a plausible constant zero forever — indistinguishable from
        a sequence that never ran — so the mismatch is reported here, once,
        where an ICD author will see it.
        """
        for kind, name in _STATUS_PACKET_NAMES.items():
            packet = self._packets[kind]
            if packet is None:
                self._logger.info(
                    "definition declares no %s packet; %s lifecycle will be log-only",
                    name,
                    kind.upper(),
                )
                continue
            known = set(self.sequencer.status(kind, 0.0)) | {"timestamp"}
            prefix = kind.upper() + "_"
            unmapped = [
                f.name
                for f in packet.fields
                if f.name.removeprefix(prefix).lower() not in known
            ]
            if unmapped:
                self._logger.warning(
                    "%s: field(s) %s match no sequencer status key — they will "
                    "pack as defaults",
                    name,
                    ", ".join(unmapped),
                )
            for field in packet.fields:
                needed = _LABEL_SETS.get(field.name.removeprefix(prefix).lower())
                if needed is None or not field.enumerations:
                    continue
                missing = sorted(needed - set(field.enumerations))
                if missing:
                    # A state the wire cannot express downlinks as the field
                    # default (0) — say so HERE, where the ICD author looks,
                    # not in a runtime log after the first failed LOAD.
                    self._logger.warning(
                        "%s.%s: enumeration is missing label(s) %s — those "
                        "states cannot be downlinked and will read as the "
                        "default value",
                        name,
                        field.name,
                        ", ".join(missing),
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

        Raises ``SequenceCommandError`` on refusal. SeqId is enforced here
        as well as by the example ICD's ValidRange: the single-slot rule is
        this simulator's contract, and a vehicle whose XTCE forgot the
        range must still refuse SeqId 3 rather than silently act on slot 1.
        """
        seq_id = self._seq_id_wire_value(command_name, args)
        if seq_id not in (None, 1):
            raise SequenceCommandError(
                f"SeqId {args.get('SeqId')} refused — this vehicle has a single "
                "ATS slot and a single RTS slot, both SeqId 1"
            )
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

    def _seq_id_wire_value(self, command_name: str, args: dict):
        """The SeqId argument as its wire value (None if absent).

        ``decode_command`` hands enum arguments over as their LABELS — a
        vehicle is free to type SeqId as an enumeration (SLOT_1 = 1), and
        the single-slot rule judges the wire value, not the spelling.
        """
        seq_id = args.get("SeqId")
        if not isinstance(seq_id, str):
            return seq_id
        command = self.simdef.command_by_name(command_name)
        param = (
            next((p for p in command.params if p.name == "SeqId"), None)
            if command is not None
            else None
        )
        if param is not None and param.enumerations:
            return param.enumerations.get(seq_id, seq_id)
        return seq_id

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
        ext_kind = sequences.kind_for(name)
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
        return set(self._kind_by_apid)

    def values_for(self, packet_def) -> dict:
        """Pack-ready values for one status packet ({} if not ours).

        Field names are mapped from the packet's own declaration: strip the
        kind prefix (``ATS_``/``RTS_``), lowercase, and look the key up in
        ``Sequencer.status()`` — so ATS_CMD_SKIPPED finds ``cmd_skipped``,
        and a vehicle that declares fewer fields simply maps fewer. Values
        are made pack-ready here (bytes for strings, wire ints for enums)
        because ``pack_telemetry`` maps no labels; an enum label the field
        does not declare skips the field with a warning rather than raising
        — the beacon and the waiter must both survive a lean enumeration.
        The packet's timestamp field is stamped from the service clock.
        """
        kind = self._kind_by_apid.get(packet_def.apid)
        if kind is None:
            return {}
        status = self.sequencer.status(kind, self.clock())
        prefix = kind.upper() + "_"
        values: dict = {}
        for field in packet_def.fields:
            key = field.name.removeprefix(prefix).lower()
            if key == "timestamp":
                values[field.name] = int(self.clock())
                continue
            if key not in status:
                continue
            value = status[key]
            if field.enumerations and isinstance(value, str):
                wire = field.enumerations.get(value)
                if wire is None:
                    self._warn_once(
                        field.name,
                        value,
                        f"{field.name}: enumeration declares no label {value!r} — "
                        "skipping the field this pass",
                    )
                    continue
                value = wire
            elif isinstance(value, str):
                value = value.encode("utf-8")
            values[field.name] = value
        return values

    def _warn_once(self, field_name: str, detail: str, message: str) -> None:
        key = (field_name, detail)
        if key in self._warned:
            return
        self._warned.add(key)
        self._logger.warning("%s (further occurrences suppressed)", message)
