"""
The onboard sequencer: one ATS slot and one RTS slot, ticked by the sim.

The sequencer owns the six-state machine the XTCE status packets declare
(IDLE, LOADED, RUNNING, STOPPED, COMPLETE, ERROR) and fires due commands
through an injected executor — the same dispatch path an uplinked command
takes, so a sequence-fired command is indistinguishable from a ground one
downstream. It never touches a clock or a disk: every method takes ``now``
from the caller, and LOAD takes an already-parsed ``Sequence``. Both are
deliberate. The injected clock makes the whole machine testable against
pretend time, and taking parsed sequences (RTS entries hold DELAYS, not
absolute times) buries the prior implementation's worst habit — re-reading
the file from disk at START, so an edited or deleted file silently swapped
the running sequence's content.

Time semantics, stated plainly:

- An entry is DUE when its deadline is at or before ``now``; each tick
  fires every due entry, across both slots, in deadline order.
- Starting an ATS whose leading entries are already in the past SKIPS
  them (counted and logged) and starts at the first future entry. If the
  whole plan is past, START is refused — the plan needs a ground re-base
  (``seq shift``), and burst-firing stale commands at a vehicle is exactly
  the accident this rule exists to prevent.
- Stopping an RTS freezes its clock: elapsed time holds, and on resume
  the remaining delays continue as if the pause never happened.
- Stopping an ATS cannot stop UTC. Entries whose times pass during the
  pause are skipped at resume, by the same rule as a late start.
- A failed command (executor returns False, or raises) is recorded and
  the sequence CONTINUES — matching flight sequencers, where one failed
  command does not strand the rest of a timeline.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Callable

from xtce_sim.sequences import Sequence

logger = logging.getLogger(__name__)

#: Fired-command executor: (command_name, raw_args) -> success. Wired to the
#: sim's normal dispatch in the integration layer.
Executor = Callable[[str, dict], bool]


class SeqState(enum.Enum):
    """Matches the XTCE SeqStateType enumeration exactly (label and value)."""

    IDLE = 0
    LOADED = 1
    RUNNING = 2
    STOPPED = 3
    COMPLETE = 4
    ERROR = 5


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
    base: float = 0.0  # RTS: pause-adjusted start epoch; ATS: start epoch
    frozen_elapsed: float = 0.0  # sequence clock held by STOP or COMPLETE
    executed: int = 0
    skipped: int = 0
    last_name: str = ""
    last_result: CmdResult = CmdResult.PENDING
    # Bumped whenever the loaded plan is discarded (abort, reload). A fired
    # command can legally abort or replace its own slot through the
    # executor; bookkeeping for an entry only lands if the plan that fired
    # it is still the one installed.
    generation: int = 0

    def clear(self) -> None:
        self.sequence = None
        self.state = SeqState.IDLE
        self.position = 0
        self.base = 0.0
        self.frozen_elapsed = 0.0
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
        """Seconds of sequence time: live while RUNNING, held at its last
        value once STOPPED or COMPLETE (never negative — a non-monotonic
        caller clock must not push a negative into an unsigned field)."""
        if self.state is SeqState.RUNNING:
            return max(0.0, now - self.base)
        if self.state in (SeqState.STOPPED, SeqState.COMPLETE):
            return self.frozen_elapsed
        return 0.0


class Sequencer:
    """One ATS and one RTS slot, advanced by ``tick(now)``."""

    def __init__(self, execute: Executor) -> None:
        self._execute = execute
        self._slots = {"ats": _Slot(kind="ats"), "rts": _Slot(kind="rts")}

    # -- commands ----------------------------------------------------------------

    def load(self, sequence: Sequence) -> tuple[bool, str]:
        """Install a parsed sequence into its slot (LOADED)."""
        slot = self._slots[sequence.kind]
        if slot.state is SeqState.RUNNING:
            return False, f"{slot.kind.upper()} is RUNNING — stop or abort it first"
        replaced = ""
        if slot.sequence is not None and slot.executed:
            # Loading over a half-run plan is legal but worth saying out loud.
            replaced = f", replacing {slot.sequence.name} ({slot.executed}/{slot.total} executed)"
        slot.clear()
        slot.sequence = sequence
        slot.state = SeqState.LOADED
        logger.info("%s loaded: %s (%d commands)", slot.kind.upper(), sequence.name, slot.total)
        return True, f"loaded {sequence.name} ({slot.total} commands){replaced}"

    def start(self, kind: str, now: float) -> tuple[bool, str]:
        slot = self._slots[kind]
        if slot.state is SeqState.RUNNING:
            return False, f"{kind.upper()} is already RUNNING — stop or abort it first"
        if slot.state not in (SeqState.LOADED, SeqState.STOPPED):
            return False, f"{kind.upper()} is {slot.state.name} — load a sequence first"
        resuming = slot.state is SeqState.STOPPED
        if kind == "rts":
            # Resume re-bases in memory: the pause simply never happened.
            slot.base = now - slot.frozen_elapsed if resuming else now
        else:
            slot.base = slot.base if resuming else now
            skipped, refusal = self._skip_past(slot, now)
            if refusal is not None:
                return False, refusal
            if skipped:
                logger.warning(
                    "ATS %s: skipped %d past command(s), starting at entry %d",
                    slot.sequence.name,
                    skipped,
                    slot.position + 1,
                )
        slot.state = SeqState.RUNNING
        verb = "resumed" if resuming else "started"
        logger.info("%s %s: %s", kind.upper(), verb, slot.sequence.name)
        return True, f"{verb} {slot.sequence.name}"

    def stop(self, kind: str, now: float) -> tuple[bool, str]:
        slot = self._slots[kind]
        if slot.state is not SeqState.RUNNING:
            return False, f"{kind.upper()} is {slot.state.name}, not RUNNING"
        slot.frozen_elapsed = max(0.0, now - slot.base)
        slot.state = SeqState.STOPPED
        logger.info("%s stopped at %d/%d", kind.upper(), slot.position, slot.total)
        return True, f"stopped at command {slot.position}/{slot.total}"

    def abort(self, kind: str) -> tuple[bool, str]:
        """Discard the slot's sequence entirely (any state -> IDLE)."""
        slot = self._slots[kind]
        name = slot.sequence.name if slot.sequence else "(nothing loaded)"
        slot.clear()
        logger.info("%s aborted: %s", kind.upper(), name)
        return True, f"aborted {name}"

    def _skip_past(self, slot: _Slot, now: float) -> tuple[int, str | None]:
        """Advance past entries already behind ``now``; refuse an all-past plan.

        The skip rule is strict (deadline < now): an entry due exactly at
        the start instant still fires on the next tick.
        """
        skipped = 0
        while not slot.exhausted and slot.deadline(slot.position) < now:
            slot.position += 1
            skipped += 1
        if slot.exhausted:
            slot.position -= skipped  # leave the slot as it was
            return 0, (
                f"all {skipped} remaining command(s) are in the past — re-base "
                "the plan with 'seq shift' and load it again"
            )
        slot.skipped += skipped
        return skipped, None

    # -- time --------------------------------------------------------------------

    def tick(self, now: float) -> list[Fired]:
        """Fire every due entry across both slots, in deadline order."""
        fired: list[Fired] = []
        while True:
            slot = self._next_due(now)
            if slot is None:
                break
            fired.append(self._fire(slot))
        for slot in self._slots.values():
            if slot.state is SeqState.RUNNING and slot.exhausted:
                slot.state = SeqState.COMPLETE
                slot.frozen_elapsed = max(0.0, now - slot.base)
                logger.info(
                    "%s complete: %s (%d executed, %d skipped)",
                    slot.kind.upper(),
                    slot.sequence.name,
                    slot.executed,
                    slot.skipped,
                )
        return fired

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

    def _fire(self, slot: _Slot) -> Fired:
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
            success = bool(self._execute(entry.command, args))
        except Exception:
            logger.exception("%s: %s raised", slot.kind.upper(), entry.command)
            success = False
        result = CmdResult.SUCCESS if success else CmdResult.FAILED
        if slot.generation == generation:
            slot.executed += 1
            slot.last_name = entry.command
            slot.last_result = result
            progress = slot.executed + slot.skipped
        else:
            # The fired command aborted or replaced its own plan: the entry
            # still ran, but its bookkeeping must not land on the new plan.
            progress = 0
        log = logger.info if success else logger.warning
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
        # set, and while STOPPED the base shifts again at resume).
        next_time = 0
        has_next = slot.sequence is not None and not slot.exhausted
        if has_next and (kind == "ats" or slot.state is SeqState.RUNNING):
            next_time = int(slot.deadline(slot.position))
        return {
            "seq_id": 0 if slot.state is SeqState.IDLE else 1,
            "seq_name": slot.sequence.name if slot.sequence else "",
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
