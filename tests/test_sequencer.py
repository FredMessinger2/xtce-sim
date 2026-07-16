"""The onboard sequencer: state machine, injected time, and firing semantics."""

from pathlib import Path

import pytest

from xtce_sim.definition import SimDefinition
from xtce_sim.sequencer import CmdResult, SeqState, Sequencer
from xtce_sim.sequences import parse_ats, parse_rts

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
IMAGING = EXAMPLES / "imaging_sat/imaging_sat.xml"

# All tests run against pretend time anchored here (2026-03-15T14:30:00Z).
T0 = 1773585000.0


def _ats(offsets=(0, 60, 120), command="IMAGER_ON"):
    from xtce_sim.sequences import format_utc

    lines = "".join(f"{format_utc(T0 + off)} {command}\n" for off in offsets)
    return parse_ats(lines, "plan.ats")


def _rts(delays=(0, 5, 30), command="HEATER_OFF"):
    lines = "".join(f"+{d} {command} HeaterId=1\n" for d in delays)
    return parse_rts(lines, "safe.rts")


class Recorder:
    """Executor stub: records calls, returns a scripted success value."""

    def __init__(self):
        self.calls = []
        self.result = True
        self.raise_on = None

    async def __call__(self, name, args):
        if self.raise_on == len(self.calls):
            self.calls.append((name, args))
            raise RuntimeError("executor blew up")
        self.calls.append((name, args))
        return self.result


def _executor(fn):
    """Wrap a sync callable as the async executor the Sequencer expects."""

    async def run(name, args):
        return fn(name, args)

    return run


@pytest.fixture()
def rec():
    return Recorder()


@pytest.fixture()
def seq(rec):
    return Sequencer(rec)


# ---------------------------------------------------------------------------
# Contract: our enums ARE the XTCE's


def test_state_and_result_enums_match_the_xtce_contract():
    simdef = SimDefinition.from_xtce(IMAGING)
    status = simdef.packet_by_name("ATS_STATUS")
    state_field = next(f for f in status.fields if f.name == "ATS_STATE")
    result_field = next(f for f in status.fields if f.name == "ATS_LAST_CMD_RESULT")
    assert state_field.enumerations == {s.name: s.value for s in SeqState}
    assert result_field.enumerations == {r.name: r.value for r in CmdResult}


# ---------------------------------------------------------------------------
# ATS firing


async def test_ats_fires_each_entry_when_due(seq, rec):
    assert seq.load(_ats())[0]
    assert seq.start("ats", T0 - 10)[0]
    assert await seq.tick(T0 - 1) == []  # nothing due yet
    fired = await seq.tick(T0)  # entry at exactly T0 is due at T0
    assert [f.command for f in fired] == ["IMAGER_ON"]
    assert await seq.tick(T0 + 59) == []
    assert len(await seq.tick(T0 + 60)) == 1
    assert len(await seq.tick(T0 + 1000)) == 1  # the last one, late but only once
    assert seq.status("ats", T0 + 1000)["state"] == "COMPLETE"
    assert await seq.tick(T0 + 2000) == []  # complete sequences stay quiet


async def test_multiple_due_entries_fire_in_order_in_one_tick(seq, rec):
    seq.load(_ats(offsets=(0, 1, 2)))
    seq.start("ats", T0 - 10)
    fired = await seq.tick(T0 + 100)  # all three overdue at once
    assert len(fired) == 3
    assert [c for c, _ in rec.calls] == ["IMAGER_ON"] * 3
    assert seq.status("ats", T0 + 100)["state"] == "COMPLETE"


async def test_ats_and_rts_interleave_by_deadline(seq, rec):
    seq.load(_ats(offsets=(10, 20), command="IMAGER_ON"))
    seq.load(_rts(delays=(5, 40)))
    seq.start("ats", T0)
    seq.start("rts", T0)  # deadlines land at +5 rts, +10 ats, +20 ats, +40 rts
    fired = await seq.tick(T0 + 60)
    assert [(f.kind, f.command) for f in fired] == [
        ("rts", "HEATER_OFF"),
        ("ats", "IMAGER_ON"),
        ("ats", "IMAGER_ON"),
        ("rts", "HEATER_OFF"),
    ]


async def test_late_ats_start_skips_past_entries(seq, rec):
    seq.load(_ats(offsets=(0, 60, 120)))
    ok, msg = seq.start("ats", T0 + 61)  # first two already past
    assert ok and "skipped" not in msg  # skips are logged, not chatty
    fired = await seq.tick(T0 + 120)
    assert len(fired) == 1  # ONLY the future entry; no burst of stale commands
    status = seq.status("ats", T0 + 120)
    assert status["cmd_skipped"] == 2
    assert status["cmd_executed"] == 1
    assert status["cmd_remaining"] == 0
    assert status["state"] == "COMPLETE"


async def test_all_past_ats_start_is_refused(seq, rec):
    seq.load(_ats(offsets=(0, 60)))
    ok, msg = seq.start("ats", T0 + 1000)
    assert not ok
    assert "seq shift" in msg  # points the operator at the ground fix
    status = seq.status("ats", T0 + 1000)
    assert status["state"] == "LOADED"  # the plan is intact, just stale
    assert status["cmd_skipped"] == 0
    assert await seq.tick(T0 + 2000) == []


# ---------------------------------------------------------------------------
# STOP: halt, plan stays loaded, run it again from the top


async def test_stop_returns_the_slot_to_as_loaded(seq, rec):
    seq.load(_rts(delays=(0, 10, 20)))
    seq.start("rts", T0)
    await seq.tick(T0)  # +0 fires
    ok, msg = seq.stop("rts")
    assert ok and "remains loaded" in msg
    status = seq.status("rts", T0 + 5)
    assert status["state"] == "LOADED"
    assert status["seq_name"] == "safe.rts"  # the plan is still on board
    assert status["cmd_executed"] == 0  # exactly the state a fresh LOAD leaves
    assert status["cmd_remaining"] == 3
    assert status["elapsed_sec"] == 0
    assert status["last_cmd_result"] == "PENDING"
    assert await seq.tick(T0 + 300) == []  # stopped: nothing fires


async def test_start_after_stop_runs_from_the_top(seq, rec):
    seq.load(_rts(delays=(0, 10)))
    seq.start("rts", T0)
    await seq.tick(T0)  # +0 fires
    seq.stop("rts")
    seq.start("rts", T0 + 300)  # a fresh run, re-based at the new START
    assert len(await seq.tick(T0 + 300)) == 1  # +0 fires again, from the top
    assert await seq.tick(T0 + 305) == []
    assert len(await seq.tick(T0 + 310)) == 1  # +10, ten seconds after START
    assert seq.status("rts", T0 + 310)["state"] == "COMPLETE"


async def test_stopped_ats_restarts_with_the_skip_rule(seq, rec):
    seq.load(_ats(offsets=(0, 60, 120)))
    seq.start("ats", T0 - 1)
    await seq.tick(T0)  # first fires
    assert seq.stop("ats")[0]
    assert await seq.tick(T0 + 60) == []  # stopped: nothing fires
    ok, msg = seq.start("ats", T0 + 90)  # restart after the +60 entry's time
    assert ok and "started" in msg
    fired = await seq.tick(T0 + 120)
    assert len(fired) == 1  # +120 fires; +0 and +60 were skipped, not burst
    status = seq.status("ats", T0 + 120)
    assert status["cmd_skipped"] == 2
    assert status["cmd_executed"] == 1


async def test_refused_start_leaves_a_complete_record_untouched(seq, rec):
    # A completed ATS's entries are all in the past, so any later START_ATS
    # is refused — and the refusal must not erase the completion record the
    # telemetry is holding (executed counts, last command, elapsed).
    seq.load(_ats(offsets=(0, 60)))
    seq.start("ats", T0 - 1)
    await seq.tick(T0 + 60)
    before = seq.status("ats", T0 + 1000)
    assert before["state"] == "COMPLETE" and before["cmd_executed"] == 2
    ok, msg = seq.start("ats", T0 + 1000)
    assert not ok and "seq shift" in msg
    assert seq.status("ats", T0 + 1000) == before  # byte-for-byte untouched


async def test_start_from_complete_reruns_the_plan(seq, rec):
    seq.load(_rts(delays=(0, 5)))
    seq.start("rts", T0)
    await seq.tick(T0 + 5)
    assert seq.status("rts", T0 + 5)["state"] == "COMPLETE"
    ok, msg = seq.start("rts", T0 + 100)  # no reload needed: START = from the top
    assert ok
    assert len(await seq.tick(T0 + 100)) == 1
    assert seq.status("rts", T0 + 100)["cmd_executed"] == 1


# ---------------------------------------------------------------------------
# RTS timing


async def test_rts_delays_are_based_at_start_not_load(seq, rec):
    seq.load(_rts(delays=(0, 5)))
    await seq.tick(T0 + 1000)  # loaded but not started: nothing fires
    assert rec.calls == []
    seq.start("rts", T0 + 2000)
    assert len(await seq.tick(T0 + 2000)) == 1  # +0 due at start instant
    assert await seq.tick(T0 + 2004) == []
    assert len(await seq.tick(T0 + 2005)) == 1


# ---------------------------------------------------------------------------
# ERROR: the state a failed LOAD reaches


async def test_load_failed_lands_the_slot_in_error(seq, rec):
    ok, msg = seq.load_failed("ats", "broken.ats", "line 3: unknown command 'TYPO'")
    assert ok and "broken.ats" in msg
    status = seq.status("ats", T0)
    assert status["state"] == "ERROR"
    assert status["seq_name"] == "broken.ats"  # WHAT failed stays visible
    assert status["seq_id"] == 0  # no active sequence
    assert status["cmd_total"] == 0
    assert await seq.tick(T0 + 100) == []  # ERROR fires nothing


async def test_load_failed_replaces_a_loaded_plan_but_not_a_running_one(seq, rec):
    seq.load(_ats())
    assert seq.load_failed("ats", "bad.ats", "unreadable")[0]
    assert seq.status("ats", T0)["state"] == "ERROR"
    # But a RUNNING plan survives a bad load attempt untouched.
    seq.load(_ats())
    seq.start("ats", T0 - 1)
    ok, msg = seq.load_failed("ats", "bad.ats", "unreadable")
    assert not ok and "RUNNING" in msg
    assert seq.status("ats", T0)["state"] == "RUNNING"


async def test_error_recovers_via_load_or_abort(seq, rec):
    seq.load_failed("rts", "bad.rts", "unreadable")
    assert not seq.start("rts", T0)[0]  # nothing usable to start
    assert seq.load(_rts())[0]  # a good LOAD clears the error
    assert seq.status("rts", T0)["state"] == "LOADED"
    seq.load_failed("rts", "bad.rts", "unreadable")
    seq.abort("rts")  # so does ABORT
    status = seq.status("rts", T0)
    assert status["state"] == "IDLE"
    assert status["seq_name"] == ""


# ---------------------------------------------------------------------------
# Failures


async def test_executor_false_records_failed_and_continues(seq, rec):
    rec.result = False
    seq.load(_ats(offsets=(0, 1)))
    seq.start("ats", T0 - 1)
    fired = await seq.tick(T0 + 10)
    assert [f.success for f in fired] == [False, False]
    status = seq.status("ats", T0 + 10)
    assert status["last_cmd_result"] == "FAILED"
    assert status["state"] == "COMPLETE"  # one failure does not strand the rest


async def test_executor_exception_records_failed_and_continues(seq, rec):
    rec.raise_on = 0
    seq.load(_ats(offsets=(0, 1)))
    seq.start("ats", T0 - 1)
    fired = await seq.tick(T0 + 10)
    assert [f.success for f in fired] == [False, True]
    assert seq.status("ats", T0 + 10)["cmd_executed"] == 2


async def test_executor_cannot_mutate_the_plan(seq):
    polluter = Sequencer(_executor(lambda name, args: args.update(HeaterId="99") or True))
    polluter.load(_rts(delays=(0,)))
    polluter.start("rts", T0)
    await polluter.tick(T0)
    polluter.stop("rts")
    # Re-running must present the original arguments.
    seen = []
    replay = Sequencer(_executor(lambda name, args: seen.append(dict(args)) or True))
    replay.load(_rts(delays=(0,)))
    replay.start("rts", T0)
    await replay.tick(T0)
    assert seen == [{"HeaterId": "1"}]


# ---------------------------------------------------------------------------
# Transitions


async def test_illegal_transitions_are_refused(seq, rec):
    ok, msg = seq.start("ats", T0)
    assert not ok and "IDLE" in msg
    ok, msg = seq.stop("ats")
    assert not ok and "not RUNNING" in msg
    seq.load(_ats())
    seq.start("ats", T0 - 1)
    ok, msg = seq.load(_ats())  # load over a running sequence
    assert not ok and "RUNNING" in msg
    ok, msg = seq.start("ats", T0)  # start a running sequence
    assert not ok


async def test_abort_discards_from_any_state(seq, rec):
    assert seq.abort("ats")[0]  # aborting nothing is not an error
    seq.load(_ats())
    seq.start("ats", T0 - 1)
    await seq.tick(T0)
    assert seq.abort("ats")[0]
    status = seq.status("ats", T0)
    assert status["state"] == "IDLE"
    assert status["seq_name"] == ""
    assert status["cmd_total"] == 0
    assert status["last_cmd_result"] == "PENDING"
    assert await seq.tick(T0 + 1000) == []


async def test_slots_are_independent(seq, rec):
    seq.load(_ats())
    seq.load(_rts())
    seq.start("ats", T0 - 1)
    assert seq.status("ats", T0)["state"] == "RUNNING"
    assert seq.status("rts", T0)["state"] == "LOADED"
    seq.abort("rts")
    assert seq.status("ats", T0)["state"] == "RUNNING"


# ---------------------------------------------------------------------------
# Status contents


async def test_status_reflects_the_full_lifecycle(seq, rec):
    idle = seq.status("ats", T0)
    assert idle["seq_id"] == 0 and idle["state"] == "IDLE"
    seq.load(_ats(offsets=(0, 60)))
    loaded = seq.status("ats", T0 - 100)
    assert loaded["seq_id"] == 1
    assert loaded["seq_name"] == "plan.ats"
    assert loaded["cmd_total"] == 2
    assert loaded["next_cmd_time"] == int(T0)  # visible before START
    seq.start("ats", T0 - 100)
    await seq.tick(T0)
    running = seq.status("ats", T0)
    assert running["cmd_executed"] == 1
    assert running["cmd_remaining"] == 1
    assert running["next_cmd_time"] == int(T0 + 60)
    assert running["last_cmd_name"] == "IMAGER_ON"
    assert running["last_cmd_result"] == "SUCCESS"
    await seq.tick(T0 + 60)
    done = seq.status("ats", T0 + 60)
    assert done["state"] == "COMPLETE"
    assert done["next_cmd_time"] == 0
    assert done["cmd_remaining"] == 0


# ---------------------------------------------------------------------------
# Reentrancy: sequences legitimately carry sequence-control commands


async def test_fired_command_may_restart_its_own_slot_without_corruption():
    # The re-arming RTS pattern: the plan's last command aborts, reloads,
    # and restarts its own slot. The new run must begin at entry 0 with
    # clean counters — not inherit the old run's position.
    seqr = None
    launches = []

    async def executor(name, args):
        if name == "REARM":
            seqr.abort("rts")
            seqr.load(parse_rts("+50 PAYLOAD_GO\n+60 REARM\n", "loop.rts"))
            seqr.start("rts", T0 + 100)
        launches.append(name)
        return True

    seqr = Sequencer(executor)
    seqr.load(parse_rts("+0 PAYLOAD_GO\n+100 REARM\n", "loop.rts"))
    seqr.start("rts", T0)
    await seqr.tick(T0)
    fired = await seqr.tick(T0 + 100)  # REARM fires and re-arms the slot
    assert [f.command for f in fired] == ["REARM"]  # no burst, no double-fire
    status = seqr.status("rts", T0 + 100)
    assert status["state"] == "RUNNING"
    assert status["cmd_executed"] == 0  # the NEW run has fired nothing yet
    assert status["cmd_total"] == 2
    assert len(await seqr.tick(T0 + 150)) == 1  # new run's entry 0 fires on time
    assert launches == ["PAYLOAD_GO", "REARM", "PAYLOAD_GO"]


async def test_reentrant_tick_does_not_double_fire():
    seqr = None
    fired_names = []

    async def executor(name, args):
        fired_names.append(name)
        await seqr.tick(T0 + 10)  # unit-4 dispatch may pump the loop mid-command
        return True

    seqr = Sequencer(executor)
    seqr.load(parse_ats("2026-03-15T14:30:00Z ONLY_ONCE\n", "one.ats"))
    seqr.start("ats", T0 - 1)
    await seqr.tick(T0 + 10)
    assert fired_names == ["ONLY_ONCE"]
    assert seqr.status("ats", T0 + 10)["cmd_executed"] == 1


async def test_fired_command_aborting_its_own_slot_leaves_it_clean():
    seqr = None

    async def executor(name, args):
        seqr.abort("ats")
        return True

    seqr = Sequencer(executor)
    seqr.load(_ats(offsets=(0, 60)))
    seqr.start("ats", T0 - 1)
    await seqr.tick(T0)
    status = seqr.status("ats", T0)
    assert status["state"] == "IDLE"
    assert status["cmd_executed"] == 0  # no execution history on an empty slot
    assert status["last_cmd_result"] == "PENDING"
    assert await seqr.tick(T0 + 100) == []


async def test_fired_command_stopping_its_own_slot_leaves_it_as_loaded():
    # STOP mid-fire: the entry that issued the STOP already ran, but its
    # bookkeeping must not dirty the freshly-reset slot.
    seqr = None

    async def executor(name, args):
        seqr.stop("ats")
        return True

    seqr = Sequencer(executor)
    seqr.load(_ats(offsets=(0, 60)))
    seqr.start("ats", T0 - 1)
    await seqr.tick(T0)
    status = seqr.status("ats", T0)
    assert status["state"] == "LOADED"
    assert status["cmd_executed"] == 0  # exactly as a fresh LOAD leaves it
    assert status["last_cmd_result"] == "PENDING"
    assert await seqr.tick(T0 + 100) == []


async def test_ats_command_may_start_the_rts_in_the_same_tick():
    seqr = None

    async def executor(name, args):
        if name == "IMAGER_ON":
            seqr.load(parse_rts("+0 HEATER_OFF HeaterId=1\n", "safe.rts"))
            seqr.start("rts", T0)
        return True

    seqr = Sequencer(executor)
    seqr.load(_ats(offsets=(0,)))
    seqr.start("ats", T0 - 1)
    fired = await seqr.tick(T0)
    assert [(f.kind, f.command) for f in fired] == [
        ("ats", "IMAGER_ON"),
        ("rts", "HEATER_OFF"),  # joined this same tick's deadline merge
    ]


# ---------------------------------------------------------------------------
# Pins the first review round proved missing (mutation survivors)


async def test_ats_started_exactly_at_an_entry_time_keeps_it():
    # The skip rule is STRICT past (deadline < now): an entry due exactly
    # at the start instant is kept and fires on the next tick.
    rec = Recorder()
    seqr = Sequencer(rec)
    seqr.load(_ats(offsets=(0, 60)))
    seqr.start("ats", T0)  # exactly the first entry's time
    assert seqr.status("ats", T0)["cmd_skipped"] == 0
    assert len(await seqr.tick(T0)) == 1


async def test_identical_deadlines_fire_ats_before_rts():
    order = []
    seqr = Sequencer(_executor(lambda name, args: order.append(name) or True))
    seqr.load(_ats(offsets=(10,), command="FROM_ATS"))
    seqr.load(parse_rts("+10 FROM_RTS\n", "r.rts"))
    seqr.start("ats", T0)
    seqr.start("rts", T0)  # both deadlines land at exactly T0+10
    await seqr.tick(T0 + 10)
    assert order == ["FROM_ATS", "FROM_RTS"]


async def test_refused_start_leaves_next_cmd_time_untouched():
    rec = Recorder()
    seqr = Sequencer(rec)
    seqr.load(_ats(offsets=(0, 60)))
    before = seqr.status("ats", T0 + 1000)["next_cmd_time"]
    assert not seqr.start("ats", T0 + 1000)[0]
    assert seqr.status("ats", T0 + 1000)["next_cmd_time"] == before == int(T0)


# ---------------------------------------------------------------------------
# Elapsed honesty


async def test_elapsed_holds_at_complete_and_resets_at_stop():
    rec = Recorder()
    seqr = Sequencer(rec)
    seqr.load(_rts(delays=(0, 10)))
    seqr.start("rts", T0)
    await seqr.tick(T0 + 10)  # completes at sequence-elapsed 10
    assert seqr.status("rts", T0 + 10)["state"] == "COMPLETE"
    assert seqr.status("rts", T0 + 500)["elapsed_sec"] == 10  # held, not ticking
    seqr.load(_ats(offsets=(0, 60)))
    seqr.start("ats", T0)
    await seqr.tick(T0)
    seqr.stop("ats")
    assert seqr.status("ats", T0 + 500)["elapsed_sec"] == 0  # as-loaded means zero


async def test_elapsed_never_goes_negative():
    rec = Recorder()
    seqr = Sequencer(rec)
    seqr.load(_rts(delays=(0, 100)))
    seqr.start("rts", T0)
    assert seqr.status("rts", T0 - 99)["elapsed_sec"] == 0  # clamped, not -99


async def test_loaded_rts_reports_no_next_cmd_time():
    # Before START an RTS has no base: its delays must not leak out as
    # January-1970 epochs.
    rec = Recorder()
    seqr = Sequencer(rec)
    seqr.load(_rts(delays=(30,)))
    assert seqr.status("rts", T0)["next_cmd_time"] == 0
    seqr.start("rts", T0)
    assert seqr.status("rts", T0)["next_cmd_time"] == int(T0 + 30)
    seqr.stop("rts")
    assert seqr.status("rts", T0 + 5)["next_cmd_time"] == 0  # delays re-base at START


async def test_load_over_a_part_run_plan_says_so():
    rec = Recorder()
    seqr = Sequencer(rec)
    seqr.load(_ats(offsets=(0, 60)))
    seqr.start("ats", T0 - 1)
    await seqr.tick(T0 + 60)  # runs to COMPLETE with 2 executed
    ok, msg = seqr.load(_ats(offsets=(0, 60)))
    assert ok
    assert "replacing plan.ats (2/2 executed)" in msg


# ---------------------------------------------------------------------------
# next_deadline: what the integration's waiter sleeps toward


async def test_next_deadline_tracks_the_earliest_running_entry():
    rec = Recorder()
    seqr = Sequencer(rec)
    assert seqr.next_deadline() is None  # nothing loaded
    seqr.load(_ats(offsets=(60, 120)))
    assert seqr.next_deadline() is None  # LOADED but not RUNNING: no due-times
    seqr.start("ats", T0)
    assert seqr.next_deadline() == T0 + 60
    seqr.load(_rts(delays=(10,)))
    seqr.start("rts", T0)
    assert seqr.next_deadline() == T0 + 10  # the RTS entry is sooner
    await seqr.tick(T0 + 10)  # RTS completes
    assert seqr.next_deadline() == T0 + 60
    seqr.stop("ats")
    assert seqr.next_deadline() is None  # stopped: nothing is due anymore
