"""CLI-level tests for the generate / run / send / monitor verbs."""

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim import ccsds, cli, codec
from xtce_sim.cli import main
from xtce_sim.definition import SimDefinition
from xtce_sim.generate import format_json
from xtce_sim.server import SimServer

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"
CMD = str(DATA / "my_vehicle/my_vehicle_commands.xml")
TLM = str(DATA / "my_vehicle/my_vehicle_telemetry.xml")


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce([DATA / "my_vehicle/my_vehicle_commands.xml", TLM])


# ---------------------------------------------------------------- generate ----


def test_generate_writes_outputs_and_emit_py(tmp_path):
    out = tmp_path / "gen"
    runner = CliRunner()
    result = runner.invoke(main, ["generate", CMD, TLM, "--out", str(out), "--emit-py"])
    assert result.exit_code == 0, result.output
    assert (out / "cmd_tlm.txt").exists()
    assert (out / "cmd_tlm.json").exists()
    assert (out / "generated.py").exists()
    assert "MyVehicle" in result.output


def test_generate_default_id_uses_file_stem():
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Copy example next to cwd so the default id (file stem) path works.
        Path("sat.xml").write_text(Path(CMD).read_text())
        result = runner.invoke(main, ["generate", "sat.xml"])
        assert result.exit_code == 0, result.output
        assert Path("runs/sat/cmd_tlm.json").exists()


# ---------------------------------------------------- _load_definition paths --


def test_load_definition_requires_source():
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--port", "1", "NOOP"])
    assert result.exit_code != 0
    assert "specify --id" in result.output


def test_load_definition_missing_id(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["send", "--id", "ghost", "--port", "1", "NOOP"])
        assert result.exit_code != 0
        assert "no runs/ghost/cmd_tlm.json found" in result.output


def test_load_definition_from_xml_def():
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--def", CMD, "--port", "1", "BOGUSCMD"])
    # Definition loaded from XML; command lookup then fails cleanly.
    assert "unknown command" in result.output


# --------------------------------------------------------------------- send ---


def test_send_unknown_command(tmp_path):
    def_json = tmp_path / "d.json"
    def_json.write_text(format_json(SimDefinition.from_xtce([Path(CMD), Path(TLM)])))
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--def", str(def_json), "--port", "1", "NOPE"])
    assert result.exit_code != 0
    assert "unknown command" in result.output


def test_send_bad_pair(tmp_path):
    def_json = tmp_path / "d.json"
    def_json.write_text(format_json(SimDefinition.from_xtce([Path(CMD), Path(TLM)])))
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--def", str(def_json), "--port", "1", "NOOP", "oops"])
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


def test_send_bad_enum(tmp_path):
    def_json = tmp_path / "d.json"
    def_json.write_text(format_json(SimDefinition.from_xtce([Path(CMD), Path(TLM)])))
    runner = CliRunner()
    result = runner.invoke(
        main, ["send", "--def", str(def_json), "--port", "1", "SET_POWER", "PowerState=BOGUS"]
    )
    assert result.exit_code != 0
    assert "unknown enum" in result.output


def test_send_connection_refused(tmp_path):
    def_json = tmp_path / "d.json"
    def_json.write_text(format_json(SimDefinition.from_xtce([Path(CMD), Path(TLM)])))
    runner = CliRunner()
    # Port 1 is not listening -> clean ClickException, not a traceback.
    result = runner.invoke(
        main, ["send", "--def", str(def_json), "--port", "1", "NOOP"]
    )
    assert result.exit_code != 0
    assert "could not reach" in result.output


# ---------------------------------------------------------------------- run ---


def test_run_happy_path(monkeypatch, tmp_path):
    """run builds, dumps, and hands off to asyncio.run (stubbed)."""

    def fake_run(coro):
        coro.close()  # avoid 'coroutine never awaited'
        return None

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    runner = CliRunner()
    result = runner.invoke(
        main, ["run", CMD, TLM, "--port", "5999", "--id", "unit", "--out", str(tmp_path / "r")]
    )
    assert result.exit_code == 0, result.output
    assert "Serving unit" in result.output


def test_run_keyboard_interrupt(monkeypatch, tmp_path):
    def fake_run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["run", CMD, TLM, "--port", "5999", "--id", "unit", "--out", str(tmp_path / "r"), "--live"],
    )
    assert result.exit_code == 0
    assert "Stopped" in result.output


def test_run_bind_failure_is_clean(monkeypatch, tmp_path):
    """A bind error (e.g. port in use) yields a clean message, not a traceback."""

    def fake_run(coro):
        coro.close()
        raise OSError("address already in use")

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    result = CliRunner().invoke(
        main, ["run", CMD, TLM, "--port", "5999", "--id", "unit", "--out", str(tmp_path / "r")]
    )
    assert result.exit_code != 0
    assert "could not serve" in result.output
    assert "Traceback" not in result.output


def test_run_rejects_out_of_range_port(tmp_path):
    runner = CliRunner()
    for bad in ("0", "70000"):
        result = runner.invoke(
            main, ["run", CMD, TLM, "--port", bad, "--out", str(tmp_path / "r")]
        )
        assert result.exit_code != 0  # IntRange(1, 65535) rejects it
        assert "Traceback" not in result.output


# ------------------------------------------------------------------- monitor --


def _def_json(tmp_path, simdef) -> str:
    p = tmp_path / "cmd_tlm.json"
    p.write_text(format_json(simdef))
    return str(p)


async def _invoke_monitor(server: SimServer, args: list[str]):
    runner = CliRunner()
    return await asyncio.to_thread(
        runner.invoke, main, ["monitor", "--port", str(server.bound_port), *args]
    )


@pytest.mark.parametrize("style", ["compact", "table", "dashboard"])
async def test_monitor_styles(simdef, tmp_path, style):
    def_json = _def_json(tmp_path, simdef)
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        count = "2" if style == "dashboard" else "3"
        result = await _invoke_monitor(
            server, ["--def", def_json, "--style", style, "--count", count]
        )
        assert result.exit_code == 0, result.output
        assert "Monitoring" in result.output
        assert "0x01" in result.output or "HOUSEKEEPING" in result.output
    finally:
        await server.stop()


async def test_monitor_packet_filter_and_fields(simdef, tmp_path):
    def_json = _def_json(tmp_path, simdef)
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        result = await _invoke_monitor(
            server,
            ["--def", def_json, "--packet", "HOUSEKEEPING", "--fields", "--count", "1"],
        )
        assert result.exit_code == 0, result.output
        # Filtered to HOUSEKEEPING and --fields shows all fields (incl. later ones).
        assert "HOUSEKEEPING" in result.output
        assert "POINTING_ERROR" in result.output
    finally:
        await server.stop()


def test_monitor_connection_refused(tmp_path, simdef):
    def_json = _def_json(tmp_path, simdef)
    runner = CliRunner()
    result = runner.invoke(
        main, ["monitor", "--def", def_json, "--port", "1", "--count", "1"]
    )
    assert result.exit_code != 0
    assert "could not reach" in result.output


def test_monitor_rejects_negative_count(tmp_path, simdef):
    def_json = _def_json(tmp_path, simdef)
    result = CliRunner().invoke(
        main, ["monitor", "--def", def_json, "--port", "1", "--count", "-1"]
    )
    assert result.exit_code != 0  # IntRange(min=0) rejects before connecting
    assert "could not reach" not in result.output


def test_decode_packet_branches(simdef):
    pkt = simdef.packets[0]
    frame = ccsds.build_telemetry_packet(pkt.apid, codec.pack_telemetry(pkt, None), 7)

    # Runt frame (< 6 bytes) -> skipped.
    assert cli._decode_packet(b"\x00\x00", simdef, set(), {}) is None

    # Active filter that doesn't include this packet -> skipped.
    assert cli._decode_packet(frame, simdef, {"NOPE"}, {}) is None

    # Normal decode -> full tuple; the shared prefix is cached per APID.
    prefixes: dict[int, str] = {}
    apid, name, seq, meta, _prefix = cli._decode_packet(frame, simdef, set(), prefixes)
    assert (apid, name, seq) == (pkt.apid, pkt.name, 7)
    assert len(meta) == len(pkt.fields)
    assert pkt.apid in prefixes  # cached

    # Unknown APID -> synthetic name, no field meta.
    unknown = ccsds.build_telemetry_packet(0x7FF, b"", 1)
    _, uname, _, umeta, _ = cli._decode_packet(unknown, simdef, set(), {})
    assert uname == "APID_0x7FF" and umeta == []

    # Truncated payload for a known packet -> raw fallback, not a crash.
    bad = ccsds.build_telemetry_packet(pkt.apid, b"\x01", 2)
    _, _, _, bad_meta, _ = cli._decode_packet(bad, simdef, set(), {})
    assert bad_meta and bad_meta[0][0] == "<raw>"


def test_decode_packet_shows_enum_labels():
    # An enumerated field displays its label; unmatched raw values stay raw.
    d = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    hk = d.packet_by_name("HOUSEKEEPING")
    frame = ccsds.build_telemetry_packet(
        hk.apid, codec.pack_telemetry(hk, {"HK_SYSTEM_MODE": 3}), 1
    )
    _, _, _, meta, _ = cli._decode_packet(frame, d, set(), {})
    values = {name: value for name, value, _ in meta}
    assert values["HK_SYSTEM_MODE"] == "IMAGING"  # 3 -> label
    frame = ccsds.build_telemetry_packet(
        hk.apid, codec.pack_telemetry(hk, {"HK_SYSTEM_MODE": 99}), 2
    )
    _, _, _, meta, _ = cli._decode_packet(frame, d, set(), {})
    values = {name: value for name, value, _ in meta}
    assert values["HK_SYSTEM_MODE"] == 99  # no label for 99 -> raw value


async def test_dashboard_frames_are_complete_snapshots(simdef, tmp_path):
    """Each dashboard frame is a full cycle (all APIDs), painted once per cycle."""
    def_json = _def_json(tmp_path, simdef)
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        result = await _invoke_monitor(
            server, ["--def", def_json, "--style", "dashboard", "--count", "2"]
        )
        assert result.exit_code == 0, result.output
        # Exactly two frames painted (not one-per-packet), each a full snapshot.
        assert result.output.count("xtce-sim monitor") == 2
        # A full cycle includes many of the 18 packets (not a partial frame).
        assert result.output.count("HOUSEKEEPING") == 2
    finally:
        await server.stop()


async def test_table_style_repaints_in_place_on_tty(simdef, tmp_path, monkeypatch):
    # On a TTY each table frame is prefixed with cursor-home + erase-below
    # (single write -> no flash); piped output (the other monitor tests)
    # stays plain and greppable. CliRunner swaps sys.stdout during invoke,
    # so the TTY check is patched via the cli helper, not sys.stdout itself.
    monkeypatch.setattr(cli, "_stdout_isatty", lambda: True)
    def_json = _def_json(tmp_path, simdef)
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=0.02)
    await server.start()
    try:
        result = await _invoke_monitor(
            server, ["--def", def_json, "--style", "table", "--count", "2"]
        )
        assert result.exit_code == 0, result.output
        assert "\033[H\033[J" in result.output  # in-place repaint marker
    finally:
        await server.stop()


# ---- calibrated display: engineering units by default, --raw for counts ------


def _hk_packet_bytes(simdef):
    """One HOUSEKEEPING packet with known raw counts in the calibrated fields."""
    hk = simdef.packet_by_name("HOUSEKEEPING")
    values = {f.name: 0 for f in hk.fields if f.python_type != "bytes"}
    values["HK_BATTERY_VOLTAGE"] = 60  # * 0.125 -> 7.5 V
    values["HK_TEMP_THERMISTOR"] = 512  # spline -> -20.0 degC
    payload = codec.pack_telemetry(hk, values)
    return ccsds.build_telemetry_packet(hk.apid, payload), hk


def test_decode_shows_engineering_units_by_default():
    simdef = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    packet, hk = _hk_packet_bytes(simdef)
    _, _, _, meta, _ = cli._decode_packet(packet, simdef, set(), {})
    rows = dict((name, value) for name, value, _ in meta)
    assert rows["HK_BATTERY_VOLTAGE"] == 7.5
    assert rows["HK_TEMP_THERMISTOR"] == -20.0
    assert rows["HK_CMD_RECV_COUNT"] == 0  # uncalibrated fields untouched


def test_decode_raw_shows_wire_counts():
    simdef = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    packet, hk = _hk_packet_bytes(simdef)
    _, _, _, meta, _ = cli._decode_packet(packet, simdef, set(), {}, raw=True)
    rows = dict((name, value) for name, value, _ in meta)
    assert rows["HK_BATTERY_VOLTAGE"] == 60
    assert rows["HK_TEMP_THERMISTOR"] == 512


def test_display_value_enum_label_beats_calibrator():
    # An enumerated field never calibrates: the label is the display value.
    from xtce_sim.definition import CalibratorInfo, FieldInfo

    f = FieldInfo(
        name="X", size_bits=8, python_type="uint8",
        enumerations={"ON": 1},
        calibrator=CalibratorInfo(coefficients=[(2.0, 1)]),
    )
    assert cli._display_value(f, 1) == "ON"
    assert cli._display_value(f, 3) == 6.0  # no label match -> calibrated
    assert cli._display_value(f, 3, raw=True) == 3


def test_raw_view_drops_units_on_calibrated_fields():
    # Counts are unitless; "60 V" would be a lie. Uncalibrated fields keep
    # their units in both views.
    simdef = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    packet, hk = _hk_packet_bytes(simdef)
    _, _, _, meta, _ = cli._decode_packet(packet, simdef, set(), {}, raw=True)
    units = {name: unit for name, _, unit in meta}
    assert units["HK_BATTERY_VOLTAGE"] is None  # calibrated: no unit on counts
    assert units["HK_TIMESTAMP"] == "s"  # uncalibrated: unit stays
    _, _, _, meta_eu, _ = cli._decode_packet(packet, simdef, set(), {})
    units_eu = {name: unit for name, _, unit in meta_eu}
    assert units_eu["HK_BATTERY_VOLTAGE"] == "V"  # calibrated view keeps it


def test_run_artifacts_live_with_the_satellite(tmp_path):
    # runs/<id>/ goes in the satellite's directory, not the CWD.
    sat = tmp_path / "bird"
    sat.mkdir()
    xml = DATA / "my_vehicle/my_vehicle.xml"
    (sat / "bird.xml").write_text(xml.read_text())
    runner = CliRunner()
    result = runner.invoke(main, ["generate", str(sat / "bird.xml")])
    assert result.exit_code == 0, result.output
    assert (sat / "runs" / "bird" / "cmd_tlm.json").exists()


def test_id_resolves_into_satellite_directories(tmp_path, monkeypatch):
    # A client run from a parent directory finds the satellite's dump.
    sat = tmp_path / "bird"
    sat.mkdir()
    (DATA / "my_vehicle/my_vehicle.xml").read_text()  # ensure exists
    dump = sat / "runs" / "sat-x"
    dump.mkdir(parents=True)
    src = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    dump.joinpath("cmd_tlm.json").write_text(format_json(src))
    monkeypatch.chdir(tmp_path)
    d = cli._load_definition("sat-x", None)
    assert d.space_system_name == "MyVehicle"


def test_no_behavior_serves_interface_only(tmp_path):
    # An unrelated .toml beside the XTCE would block startup; --no-behavior
    # skips discovery entirely.
    sat = tmp_path / "bird"
    sat.mkdir()
    (sat / "bird.xml").write_text((DATA / "my_vehicle/my_vehicle.xml").read_text())
    (sat / "notes.toml").write_text("[project]\nname = 'not behavior'\n")
    runner = CliRunner()
    fails = runner.invoke(main, ["inspect", str(sat / "bird.xml")])
    assert fails.exit_code != 0  # auto-discovery trips on notes.toml...
    assert "--no-behavior" in fails.output  # ...and says how to get out
    ok = runner.invoke(main, ["inspect", str(sat / "bird.xml"), "--no-behavior"])
    assert ok.exit_code == 0, ok.output


def test_send_warns_on_hazardous_command(tmp_path):
    """A critical command prints its declared significance before sending."""
    from xtce_sim.generate import format_json

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    # Port 1 refuses: the warning must appear even though the send fails.
    result = CliRunner().invoke(
        main, ["send", "--def", str(def_json), "--port", "1", "ADCS_DESATURATE"]
    )
    assert "ADCS_DESATURATE is CRITICAL" in result.output
    assert "momentum unloads" in result.output


def test_send_is_quiet_about_normal_commands(tmp_path):
    from xtce_sim.generate import format_json

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    result = CliRunner().invoke(
        main, ["send", "--def", str(def_json), "--port", "1", "NOOP"]
    )
    assert "is NORMAL" not in result.output and "is CRITICAL" not in result.output


def test_exercise_dry_run_badges_hazardous_commands(tmp_path):
    from xtce_sim.generate import format_json

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    result = CliRunner().invoke(
        main,
        ["exercise", "--def", str(def_json), "--port", "1", "--dry-run",
         "--command", "ADCS_DESATURATE", "--command", "ADCS_RESET_ESTIMATOR",
         "--command", "NOOP"],
    )
    assert result.exit_code == 0, result.output
    assert "[CRITICAL]" in result.output and "[VITAL]" in result.output


def test_send_rejects_out_of_range_locally(tmp_path):
    """Ground-side enforcement: nothing is transmitted, the message names
    the argument and its declared range."""
    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    result = CliRunner().invoke(
        main,
        ["send", "--def", str(def_json), "--port", "1",
         "ADCS_WHEEL_SET_SPEED", "WheelId=7", "Speed=0"],
    )
    assert result.exit_code != 0
    assert "WheelId=7" in result.output and "outside ValidRange" in result.output
    assert "sent" not in result.output
    # It failed BEFORE connecting: no could-not-reach error for port 1.
    assert "could not reach" not in result.output


async def test_send_force_transmits_and_vehicle_rejects(tmp_path):
    """--force bypasses the ground check; the vehicle rejects for itself —
    observed via the REJECTED command echo on a watching connection."""
    import asyncio

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    server = SimServer(simdef, host="127.0.0.1", port=0, beacon_interval=10.0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["send", "--def", str(def_json), "--port", str(server.bound_port),
             "--force", "ADCS_WHEEL_SET_SPEED", "WheelId=7", "Speed=0"],
        )
        assert result.exit_code == 0, result.output
        assert "skipping ground-side range checks" in result.output
        assert "sent ADCS_WHEEL_SET_SPEED" in result.output
        # The vehicle's verdict, on the downlink:
        buffer = b""
        status = None
        for _ in range(200):
            buffer += await asyncio.wait_for(reader.read(4096), timeout=2.0)
            packets, buffer = ccsds.deframe(buffer)
            echo = next(
                (p for p in packets
                 if ccsds.CCSDSHeader.unpack(p[:6]).apid == ccsds.CMD_ECHO_APID),
                None,
            )
            if echo is not None:
                status, _embedded = ccsds.parse_command_echo(echo)
                break
        assert status == ccsds.ECHO_REJECTED
        writer.close()
    finally:
        await server.stop()


def test_reject_probes_with_no_probeable_commands_warns(tmp_path):
    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    # NOOP has no arguments: nothing to push out of range.
    result = CliRunner().invoke(
        main,
        ["exercise", "--def", str(def_json), "--port", "1", "--dry-run",
         "--command", "NOOP", "--reject-probes", "3"],
    )
    assert result.exit_code == 0, result.output
    assert "no selected command has a probe-able argument" in result.output
    assert "[REJECT-PROBE]" not in result.output  # none could be planned


def test_report_counts_only_transmitted_probes(monkeypatch, capsys):
    from xtce_sim.cli import _print_exercise_report
    from xtce_sim.exercise import ExerciseReport, SendResult

    report = ExerciseReport()
    report.sends.append(SendResult("A", "reject-probe X=9", True))
    report.sends.append(SendResult("B", "reject-probe Y=9", False, "refused"))
    _print_exercise_report(report, verify=False)
    out = capsys.readouterr().out
    assert "Rejection probes: 1 transmitted" in out  # the failed one not counted
    assert "should reject" in out  # hedged: the exerciser doesn't read echoes


# ------------------------------------------------------------------ upload ----


async def _imaging_file_server(tmp_path):
    """A live imaging_sat server with a file store, plus its def JSON path."""
    from xtce_sim.fileservice import FileService, FileStore

    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    service = FileService(FileStore(tmp_path / "files"), simdef)
    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=10.0, file_service=service
    )
    await server.start()
    return server, def_json


async def test_upload_verb_end_to_end(tmp_path):
    """The console workflow: upload a file, read the vehicle's receipt back."""
    server, def_json = await _imaging_file_server(tmp_path)
    try:
        plan = tmp_path / "plan.ats"
        plan.write_bytes(b"2026-07-12T00:00:00Z NOOP\n" * 50)
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["upload", str(plan), "--def", str(def_json),
             "--port", str(server.bound_port), "--chunk-size", "128"],
        )
        assert result.exit_code == 0, result.output
        assert "uploading plan.ats" in result.output
        assert "receipt: SUCCESS" in result.output
        assert (tmp_path / "files/plan.ats").read_bytes() == plan.read_bytes()
    finally:
        await server.stop()


async def test_upload_verb_reports_vehicle_refusal(tmp_path):
    """A file too big for the store comes back as the vehicle's FAILED
    receipt, and the verb exits nonzero — not quiet success."""
    server, def_json = await _imaging_file_server(tmp_path)
    server.file_service.store.quota = 10  # shrink the store under the file
    try:
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 100)
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["upload", str(big), "--def", str(def_json),
             "--port", str(server.bound_port)],
        )
        assert result.exit_code != 0
        assert "vehicle refused" in result.output
        assert not (tmp_path / "files/big.bin").exists()
    finally:
        await server.stop()


def test_upload_refuses_a_doomed_filename_before_sending(tmp_path):
    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    long_named = tmp_path / ("n" * 40 + ".ats")
    long_named.write_bytes(b"x")
    result = CliRunner().invoke(
        main,
        ["upload", str(long_named), "--def", str(def_json), "--port", "1"],
    )
    assert result.exit_code != 0
    assert "exceeds 32 bytes" in result.output
    assert "could not reach" not in result.output  # refused before connecting


def test_upload_bad_timeout_is_a_clean_error(tmp_path):
    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    f = tmp_path / "f.ats"
    f.write_bytes(b"x")
    result = CliRunner().invoke(
        main,
        ["upload", str(f), "--def", str(def_json), "--port", "1",
         "--timeout", "soon"],
    )
    assert result.exit_code != 0


def test_upload_connection_refused_is_a_clean_error(tmp_path):
    simdef = SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    f = tmp_path / "f.ats"
    f.write_bytes(b"x")
    result = CliRunner().invoke(
        main,
        ["upload", str(f), "--def", str(def_json), "--port", "1"],
    )
    assert result.exit_code != 0
    assert "could not reach" in result.output


async def test_upload_without_receipt_contract_is_honest(tmp_path):
    """A vehicle with no FILE_RECEIPT packet still stores the file, and the
    verb says the transfer is unconfirmed instead of claiming success."""
    from xtce_sim.fileservice import FileService, FileStore

    simdef = SimDefinition.from_xtce(
        [DATA / "my_vehicle/my_vehicle_commands.xml", Path(TLM)]
    )
    def_json = tmp_path / "cmd_tlm.json"
    def_json.write_text(format_json(simdef))
    service = FileService(FileStore(tmp_path / "files"), simdef)
    server = SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=10.0, file_service=service
    )
    await server.start()
    try:
        f = tmp_path / "f.ats"
        f.write_bytes(b"content")
        result = await asyncio.to_thread(
            CliRunner().invoke,
            main,
            ["upload", str(f), "--def", str(def_json), "--port", str(server.bound_port)],
        )
        assert result.exit_code == 0, result.output
        assert "not confirmed" in result.output
        # The file still landed; only the confirmation channel is missing.
        for _ in range(100):
            if (tmp_path / "files/f.ats").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "files/f.ats").read_bytes() == b"content"
    finally:
        await server.stop()


def test_display_value_renders_string_fields_as_text():
    """monitor shows a string field as text (--raw keeps the wire bytes)."""
    from xtce_sim.definition import FieldInfo

    field = FieldInfo("FR_FILENAME", 256, "string")
    assert cli._display_value(field, b"plan.ats\x00\x00") == "plan.ats"
    assert cli._display_value(field, b"plan.ats\x00\x00", raw=True) == b"plan.ats\x00\x00"
