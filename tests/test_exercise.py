"""Tests for the command-surface exerciser (xtce_sim.exercise + `exercise` verb)."""

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim import ccsds
from xtce_sim.cli import main
from xtce_sim.definition import ParamInfo, SimDefinition
from xtce_sim.exercise import (
    TelemetryHealth,
    _tally,
    check_telemetry,
    command_arg_sets,
    example_values,
    run_exercise,
)
from xtce_sim.generate import format_json
from xtce_sim.server import SimServer

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(EXAMPLES / "my_vehicle/my_vehicle.xml")


def _p(python_type="uint8", **kw) -> ParamInfo:
    kw.setdefault("name", "P")
    kw.setdefault("size_bits", 8)
    return ParamInfo(python_type=python_type, **kw)


def _cmd(simdef, name):
    return next(c for c in simdef.commands if c.name == name)


# ---- pure value / arg-set generation -------------------------------------


def test_example_values_enum_yields_every_label():
    p = _p(enumerations={"OFF": 0, "ON": 1, "STANDBY": 2})
    assert example_values(p) == ["OFF", "ON", "STANDBY"]


def test_example_values_numeric_min_and_max():
    assert example_values(_p("uint8", valid_min=3, valid_max=200)) == [3, 200]


def test_example_values_numeric_unbounded_defaults_zero():
    assert example_values(_p("int16")) == [0]


def test_example_values_dedupes_equal_min_max():
    assert example_values(_p("uint8", valid_min=5, valid_max=5)) == [5]


def test_example_values_float_and_string_and_unknown():
    assert example_values(_p("float32", valid_min=1.0, valid_max=2.0)) == [1.0, 2.0]
    assert example_values(_p("string", size_bits=64)) == ["TEST"]  # 8-byte field
    assert example_values(_p("bool")) == [0]  # unknown type -> safe 0


def test_example_values_clamps_int_over_wire_range():
    # A declared max beyond the wire type (e.g. engineering units) is clamped
    # so struct.pack never overflows.
    assert example_values(_p("uint8", valid_min=0, valid_max=1000)) == [0, 255]
    assert example_values(_p("int8", valid_min=-500, valid_max=500)) == [-128, 127]


def test_example_values_clamps_string_to_capacity():
    # Command encoding rejects oversized values, so the sample string must be
    # trimmed to the field's byte capacity (down to empty for 0-size fields).
    assert example_values(_p("string", size_bits=16)) == ["TE"]
    assert example_values(_p("string", size_bits=0)) == [""]
    assert example_values(_p("bytes", size_bits=64)) == ["TEST"]


def test_example_values_clamps_float32_overflow():
    vals = example_values(_p("float32", valid_min=0.0, valid_max=1e39))
    assert vals[-1] <= 3.4028234663852887e38  # clamped to a packable float32
    # and it actually packs
    import struct
    struct.pack(">f", vals[-1])


def test_arg_sets_no_params_is_single_baseline(simdef):
    assert command_arg_sets(_cmd(simdef, "NOOP")) == [("baseline", {})]


def test_arg_sets_cover_every_enum_label(simdef):
    cmd = _cmd(simdef, "SET_POWER")
    ps = next(p for p in cmd.params if p.name == "PowerState")
    sent = {args["PowerState"] for _, args in command_arg_sets(cmd)}
    assert sent == set(ps.enumerations)  # each label is exercised


def test_arg_sets_baseline_first_and_valid(simdef):
    cmd = _cmd(simdef, "SET_POWER")
    label, args = command_arg_sets(cmd)[0]
    assert label == "baseline"
    assert all(args[p.name] == example_values(p)[0] for p in cmd.params)


# ---- report / failure handling (no server needed) ------------------------


def test_run_exercise_records_send_failures_when_unreachable(simdef):
    # Port 1 is never listening -> every send fails, cleanly captured (not raised).
    report = run_exercise(simdef, "127.0.0.1", 1, verify=False)
    assert not report.ok
    assert report.failures and all(not s.ok for s in report.sends)


def test_check_telemetry_connection_error_is_reported(simdef):
    health = check_telemetry("127.0.0.1", 1, simdef, timeout=0.5)
    assert health.error is not None and health.packets == 0


def test_tally_ignores_runt_and_unknown_apid(simdef):
    health = TelemetryHealth()
    _tally(b"\x00\x00", simdef, health)  # runt frame (<6 bytes) -> ignored
    assert health.packets == 0
    unknown = ccsds.build_telemetry_packet(0x7FF, b"", 0)
    _tally(unknown, simdef, health)  # unknown APID -> counted, but not decoded
    assert health.packets == 1 and health.apids == {0x7FF} and health.sample is None


def test_tally_counts_decode_failure_on_short_payload(simdef):
    health = TelemetryHealth()
    pkt = ccsds.build_telemetry_packet(simdef.packets[0].apid, b"\x01", 0)  # too short
    _tally(pkt, simdef, health)
    assert health.decode_failures == 1


async def test_check_telemetry_tolerates_corrupt_frame(simdef):
    # A server that emits a frame with a bad CRC must not crash the exerciser;
    # it's reported as a telemetry-health error instead.
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve_garbage():
        conn, _ = listener.accept()
        good = ccsds.frame(ccsds.build_telemetry_packet(simdef.packets[0].apid, b"\x00" * 4, 0))
        conn.sendall(good[:-2] + bytes([good[-2] ^ 0xFF, good[-1] ^ 0xFF]))  # break the CRC
        conn.close()

    threading.Thread(target=serve_garbage, daemon=True).start()
    try:
        health = await asyncio.to_thread(check_telemetry, "127.0.0.1", port, simdef, timeout=1.0)
    finally:
        listener.close()
    assert health.error is not None and "framing" in health.error


# ---- end to end against an in-process server -----------------------------


async def test_run_exercise_sends_all_and_telemetry_is_healthy(simdef):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        report = await asyncio.to_thread(
            run_exercise, simdef, "127.0.0.1", server.bound_port
        )
        expected = sum(len(command_arg_sets(c)) for c in simdef.commands)
        assert report.ok
        assert len(report.sends) == expected and all(s.ok for s in report.sends)
        assert report.telemetry.error is None
        assert report.telemetry.decode_failures == 0
        assert report.telemetry.packets > 0
    finally:
        await server.stop()


async def test_exercise_cli_end_to_end(simdef, tmp_path):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["exercise", "--def", str(def_json), "--port", str(server.bound_port)],
        )
        assert result.exit_code == 0, result.output
        assert "sent" in result.output and "OK" in result.output
        assert "Telemetry:" in result.output and "0 decode failure(s)" in result.output
    finally:
        await server.stop()


async def test_exercise_cli_command_filter(simdef, tmp_path):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["exercise", "--def", str(def_json), "--port", str(server.bound_port),
             "--command", "NOOP", "--no-verify"],
        )
        assert result.exit_code == 0, result.output
        assert "sent 1/1 OK" in result.output  # only NOOP, one send, no verify
    finally:
        await server.stop()


# ---- pacing and looping ---------------------------------------------------


async def test_run_exercise_on_send_narrates_every_send(simdef):
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        narrated = []
        report = await asyncio.to_thread(
            run_exercise,
            simdef,
            "127.0.0.1",
            server.bound_port,
            commands={"NOOP", "SET_MODE"},
            verify=False,
            on_send=narrated.append,
        )
        assert [(r.command, r.label) for r in narrated] == [
            (s.command, s.label) for s in report.sends
        ]
        assert len(narrated) == len(report.sends) > 1
    finally:
        await server.stop()


def test_run_exercise_pause_waits_between_sends_only(simdef, monkeypatch):
    import xtce_sim.exercise as ex

    slept = []
    monkeypatch.setattr(ex.time, "sleep", slept.append)
    # Unreachable port: sends fail fast but the pacing path still runs.
    # Pacing is BETWEEN sends: no sleep before the first or after the last.
    report = run_exercise(simdef, "127.0.0.1", 1, verify=False, pause=0.25)
    assert slept == [0.25] * (len(report.sends) - 1)


async def test_exercise_cli_pause_narrates_sends(simdef, tmp_path):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["exercise", "--def", str(def_json), "--port", str(server.bound_port),
             "--command", "NOOP", "--no-verify", "--pause", "0.01"],
        )
        assert result.exit_code == 0, result.output
        # Per-send narration line, then the summary.
        assert "NOOP" in result.output and "baseline" in result.output
        assert "sent 1/1 OK" in result.output
    finally:
        await server.stop()


async def test_exercise_cli_loop_repeats_until_interrupt(simdef, tmp_path, monkeypatch):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))

    import xtce_sim.cli as cli_mod

    calls = {"n": 0}
    real_run = cli_mod.run_exercise

    def counted_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return real_run(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "run_exercise", counted_run)
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["exercise", "--def", str(def_json), "--port", str(server.bound_port),
             "--command", "NOOP", "--no-verify", "--loop"],
        )
        assert result.exit_code == 0, result.output
        assert calls["n"] == 3
        assert "— sweep 1 —" in result.output and "— sweep 2 —" in result.output
        # The interrupt hit during sweep 3, so only 2 completed.
        assert "interrupted during sweep 3; 2 sweep(s) completed." in result.output
    finally:
        await server.stop()


def test_exercise_cli_interrupt_on_oneshot_run_is_not_success(simdef, tmp_path, monkeypatch):
    """Ctrl-C on a plain (non-loop) run must not exit 0 — the run never finished."""
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))

    import xtce_sim.cli as cli_mod

    def interrupted_run(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "run_exercise", interrupted_run)
    monkeypatch.setattr(
        cli_mod.socket, "create_connection", lambda *a, **k: _FakeSock()
    )
    result = CliRunner().invoke(
        main, ["exercise", "--def", str(def_json), "--port", "5000", "--no-verify"]
    )
    assert result.exit_code == 130, result.output
    assert "interrupted during sweep 1; 0 sweep(s) completed." in result.output


def test_exercise_cli_loop_stops_and_fails_when_sim_dies(simdef, tmp_path, monkeypatch):
    """A soak whose sends ALL fail must stop looping and exit nonzero."""
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))

    import xtce_sim.cli as cli_mod
    from xtce_sim.exercise import ExerciseReport, SendResult

    def dead_sim_run(*args, **kwargs):
        report = ExerciseReport()
        report.sends.append(SendResult("NOOP", "baseline", False, "refused"))
        return report

    monkeypatch.setattr(cli_mod, "run_exercise", dead_sim_run)
    monkeypatch.setattr(
        cli_mod.socket, "create_connection", lambda *a, **k: _FakeSock()
    )
    result = CliRunner().invoke(
        main,
        ["exercise", "--def", str(def_json), "--port", "5000", "--no-verify", "--loop"],
    )
    assert result.exit_code == 1, result.output
    assert "every send failed — stopping the loop." in result.output
    assert result.output.count("— sweep") == 1  # no spin against a dead port


class _FakeSock:
    def close(self):
        pass


# ---- CLI edge cases (no server) ------------------------------------------


def test_exercise_dry_run_needs_no_server(simdef, tmp_path):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    result = CliRunner().invoke(
        main, ["exercise", "--def", str(def_json), "--port", "1", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "dry run, nothing sent" in result.output


def test_exercise_unknown_command_filter_rejected(simdef, tmp_path):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    result = CliRunner().invoke(
        main, ["exercise", "--def", str(def_json), "--port", "1", "--command", "NOPE"]
    )
    assert result.exit_code != 0
    assert "unknown command" in result.output


def test_exercise_connection_refused_is_clean(simdef, tmp_path):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    result = CliRunner().invoke(
        main, ["exercise", "--def", str(def_json), "--port", "1"]
    )
    assert result.exit_code != 0
    assert "could not reach" in result.output
