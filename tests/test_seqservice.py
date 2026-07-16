"""The sequence service and its live integration: LOAD reads the vehicle's
own file store, fired commands travel the normal dispatch path, and the two
status packets are sequencer-written, event-driven telemetry."""

import asyncio
import time
from pathlib import Path

import pytest

from xtce_sim import ccsds, codec
from xtce_sim.definition import SimDefinition
from xtce_sim.fileservice import FileStore
from xtce_sim.seqservice import SequenceCommandError, SequenceService, steady_view
from xtce_sim.server import SimServer

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"

# The unit tests below run against pretend time anchored here
# (2026-03-15T14:30:00Z); the integration tests at the bottom use the real
# clock, because they exercise the server's real waiter task.
T0 = 1773585000.0


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")


@pytest.fixture()
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "files")


@pytest.fixture()
def service(store, simdef) -> SequenceService:
    svc = SequenceService(store, simdef, clock=lambda: T0)

    async def executor(name, args):
        return True

    svc.bind_executor(executor)
    return svc


def _filename_arg(name: str) -> dict:
    """A Filename argument exactly as decode_command delivers it: NUL-padded
    bytes, the wire shape of the XTCE's 32-byte string."""
    return {"Filename": name.encode("utf-8").ljust(32, b"\x00")}


# ---------------------------------------------------------------------------
# Command claims


def test_service_claims_exactly_the_eight_sequence_commands(service):
    for name in (
        "LOAD_ATS",
        "START_ATS",
        "STOP_ATS",
        "ABORT_ATS",
        "LOAD_RTS",
        "START_RTS",
        "STOP_RTS",
        "ABORT_RTS",
    ):
        assert service.handles(name)
    assert not service.handles("FILE_LIST")
    assert not service.handles("NOOP")


# ---------------------------------------------------------------------------
# LOAD: the file comes out of the vehicle's own store


def test_load_reads_the_plan_from_the_store(service, store):
    store.write("plan.rts", b"+0 IMAGER_ON\n+5 IMAGER_OFF\n")
    msg = service.handle_command("LOAD_RTS", _filename_arg("plan.rts"))
    assert "loaded plan.rts (2 commands)" in msg
    status = service.sequencer.status("rts", T0)
    assert status["state"] == "LOADED"


def test_loaded_plan_survives_deletion_of_its_file(service, store):
    # The sequencer holds the PARSED plan: the store cannot swap a loaded
    # sequence's content from under it (the old implementation's worst habit).
    store.write("plan.rts", b"+0 IMAGER_ON\n")
    service.handle_command("LOAD_RTS", _filename_arg("plan.rts"))
    store.delete("plan.rts")
    assert service.sequencer.status("rts", T0)["state"] == "LOADED"
    assert service.handle_command("START_RTS", {"SeqId": 1})


def test_load_of_a_missing_file_lands_error_and_raises(service):
    args = _filename_arg("missing.ats")
    with pytest.raises(SequenceCommandError, match="upload it first"):
        service.handle_command("LOAD_ATS", args)
    status = service.sequencer.status("ats", T0)
    assert status["state"] == "ERROR"
    assert status["seq_name"] == "missing.ats"  # WHAT failed stays visible


def test_load_of_an_unparseable_plan_lands_error_with_the_line(service, store):
    store.write("bad.ats", b"2026-03-15T14:30:00Z NO_SUCH_COMMAND\n")
    args = _filename_arg("bad.ats")
    with pytest.raises(SequenceCommandError, match="NO_SUCH_COMMAND"):
        service.handle_command("LOAD_ATS", args)
    assert service.sequencer.status("ats", T0)["state"] == "ERROR"


def test_load_of_a_binary_file_lands_error(service, store):
    store.write("blob.ats", bytes(range(256)))
    args = _filename_arg("blob.ats")
    with pytest.raises(SequenceCommandError, match="not a text file"):
        service.handle_command("LOAD_ATS", args)
    assert service.sequencer.status("ats", T0)["state"] == "ERROR"


def test_load_refuses_a_plan_of_the_wrong_kind(service, store):
    store.write("plan.rts", b"+0 IMAGER_ON\n")
    args = _filename_arg("plan.rts")
    with pytest.raises(SequenceCommandError, match="RTS plan"):
        service.handle_command("LOAD_ATS", args)
    assert service.sequencer.status("ats", T0)["state"] == "ERROR"


def test_load_with_no_filename_lands_error(service):
    with pytest.raises(SequenceCommandError, match="no Filename"):
        service.handle_command("LOAD_ATS", {})
    assert service.sequencer.status("ats", T0)["seq_name"] == "(no filename)"


def test_bad_load_cannot_tear_down_a_running_plan(service, store):
    store.write("plan.rts", b"+0 IMAGER_ON\n+600 IMAGER_OFF\n")
    service.handle_command("LOAD_RTS", _filename_arg("plan.rts"))
    service.handle_command("START_RTS", {"SeqId": 1})
    args = _filename_arg("missing.rts")
    with pytest.raises(SequenceCommandError, match="RUNNING"):
        service.handle_command("LOAD_RTS", args)
    assert service.sequencer.status("rts", T0)["state"] == "RUNNING"


# ---------------------------------------------------------------------------
# START/STOP/ABORT route to the machine; refusals raise


def test_refused_start_raises(service):
    with pytest.raises(SequenceCommandError, match="IDLE"):
        service.handle_command("START_ATS", {"SeqId": 1})


def test_stop_and_abort_route_to_the_right_slot(service, store):
    store.write("plan.rts", b"+0 IMAGER_ON\n+600 IMAGER_OFF\n")
    service.handle_command("LOAD_RTS", _filename_arg("plan.rts"))
    service.handle_command("START_RTS", {"SeqId": 1})
    assert "remains loaded" in service.handle_command("STOP_RTS", {"SeqId": 1})
    assert "aborted" in service.handle_command("ABORT_RTS", {"SeqId": 1})
    assert service.sequencer.status("rts", T0)["state"] == "IDLE"


def test_unbound_executor_is_a_loud_error(store, simdef):
    service = SequenceService(store, simdef, clock=lambda: T0)
    store.write("plan.rts", b"+0 IMAGER_ON\n")
    service.handle_command("LOAD_RTS", _filename_arg("plan.rts"))
    service.handle_command("START_RTS", {"SeqId": 1})
    # The fire is recorded FAILED (the sequencer guards executor raises),
    # not silently dropped.
    fired = asyncio.run(service.tick(T0 + 1))
    assert [f.success for f in fired] == [False]


# ---------------------------------------------------------------------------
# Status telemetry: sequencer-written, packet-declared


def test_values_for_maps_the_packet_declared_fields(service, store, simdef):
    store.write("plan.ats", b"2026-03-15T14:31:00Z IMAGER_ON\n")
    service.handle_command("LOAD_ATS", _filename_arg("plan.ats"))
    packet = simdef.packet_by_name("ATS_STATUS")
    values = service.values_for(packet)
    assert values["ATS_SEQ_NAME"] == b"plan.ats"  # pack-ready bytes
    assert values["ATS_STATE"] == 1  # LOADED, as its wire value
    assert values["ATS_CMD_TOTAL"] == 1
    assert values["ATS_CMD_SKIPPED"] == 0  # the unit-2 obligation, downlinked
    assert values["ATS_NEXT_CMD_TIME"] == int(T0 + 60)
    assert values["ATS_TIMESTAMP"] == int(T0)  # stamped from the service clock
    # Every value must pack as-is; a str or a label here would raise.
    codec.pack_telemetry(packet, values)


def test_a_post_2106_deadline_saturates_instead_of_killing_the_packet(service, store, simdef):
    # NEXT_CMD_TIME is a uint32 epoch; a plan scheduled past 2106 is legal
    # but inexpressible. The packet must survive (saturated), not vanish.
    store.write("far.ats", b"2126-01-01T00:00:00Z IMAGER_ON\n")
    service.handle_command("LOAD_ATS", _filename_arg("far.ats"))
    packet = simdef.packet_by_name("ATS_STATUS")
    values = service.values_for(packet)
    assert values["ATS_NEXT_CMD_TIME"] == 0xFFFFFFFF
    codec.pack_telemetry(packet, values)  # packs without raising


def test_values_for_other_packets_is_empty(service, simdef):
    housekeeping = simdef.packet_by_name("HOUSEKEEPING")
    assert service.values_for(housekeeping) == {}


def test_status_apids_are_the_two_declared_packets(service, simdef):
    assert service.status_apids == {
        simdef.packet_by_name("ATS_STATUS").apid,
        simdef.packet_by_name("RTS_STATUS").apid,
    }


def test_vehicle_without_status_packets_is_log_only(store):
    my_vehicle = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    service = SequenceService(store, my_vehicle, clock=lambda: T0)
    assert service.status_apids == set()
    hk = my_vehicle.packets[0]
    assert service.values_for(hk) == {}


def test_steady_view_drops_the_self_moving_fields():
    values = {
        "RTS_STATE": 2,
        "RTS_CMD_EXECUTED": 3,
        "RTS_ELAPSED_SEC": 41,
        "RTS_TIMESTAMP": 1773585000,
    }
    assert steady_view(values) == {"RTS_STATE": 2, "RTS_CMD_EXECUTED": 3}


# ---------------------------------------------------------------------------
# Integration: the server's waiter, executor, and event-driven status
#
# beacon_interval is 30 s in every test below, so any telemetry that arrives
# within the assertions' window is event-driven — the beacon cannot be the
# sender.


def _command_packet(simdef, name: str, args: dict | None = None, *, validate=True) -> bytes:
    command = simdef.command_by_name(name)
    payload = codec.encode_command(command, args, validate=validate)
    return (
        ccsds.CCSDSHeader(packet_type=int(ccsds.PacketType.COMMAND), apid=1).pack()
        + bytes([command.opcode])
        + payload
    )


class _Downlink:
    """Collects every packet a live connection downlinks, sorted by kind."""

    def __init__(self, simdef):
        self.simdef = simdef
        self.echoes: list[tuple[int, int]] = []  # (echo status, inner opcode)
        self.status: dict[str, list[dict]] = {"ATS_STATUS": [], "RTS_STATUS": []}

    def feed(self, packet: bytes) -> None:
        header = ccsds.CCSDSHeader.unpack(packet[:6])
        if header.apid == ccsds.CMD_ECHO_APID:
            status, inner = ccsds.parse_command_echo(packet)
            opcode, _ = ccsds.parse_command_packet(inner)
            self.echoes.append((status, opcode))
            return
        packet_def = self.simdef.packet_by_apid(header.apid)
        if packet_def is not None and packet_def.name in self.status:
            self.status[packet_def.name].append(
                codec.unpack_telemetry(packet_def, packet[6:])
            )

    async def pump_until(self, reader, done, timeout: float = 5.0) -> None:
        """Feed frames until ``done(self)`` is true (assert-fails on timeout)."""
        deadline = time.monotonic() + timeout
        buffer = b""
        while not done(self):
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"timed out; echoes={self.echoes} "
                f"ats={len(self.status['ATS_STATUS'])} rts={len(self.status['RTS_STATUS'])}"
            )
            data = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            assert data, "server closed the connection"
            packets, buffer = ccsds.deframe(buffer + data)
            for p in packets:
                self.feed(p)


def _state_label(simdef, packet_name: str, value: int) -> str:
    packet = simdef.packet_by_name(packet_name)
    field = next(f for f in packet.fields if f.name.endswith("_STATE"))
    return next(k for k, v in field.enumerations.items() if v == value)


async def _serve(simdef, tmp_path):
    store = FileStore(tmp_path / "files")
    service = SequenceService(store, simdef)
    server = SimServer(
        simdef,
        host="127.0.0.1",
        port=0,
        beacon_interval=30.0,
        sequence_service=service,
    )
    await server.start()
    return server, store


async def test_e2e_load_start_fire_and_complete(simdef, tmp_path):
    """The whole payoff path: a stored plan LOADs, STARTs, fires its command
    through the normal dispatch (a real echo appears), and completes — all
    reported by event-driven RTS_STATUS packets, no beacon involved."""
    server, store = await _serve(simdef, tmp_path)
    try:
        store.write("plan.rts", b"+0 IMAGER_ON\n")
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        down = _Downlink(simdef)

        writer.write(ccsds.frame(_command_packet(simdef, "LOAD_RTS", {"Filename": "plan.rts"})))
        writer.write(ccsds.frame(_command_packet(simdef, "START_RTS", {"SeqId": 1})))
        await writer.drain()

        imager_on = simdef.command_by_name("IMAGER_ON").opcode
        await down.pump_until(
            reader,
            lambda d: (ccsds.ECHO_EXECUTED, imager_on) in d.echoes
            and any(s["RTS_CMD_EXECUTED"] == 1 for s in d.status["RTS_STATUS"]),
        )

        # The fired command's echo is indistinguishable from a ground send.
        assert (ccsds.ECHO_EXECUTED, imager_on) in down.echoes
        final = down.status["RTS_STATUS"][-1]
        assert _state_label(simdef, "RTS_STATUS", final["RTS_STATE"]) == "COMPLETE"
        assert final["RTS_CMD_EXECUTED"] == 1
        assert final["RTS_SEQ_NAME"].split(b"\x00")[0] == b"plan.rts"
        writer.close()
    finally:
        await server.stop()


async def test_e2e_failed_load_echoes_failed_and_shows_error(simdef, tmp_path):
    server, _ = await _serve(simdef, tmp_path)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        down = _Downlink(simdef)
        load_ats = simdef.command_by_name("LOAD_ATS").opcode

        writer.write(ccsds.frame(_command_packet(simdef, "LOAD_ATS", {"Filename": "ghost.ats"})))
        await writer.drain()
        await down.pump_until(
            reader,
            lambda d: (ccsds.ECHO_FAILED, load_ats) in d.echoes
            and any(
                _state_label(simdef, "ATS_STATUS", s["ATS_STATE"]) == "ERROR"
                for s in d.status["ATS_STATUS"]
            ),
        )
        errored = down.status["ATS_STATUS"][-1]
        assert errored["ATS_SEQ_NAME"].split(b"\x00")[0] == b"ghost.ats"
        writer.close()
    finally:
        await server.stop()


async def test_e2e_seqid_other_than_1_is_rejected_by_the_vehicle(simdef, tmp_path):
    """ValidRange 1..1 comes from the ICD: the ground override (validate=False)
    transmits SeqId=2 anyway, and the VEHICLE rejects it before any effect."""
    server, store = await _serve(simdef, tmp_path)
    try:
        store.write("plan.rts", b"+0 IMAGER_ON\n")
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        down = _Downlink(simdef)
        start_rts = simdef.command_by_name("START_RTS").opcode

        writer.write(ccsds.frame(_command_packet(simdef, "LOAD_RTS", {"Filename": "plan.rts"})))
        writer.write(
            ccsds.frame(_command_packet(simdef, "START_RTS", {"SeqId": 2}, validate=False))
        )
        await writer.drain()
        await down.pump_until(reader, lambda d: (ccsds.ECHO_REJECTED, start_rts) in d.echoes)

        # Rejected before reaching the sequencer: the slot never started.
        for status in down.status["RTS_STATUS"]:
            assert _state_label(simdef, "RTS_STATUS", status["RTS_STATE"]) != "RUNNING"
        writer.close()
    finally:
        await server.stop()


async def test_e2e_status_arrives_on_the_event_not_the_beacon(simdef, tmp_path):
    """A LOAD's status packet shows up immediately (beacon is 30 s away)."""
    server, store = await _serve(simdef, tmp_path)
    try:
        store.write("plan.ats", b"2126-01-01T00:00:00Z IMAGER_ON\n")
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        down = _Downlink(simdef)

        started = time.monotonic()
        writer.write(ccsds.frame(_command_packet(simdef, "LOAD_ATS", {"Filename": "plan.ats"})))
        await writer.drain()
        await down.pump_until(
            reader,
            lambda d: any(
                _state_label(simdef, "ATS_STATUS", s["ATS_STATE"]) == "LOADED"
                for s in d.status["ATS_STATUS"]
            ),
        )
        assert time.monotonic() - started < 5.0  # a beacon could not have sent it
        writer.close()
    finally:
        await server.stop()


async def test_e2e_far_future_ats_fires_nothing_and_the_waiter_stays_quiet(simdef, tmp_path):
    """A started ATS with a distant deadline just sleeps: no fires, no spin."""
    server, store = await _serve(simdef, tmp_path)
    try:
        store.write("plan.ats", b"2126-01-01T00:00:00Z IMAGER_ON\n")
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        down = _Downlink(simdef)
        imager_on = simdef.command_by_name("IMAGER_ON").opcode
        start_ats = simdef.command_by_name("START_ATS").opcode

        writer.write(ccsds.frame(_command_packet(simdef, "LOAD_ATS", {"Filename": "plan.ats"})))
        writer.write(ccsds.frame(_command_packet(simdef, "START_ATS", {"SeqId": 1})))
        await writer.drain()
        await down.pump_until(
            reader,
            lambda d: any(
                _state_label(simdef, "ATS_STATUS", s["ATS_STATE"]) == "RUNNING"
                for s in d.status["ATS_STATUS"]
            ),
        )
        assert (ccsds.ECHO_EXECUTED, start_ats) in down.echoes
        await asyncio.sleep(0.3)  # give a buggy waiter time to misfire
        assert (ccsds.ECHO_EXECUTED, imager_on) not in down.echoes
        writer.close()
    finally:
        await server.stop()
