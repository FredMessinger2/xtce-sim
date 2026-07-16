"""Sequence file parsing, validation, and ground-side time shifting."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim.cli import main
from xtce_sim.definition import SimDefinition
from xtce_sim.sequences import (
    SequenceError,
    format_utc,
    parse_ats,
    parse_duration,
    parse_rts,
    shift_ats,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
IMAGING = EXAMPLES / "imaging_sat/imaging_sat.xml"

ATS_TEXT = """\
# burn plan, rev 3
2026-03-15T14:30:00Z  ADCS_SET_MODE Mode=NADIR
2026-03-15T14:29:00Z  IMAGER_ON     # deliberately out of order
2026-03-15T14:31:30.5Z ADCS_SLEW_TO_QUATERNION Q1=0 Q2=0 Q3=0.7071 Q4=0.7071
"""

RTS_TEXT = """\
+30   HEATER_OFF HeaterId=1
+0    ADCS_SET_MODE Mode=SUNSAFE
+2.5  IMAGER_ON
"""


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(IMAGING)


# ---------------------------------------------------------------------------
# Parsing


def test_ats_parses_sorts_and_keeps_utc(simdef):
    seq = parse_ats(ATS_TEXT, "burn.ats", simdef)
    assert seq.kind == "ats"
    assert [e.command for e in seq.entries] == [
        "IMAGER_ON",
        "ADCS_SET_MODE",
        "ADCS_SLEW_TO_QUATERNION",
    ]
    # 2026-03-15T14:29:00Z as epoch, timezone-independent.
    assert format_utc(seq.entries[0].time) == "2026-03-15T14:29:00Z"
    assert seq.span == pytest.approx(150.5)
    assert seq.entries[1].args == {"Mode": "NADIR"}
    assert seq.entries[1].line == 2  # source line survives sorting


def test_rts_stores_delays_not_absolute_times(simdef):
    seq = parse_rts(RTS_TEXT, "safe.rts", simdef)
    assert [e.time for e in seq.entries] == [0.0, 2.5, 30.0]
    assert seq.entries[0].command == "ADCS_SET_MODE"
    # No clock was consulted: the delays ARE the parse result. Re-basing
    # to a start time is the scheduler's job, in memory, at START.


def test_naked_and_offset_timestamps_are_utc():
    naked = parse_ats("2026-03-15T14:30:00 IMAGER_ON\n", "a.ats")
    zulu = parse_ats("2026-03-15T14:30:00Z IMAGER_ON\n", "b.ats")
    offset = parse_ats("2026-03-15T16:30:00+02:00 IMAGER_ON\n", "c.ats")
    assert naked.entries[0].time == zulu.entries[0].time == offset.entries[0].time


def test_ats_problems_are_total_and_all_or_nothing(simdef):
    text = """\
2026-03-15T14:30:00Z IMAGER_ON
not-a-time IMAGER_ON
2026-03-15T14:31:00Z
2026-03-15T14:32:00Z IMAGER_ON badtoken
2026-03-15T14:33:00Z IMAGER_ON A=1 A=2
2026-03-15T14:34:00Z NO_SUCH_COMMAND
2026-03-15T14:35:00Z ADCS_SET_MODE Mode=WARP
2026-03-15T14:36:00Z ADCS_WHEEL_ENABLE WheelId=9
"""
    with pytest.raises(SequenceError) as exc:
        parse_ats(text, "bad.ats", simdef)
    problems = exc.value.problems
    text_all = "\n".join(problems)
    assert "line 2: invalid UTC timestamp" in text_all
    assert "line 3: expected" in text_all
    assert "line 4: expected KEY=VALUE, got 'badtoken'" in text_all
    assert "line 5: duplicate argument 'A'" in text_all
    assert "line 6: unknown command 'NO_SUCH_COMMAND'" in text_all
    assert "line 7:" in text_all and "WARP" in text_all  # bad enum label
    assert "line 8:" in text_all and "WheelId" in text_all  # ValidRange caught at parse
    assert len(problems) == 7  # every problem, one pass


def test_rts_rejects_bad_delays():
    # Plain decimal seconds only: float() niceties like exponents and
    # underscores are not how anyone writes a command plan.
    for bad, why in (
        ("30", "must start with '+'"),
        ("+-5", "non-negative"),
        ("+inf", "non-negative"),
        ("+nan", "non-negative"),
        ("+abc", "non-negative"),
        ("+1e3", "non-negative"),
        ("+1_0", "non-negative"),
    ):
        with pytest.raises(SequenceError) as exc:
            parse_rts(f"{bad} IMAGER_ON\n", "r.rts")
        assert why in str(exc.value)


def test_space_separated_timestamp_is_rejected_with_guidance():
    # '2026-03-15 14:30:00' tokenizes as a bare date plus a bogus command;
    # silently reading it as midnight would fire the plan 14.5 hours early.
    with pytest.raises(SequenceError) as exc:
        parse_ats("2026-03-15 14:30:00 IMAGER_ON\n", "space.ats")
    assert "no time of day" in str(exc.value)
    assert "T between date and time" in str(exc.value)
    with pytest.raises(SequenceError):
        parse_ats("2026-03-15 IMAGER_ON\n", "bare-date.ats")


def test_equal_timestamps_keep_file_order():
    text = (
        "2026-03-15T14:30:00Z IMAGER_ON\n"
        "2026-03-15T14:30:00Z HEATER_OFF\n"
        "2026-03-15T14:30:00Z ADCS_DESATURATE\n"
    )
    seq = parse_ats(text, "ties.ats")
    assert [e.command for e in seq.entries] == ["IMAGER_ON", "HEATER_OFF", "ADCS_DESATURATE"]


def test_problems_report_in_file_order(simdef):
    # A definition-level problem on line 1 must list before a syntax
    # problem on line 3, not after it.
    text = (
        "2026-03-15T14:30:00Z ADCS_WHEEL_ENABLE WheelId=9\n"
        "2026-03-15T14:31:00Z IMAGER_ON\n"
        "not-a-time IMAGER_ON\n"
    )
    with pytest.raises(SequenceError) as exc:
        parse_ats(text, "order.ats", simdef)
    first, second = exc.value.problems
    assert first.startswith("line 1:") and "WheelId" in first
    assert second.startswith("line 3:")


def test_rts_short_line_names_the_rts_shape():
    with pytest.raises(SequenceError) as exc:
        parse_rts("+30\n", "r.rts")
    assert "expected '+<seconds> <COMMAND>" in str(exc.value)


def test_empty_sequence_is_an_error():
    with pytest.raises(SequenceError) as exc:
        parse_ats("# only a comment\n\n", "empty.ats")
    assert "at least one" in str(exc.value)


def test_syntax_only_parse_skips_definition_checks():
    # Without a definition (the shift tool's mode), unknown commands pass;
    # syntax problems still fail.
    seq = parse_ats("2026-03-15T14:30:00Z NOT_YET_DEFINED A=1\n", "later.ats")
    assert seq.entries[0].command == "NOT_YET_DEFINED"


# ---------------------------------------------------------------------------
# Shifting


def test_shift_preserves_layout_and_spacing():
    delta = 86400.0  # exactly one day: same wall time, next day
    shifted = shift_ats(ATS_TEXT, "burn.ats", delta)
    assert "# burn plan, rev 3" in shifted  # comments survive
    assert "# deliberately out of order" in shifted  # trailing comments survive
    assert "2026-03-16T14:30:00Z  ADCS_SET_MODE Mode=NADIR" in shifted
    assert "2026-03-16T14:31:30.5Z ADCS_SLEW_TO_QUATERNION" in shifted  # sub-second kept
    before = parse_ats(ATS_TEXT, "a.ats")
    after = parse_ats(shifted, "b.ats")
    assert after.span == pytest.approx(before.span)  # spacing preserved
    assert after.entries[0].time - before.entries[0].time == pytest.approx(delta)


def test_shift_rejects_broken_files():
    with pytest.raises(SequenceError):
        shift_ats("garbage here\n", "g.ats", 10.0)


def test_shift_preserves_crlf_and_missing_final_newline():
    crlf = "# plan\r\n2026-03-15T14:30:00Z IMAGER_ON\r\n"
    shifted = shift_ats(crlf, "win.ats", 60.0)
    assert shifted == "# plan\r\n2026-03-15T14:31:00Z IMAGER_ON\r\n"
    bare = "2026-03-15T14:30:00Z IMAGER_ON"  # no trailing newline
    assert shift_ats(bare, "bare.ats", 60.0) == "2026-03-15T14:31:00Z IMAGER_ON"


def test_shift_leaves_timestamp_lookalike_argument_values_alone():
    text = "2026-03-15T14:30:00Z LOG Note=2026-03-15T14:30:00Z\n"
    shifted = shift_ats(text, "look.ats", 3600.0)
    assert shifted == "2026-03-15T15:30:00Z LOG Note=2026-03-15T14:30:00Z\n"


def test_format_utc_precision():
    t = parse_ats("2026-03-15T14:30:00Z IMAGER_ON\n", "a.ats").entries[0].time
    assert format_utc(t) == "2026-03-15T14:30:00Z"
    assert format_utc(t + 0.25) == "2026-03-15T14:30:00.25Z"


# ---------------------------------------------------------------------------
# Durations


def test_parse_duration_forms():
    assert parse_duration("30s") == 30.0
    assert parse_duration("500ms") == 0.5
    assert parse_duration("5m") == 300.0
    assert parse_duration("1h") == 3600.0
    assert parse_duration("90") == 90.0  # bare numbers are seconds
    assert parse_duration("2.5s") == 2.5
    for bad in ("", "fast", "-5s", "5 s", "1d"):
        with pytest.raises(ValueError):
            parse_duration(bad)


# ---------------------------------------------------------------------------
# CLI


def test_seq_check_validates_and_lists(tmp_path):
    f = tmp_path / "burn.ats"
    f.write_text(ATS_TEXT)
    result = CliRunner().invoke(main, ["seq", "check", str(f), "--def", str(IMAGING)])
    assert result.exit_code == 0, result.output
    assert "OK: burn.ats — 3 command(s) over 150.5 s" in result.output
    assert "IMAGER_ON" in result.output


def test_seq_check_reports_every_problem(tmp_path):
    f = tmp_path / "bad.ats"
    f.write_text("2026-01-01T00:00:00Z ADCS_WHEEL_ENABLE WheelId=9\nnot-a-time X\n")
    result = CliRunner().invoke(main, ["seq", "check", str(f), "--def", str(IMAGING)])
    assert result.exit_code != 0
    assert "WheelId" in result.output
    assert "invalid UTC timestamp" in result.output


def test_seq_check_rejects_unknown_extension(tmp_path):
    f = tmp_path / "plan.txt"
    f.write_text("+1 IMAGER_ON\n")
    result = CliRunner().invoke(main, ["seq", "check", str(f), "--def", str(IMAGING)])
    assert result.exit_code != 0
    assert ".ats or .rts" in result.output


def test_seq_shift_moves_first_command_to_start_in(tmp_path):
    f = tmp_path / "burn.ats"
    f.write_text(ATS_TEXT)
    result = CliRunner().invoke(main, ["seq", "shift", str(f), "--start-in", "10m"])
    assert result.exit_code == 0, result.output
    shifted = parse_ats(result.output, "shifted.ats")
    import time as _time

    lead = shifted.entries[0].time - _time.time()
    assert 590 < lead < 610  # first command ~10 minutes out
    assert shifted.span == pytest.approx(150.5)


def test_seq_shift_write_rewrites_in_place(tmp_path):
    f = tmp_path / "burn.ats"
    f.write_text(ATS_TEXT)
    result = CliRunner().invoke(main, ["seq", "shift", str(f), "--start-in", "30s", "--write"])
    assert result.exit_code == 0, result.output
    assert "first command at" in result.output
    assert "# burn plan, rev 3" in f.read_text()  # comments survive in-place


def test_seq_shift_refuses_rts(tmp_path):
    f = tmp_path / "safe.rts"
    f.write_text(RTS_TEXT)
    result = CliRunner().invoke(main, ["seq", "shift", str(f), "--start-in", "30s"])
    assert result.exit_code != 0
    assert "already relative" in result.output


def test_seq_check_accepts_uppercase_suffix(tmp_path):
    f = tmp_path / "BURN.ATS"
    f.write_text(ATS_TEXT)
    result = CliRunner().invoke(main, ["seq", "check", str(f), "--def", str(IMAGING)])
    assert result.exit_code == 0, result.output


def test_seq_tools_report_non_utf8_cleanly(tmp_path):
    f = tmp_path / "legacy.ats"
    f.write_bytes(b"# caf\xe9 plan\n2026-03-15T14:30:00Z IMAGER_ON\n")  # latin-1 byte
    for verb in (
        ["seq", "check", str(f), "--def", str(IMAGING)],
        ["seq", "shift", str(f), "--start-in", "30s"],
    ):
        result = CliRunner().invoke(main, verb)
        assert result.exit_code != 0
        assert "could not read" in result.output
        assert "Traceback" not in result.output


def test_kind_for_is_case_insensitive_and_shared():
    # One classifier for the ground CLI and the vehicle's LOAD guard:
    # uppercase names are common flight-file convention.
    from xtce_sim.sequences import kind_for

    assert kind_for("plan.ats") == "ats"
    assert kind_for("PLAN.ATS") == "ats"
    assert kind_for("Safe.Rts") == "rts"
    assert kind_for("notes.txt") is None
    assert kind_for("noextension") is None


def test_seq_shift_output_is_whole_seconds(tmp_path):
    f = tmp_path / "burn.ats"
    f.write_text("2026-03-15T14:30:00Z IMAGER_ON\n")
    result = CliRunner().invoke(main, ["seq", "shift", str(f), "--start-in", "10m"])
    assert result.exit_code == 0, result.output
    assert "." not in result.output.split()[0]  # no microsecond noise
