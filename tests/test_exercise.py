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


def test_unsatisfiable_range_is_diagnosed_not_stumbled_into():
    """A ValidRange disjoint from the wire type means every possible send
    would be rejected — the exerciser reports the definition problem."""
    from xtce_sim.definition import CommandDef, ParamInfo
    from xtce_sim.exercise import example_values

    bad = ParamInfo("Setpoint", 8, "uint8", valid_min=1000, valid_max=5000)
    with pytest.raises(ValueError, match="no wire-encodable uint8 value satisfies"):
        example_values(bad)
    # And a sweep containing such a command records one FAIL and moves on.
    cmd = CommandDef(name="BAD", opcode=0x30, params=[bad])
    report = run_exercise(
        SimDefinition(space_system_name="S", commands=[cmd]),
        "127.0.0.1", 1, verify=False,
    )
    assert len(report.sends) == 1
    assert not report.sends[0].ok and "no wire-encodable" in report.sends[0].error


# ---- rejection probes -------------------------------------------------------


def test_invalid_value_generation():
    from xtce_sim.exercise import _invalid_value

    # Numeric: one past the declared max, still wire-encodable.
    assert _invalid_value(_p("uint8", valid_min=1, valid_max=4)) == 5
    # Range spans the whole wire type: nothing to probe.
    assert _invalid_value(_p("uint8", valid_min=0, valid_max=255)) is None
    # Unconstrained: nothing to probe.
    assert _invalid_value(_p("int16")) is None
    # Enum: first value past the declared set that fits the field.
    assert _invalid_value(_p(enumerations={"A": 0, "B": 1})) == 2
    # Float: pushed past the max.
    bad = _invalid_value(_p("float32", size_bits=32, valid_min=-1.0, valid_max=1.0))
    assert bad > 1.0


def test_reject_probe_violates_exactly_one_argument(simdef):
    from xtce_sim import codec
    from xtce_sim.exercise import reject_probe

    cmd = _cmd(simdef, "SET_POWER")  # SubsystemId ranged, PowerState enum
    label, args = reject_probe(cmd)
    assert label.startswith("reject-probe")
    violations = codec.range_violations(cmd, args)
    assert len(violations) == 1  # one argument out of range, the rest valid
    codec.encode_command(cmd, args, validate=False)  # and it packs


def test_build_send_plan_sprinkles_probes_deterministically(simdef):
    from xtce_sim.exercise import build_send_plan

    targets = list(simdef.commands)
    plan_a, _ = build_send_plan(targets, reject_probes=5, seed=42)
    plan_b, _ = build_send_plan(targets, reject_probes=5, seed=42)
    assert plan_a == plan_b  # same seed, same sprinkle
    probes = [(c.name, label) for c, label, _args, validate in plan_a if not validate]
    assert len(probes) == 5
    normal = [entry for entry in plan_a if entry[3]]
    baseline_plan, _ = build_send_plan(targets, reject_probes=0)
    assert normal == baseline_plan  # probes ADD to the sweep, never replace


async def test_reject_probes_end_to_end_vehicle_rejects_each(simdef):
    """Probes transmit, and the vehicle answers each with a REJECTED echo."""
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        report = await asyncio.to_thread(
            run_exercise,
            simdef, "127.0.0.1", server.bound_port,
            verify=False, reject_probes=3,
        )
        assert report.ok  # every send (probes included) transmitted
        probes = [s for s in report.sends if s.label.startswith("reject-probe")]
        assert len(probes) == 3
        # Count REJECTED echoes on the downlink: exactly one per probe.
        rejected = 0
        buffer = b""
        for _ in range(300):
            buffer += await asyncio.wait_for(reader.read(65536), timeout=2.0)
            packets, buffer = ccsds.deframe(buffer)
            for pkt in packets:
                if ccsds.CCSDSHeader.unpack(pkt[:6]).apid != ccsds.CMD_ECHO_APID:
                    continue
                status, _ = ccsds.parse_command_echo(pkt)
                if status == ccsds.ECHO_REJECTED:
                    rejected += 1
            if rejected >= 3:
                break
        assert rejected == 3
        writer.close()
    finally:
        await server.stop()


def test_exercise_dry_run_shows_reject_probes(simdef, tmp_path):
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    result = CliRunner().invoke(
        main,
        ["exercise", "--def", str(def_json), "--port", "1", "--dry-run",
         "--reject-probes", "4"],
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("reject-probe") == 4


def test_invalid_value_nonfinite_bounds_are_not_probeable():
    from xtce_sim.exercise import _invalid_value

    # maxInclusive="INF" parses to float inf — can't be exceeded, and must
    # not crash the int path (int(inf) raises OverflowError).
    assert _invalid_value(_p("float64", size_bits=64, valid_max=float("inf"))) is None
    assert _invalid_value(_p("uint8", valid_max=float("inf"))) is None
    assert _invalid_value(_p("uint8", valid_min=float("-inf"))) is None


def test_invalid_enum_value_respects_signed_wire_types():
    from xtce_sim.exercise import _invalid_value

    # int8 enum crowding the top of the wire type: the probe must come from
    # BELOW the declared set, not overflow struct.pack at 128.
    bad = _invalid_value(_p("int8", enumerations={"A": 126, "B": 127}))
    assert bad == 125
    import struct
    struct.pack(">b", bad)  # wire-encodable


def test_invalid_float_value_falls_through_to_min_side():
    from xtce_sim.exercise import _invalid_value

    # Max-side candidate would overflow float32; the min side still works.
    p = _p("float32", size_bits=32, valid_min=0.0, valid_max=3.2e38)
    bad = _invalid_value(p)
    assert bad is not None and bad < 0.0


def test_nonfinite_bound_definition_degrades_to_problem_not_traceback():
    from xtce_sim.definition import CommandDef
    from xtce_sim.exercise import build_send_plan

    cmd = CommandDef(
        name="INF", opcode=0x31,
        params=[_p("uint8", name="X", valid_min=1, valid_max=float("inf"))],
    )
    plan, problems = build_send_plan([cmd], reject_probes=2)
    assert problems and problems[0][0] == "INF"
    assert all(validate for _c, _l, _a, validate in plan)  # no probe emitted


async def test_check_telemetry_does_not_wait_for_event_only_packets():
    """FILE_RECEIPT never beacons (event telemetry), so a healthy idle
    vehicle must satisfy the verify pass within one beacon cycle instead of
    burning the whole timeout waiting for an event that never comes."""
    import time as _time

    from xtce_sim.fileservice import FileService, FileStore

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        service = FileService(FileStore(Path(tmp) / "files"), simdef)
        server = SimServer(
            simdef, host="127.0.0.1", port=0, beacon_interval=0.05, file_service=service
        )
        await server.start()
        try:
            t0 = _time.monotonic()
            health = await asyncio.to_thread(
                check_telemetry, "127.0.0.1", server.bound_port, simdef, timeout=5.0
            )
            elapsed = _time.monotonic() - t0
            assert health.error is None
            assert health.decode_failures == 0
            # Early exit on the first full beacon cycle, not the 5 s timeout.
            assert elapsed < 3.0, f"verify burned {elapsed:.1f}s waiting for an event"
            receipt_apid = simdef.packet_by_name("FILE_RECEIPT").apid
            assert receipt_apid not in health.apids
        finally:
            await server.stop()
