"""
The onboard sequencer: one ATS slot and one RTS slot, ticked by the sim.

The sequencer owns the five-state machine the XTCE status packets declare
(IDLE, LOADED, RUNNING, COMPLETE, ERROR) and fires due commands through an
injected executor — the same dispatch path an uplinked command takes, so a
sequence-fired command is indistinguishable from a ground one downstream.
It never touches a clock or a disk: every method takes ``now`` from the
caller, and LOAD takes an already-parsed ``Sequence``. Both are deliberate.
The injected clock makes the whole machine testable against pretend time,
and taking parsed sequences (RTS entries hold DELAYS, not absolute times)
buries the prior implementation's worst habit — re-reading the file from
disk at START, so an edited or deleted file silently swapped the running
sequence's content.

Verb semantics, stated plainly (they match cFS Stored Command's, where STOP
kills execution rather than pausing it):

- LOAD installs a plan; a failed load (``load_failed``) lands the slot in
  ERROR, naming the plan that refused.
- START runs the loaded plan FROM THE TOP, whether it is freshly LOADED or
  already COMPLETE. There is no pause/resume: an entry is DUE when its
  deadline is at or before ``now``, and each tick fires every due entry,
  across both slots, in deadline order.
- Starting an ATS whose leading entries are already in the past SKIPS them
  (counted and logged) and starts at the first future entry. If the whole
  plan is past, START is refused — the plan needs a ground re-base
  (``seq shift``), and burst-firing stale commands at a vehicle is exactly
  the accident this rule exists to prevent.
- STOP halts execution and returns the slot to LOADED, exactly the state a
  fresh LOAD leaves: position, counters, and history all reset. The plan
  stays on board; START runs it again from the top.
- ABORT halts execution and clears the slot entirely (any state -> IDLE);
  re-running needs a new LOAD.
- A failed command (executor returns False, or raises) is recorded and
  the sequence CONTINUES — matching flight sequencers, where one failed
  command does not strand the rest of a timeline.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from xtce_sim.sequences import Sequence

logger = logging.getLogger(__name__)

#: Fired-command executor: (command_name, raw_args) -> success. Wired to the
#: sim's normal dispatch in the integration layer; async because dispatch is.
Executor = Callable[[str, dict], Awaitable[bool]]


class SeqState(enum.Enum):
    """Matches the XTCE SeqStateType enumeration exactly (label and value)."""

    IDLE = 0
    LOADED = 1
    RUNNING = 2
    COMPLETE = 3
    ERROR = 4


class CmdResult(enum.Enum):
    """Matches the XTCE CmdResultType enumeration exactly (label and value)."""

    SUCCESS = 0
    FAILED = 1
    PENDING = 2  # nothing has fired yet


@dataclass
class Fired:
    """One command the sequencer dispatched during a tick."""

    kind: str  # "ats" | "rts"
    command: str
    args: dict[str, str]
    success: bool


@dataclass
class _Slot:
    kind: str  # "ats" | "rts"
    sequence: Sequence | None = None
    state: SeqState = SeqState.IDLE
    position: int = 0  # index of the next entry to consider
    base: float = 0.0  # start epoch (RTS delays are relative to it)
    final_elapsed: float = 0.0  # sequence clock held at COMPLETE
    executed: int = 0
    skipped: int = 0
    last_name: str = ""
    last_result: CmdResult = CmdResult.PENDING
    error_name: str = ""  # the plan a failed LOAD refused (ERROR state)
    # Bumped whenever the current run's bookkeeping becomes stale (abort,
    # reload, stop). A fired command can legally stop, abort, or replace its
    # own slot through the executor; bookkeeping for an entry only lands if
    # the run that fired it is still the one installed.
    generation: int = 0

    def clear(self) -> None:
        self.sequence = None
        self.state = SeqState.IDLE
        self.error_name = ""
        self.reset_run()

    def reset_run(self) -> None:
        """Discard run progress, returning to the as-loaded state."""
        self.position = 0
        self.base = 0.0
        self.final_elapsed = 0.0
        self.executed = 0
        self.skipped = 0
        self.last_name = ""
        self.last_result = CmdResult.PENDING
        self.generation += 1

    @property
    def total(self) -> int:
        return len(self.sequence.entries) if self.sequence else 0

    @property
    def exhausted(self) -> bool:
        return self.sequence is not None and self.position >= self.total

    def deadline(self, index: int) -> float:
        """Epoch seconds at which entry ``index`` is due."""
        entry = self.sequence.entries[index]
        return entry.time if self.kind == "ats" else self.base + entry.time

    def elapsed(self, now: float) -> float:
        """Seconds of sequence time: live while RUNNING, held at its final
        value at COMPLETE (never negative — a non-monotonic caller clock
        must not push a negative into an unsigned field)."""
        if self.state is SeqState.RUNNING:
            return max(0.0, now - self.base)
        if self.state is SeqState.COMPLETE:
            return self.final_elapsed
        return 0.0


class Sequencer:
    """One ATS and one RTS slot, advanced by ``tick(now)``."""

    def __init__(self, execute: Executor, logger_: logging.Logger | None = None) -> None:
        self._execute = execute
        # Injected like the services': the integration hands over the
        # instance's own logger so slot events land in the sim window.
        self._log = logger_ or logger
        self._slots = {"ats": _Slot(kind="ats"), "rts": _Slot(kind="rts")}

    # -- commands ----------------------------------------------------------------

    def load(self, sequence: Sequence) -> tuple[bool, str]:
        """Install a parsed sequence into its slot (LOADED)."""
        slot = self._slots[sequence.kind]
        if slot.state is SeqState.RUNNING:
            return False, f"{slot.kind.upper()} is RUNNING — stop or abort it first"
        replaced = ""
        if slot.sequence is not None and slot.executed:
            # Loading over a part-run plan is legal but worth saying out loud.
            replaced = f", replacing {slot.sequence.name} ({slot.executed}/{slot.total} executed)"
        slot.clear()
        slot.sequence = sequence
        slot.state = SeqState.LOADED
        self._log.info("%s loaded: %s (%d commands)", slot.kind.upper(), sequence.name, slot.total)
        return True, f"loaded {sequence.name} ({slot.total} commands){replaced}"

    def load_failed(self, kind: str, name: str, reason: str) -> tuple[bool, str]:
        """A LOAD that could not produce a plan: the slot lands in ERROR.

        Refused while RUNNING for the same reason ``load`` is — a bad file
        must not tear down the plan currently executing. The failed plan's
        name stays visible in the status packet so the operator can see
        WHAT refused, not just that something did.
        """
        slot = self._slots[kind]
        if slot.state is SeqState.RUNNING:
            return False, f"{kind.upper()} is RUNNING — stop or abort it first"
        slot.clear()
        slot.state = SeqState.ERROR
        slot.error_name = name
        self._log.error("%s load failed: %s — %s", kind.upper(), name, reason)
        return True, f"load of {name} failed: {reason}"

    def start(self, kind: str, now: float) -> tuple[bool, str]:
        """Run the loaded plan from the top (LOADED or COMPLETE -> RUNNING)."""
        slot = self._slots[kind]
        if slot.state is SeqState.RUNNING:
            return False, f"{kind.upper()} is already RUNNING — stop or abort it first"
        if slot.state not in (SeqState.LOADED, SeqState.COMPLETE):
            return False, f"{kind.upper()} is {slot.state.name} — load a sequence first"
        skipped = 0
        if kind == "ats":
            # Decide the skip/refusal BEFORE touching the slot: a refused
            # START must leave the slot exactly as it was, including a
            # COMPLETE run's record. Entries are time-sorted, so the count
            # of strictly-past deadlines is also the first future index
            # (an entry due exactly at the start instant is kept and fires
            # on the next tick).
            skipped = sum(1 for e in slot.sequence.entries if e.time < now)
            if skipped == slot.total:
                return False, (
                    f"all {skipped} remaining command(s) are in the past — re-base "
                    "the plan with 'seq shift' and load it again"
                )
        slot.reset_run()
        slot.base = now
        slot.position = skipped
        slot.skipped = skipped
        if skipped:
            self._log.warning(
                "ATS %s: skipped %d past command(s), starting at entry %d",
                slot.sequence.name,
                skipped,
                slot.position + 1,
            )
        slot.state = SeqState.RUNNING
        self._log.info("%s started: %s", kind.upper(), slot.sequence.name)
        return True, f"started {slot.sequence.name}"

    def stop(self, kind: str) -> tuple[bool, str]:
        """Halt execution; the plan stays on board, reset to as-loaded."""
        slot = self._slots[kind]
        if slot.state is not SeqState.RUNNING:
            return False, f"{kind.upper()} is {slot.state.name}, not RUNNING"
        halted_at = f"{slot.position}/{slot.total}"
        slot.reset_run()
        slot.state = SeqState.LOADED
        self._log.info("%s stopped at %s; %s remains loaded", kind.upper(), halted_at, slot.sequence.name)
        return True, f"stopped at command {halted_at}; {slot.sequence.name} remains loaded"

    def abort(self, kind: str) -> tuple[bool, str]:
        """Discard the slot's sequence entirely (any state -> IDLE)."""
        slot = self._slots[kind]
        name = slot.sequence.name if slot.sequence else "(nothing loaded)"
        slot.clear()
        self._log.info("%s aborted: %s", kind.upper(), name)
        return True, f"aborted {name}"

    # -- time --------------------------------------------------------------------

    async def tick(self, now: float) -> list[Fired]:
        """Fire every due entry across both slots, in deadline order."""
        fired: list[Fired] = []
        while True:
            slot = self._next_due(now)
            if slot is None:
                break
            fired.append(await self._fire(slot))
        for slot in self._slots.values():
            if slot.state is SeqState.RUNNING and slot.exhausted:
                slot.state = SeqState.COMPLETE
                slot.final_elapsed = max(0.0, now - slot.base)
                self._log.info(
                    "%s complete: %s (%d executed, %d skipped)",
                    slot.kind.upper(),
                    slot.sequence.name,
                    slot.executed,
                    slot.skipped,
                )
        return fired

    def next_deadline(self) -> float | None:
        """The earliest due-time across both slots, or None if nothing is
        RUNNING — what the integration's waiter sleeps toward."""
        deadlines = [
            slot.deadline(slot.position)
            for slot in self._slots.values()
            if slot.state is SeqState.RUNNING and not slot.exhausted
        ]
        return min(deadlines) if deadlines else None

    def _next_due(self, now: float) -> _Slot | None:
        """The running slot whose next entry has the earliest due deadline."""
        best: _Slot | None = None
        for slot in self._slots.values():  # ats first: the deadline tiebreak
            if slot.state is not SeqState.RUNNING or slot.exhausted:
                continue
            deadline = slot.deadline(slot.position)
            if deadline > now:
                continue
            if best is None or deadline < best.deadline(best.position):
                best = slot
        return best

    async def _fire(self, slot: _Slot) -> Fired:
        entry = slot.sequence.entries[slot.position]
        # Claim the entry BEFORE dispatching: sequences legitimately carry
        # sequence-control commands (an ATS that starts an RTS, a re-arming
        # loop), so the executor may re-enter this sequencer — including a
        # reentrant tick(), which must not fire this same entry again.
        slot.position += 1
        generation = slot.generation
        total = slot.total
        args = dict(entry.args)  # the executor must not mutate the plan
        try:
            success = bool(await self._execute(entry.command, args))
        except Exception:
            self._log.exception("%s: %s raised", slot.kind.upper(), entry.command)
            success = False
        result = CmdResult.SUCCESS if success else CmdResult.FAILED
        if slot.generation == generation:
            slot.executed += 1
            slot.last_name = entry.command
            slot.last_result = result
            progress = slot.executed + slot.skipped
        else:
            # The fired command stopped, aborted, or replaced its own run:
            # the entry still ran, but its bookkeeping must not land on the
            # new run.
            progress = 0
        log = self._log.info if success else self._log.warning
        log(
            "%s fired %s (%d/%d) -> %s",
            slot.kind.upper(),
            entry.command,
            progress,
            total,
            result.name,
        )
        return Fired(kind=slot.kind, command=entry.command, args=args, success=success)

    # -- telemetry ---------------------------------------------------------------

    def status(self, kind: str, now: float) -> dict[str, object]:
        """Source values for the kind's status packet, enum values as labels
        (the storage layer maps labels through each field's own enumeration,
        exactly as the ADCS model's outputs do)."""
        slot = self._slots[kind]
        remaining = max(0, slot.total - slot.executed - slot.skipped)
        # An ATS deadline is meaningful the moment the plan loads; an RTS
        # deadline exists only while RUNNING (before START its base is not
        # set, and STOP discards the base with the rest of the run).
        next_time = 0
        has_next = slot.sequence is not None and not slot.exhausted
        if has_next and (kind == "ats" or slot.state is SeqState.RUNNING):
            next_time = int(slot.deadline(slot.position))
        # ERROR holds no usable plan, so no sequence is "active" — but the
        # name of the plan that refused stays visible.
        name = slot.sequence.name if slot.sequence else slot.error_name
        return {
            "seq_id": 0 if slot.state in (SeqState.IDLE, SeqState.ERROR) else 1,
            "seq_name": name,
            "state": slot.state.name,
            "cmd_total": slot.total,
            "cmd_executed": slot.executed,
            "cmd_skipped": slot.skipped,
            "cmd_remaining": remaining,
            "next_cmd_time": next_time,
            "elapsed_sec": int(slot.elapsed(now)),
            "last_cmd_name": slot.last_name,
            "last_cmd_result": slot.last_result.name,
        }
