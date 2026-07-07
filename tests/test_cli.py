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
CMD = str(EXAMPLES / "my_vehicle_commands.xml")
TLM = str(EXAMPLES / "my_vehicle_telemetry.xml")


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce([EXAMPLES / "my_vehicle_commands.xml", TLM])


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
        assert "not found" in result.output


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
    d = SimDefinition.from_xtce(EXAMPLES / "imaging_sat.xml")
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
        # A full cycle includes many of the 14 packets (not a partial frame).
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
