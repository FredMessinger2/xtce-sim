"""The sequence service and its live integration: LOAD reads the vehicle's
own file store, fired commands travel the normal dispatch path, and the two
status packets are sequencer-written, event-driven telemetry."""

import asyncio
import dataclasses
import time
from pathlib import Path

import pytest

from xtce_sim import ccsds, codec
from xtce_sim.definition import SimDefinition
from xtce_sim.fileservice import FileStore
from xtce_sim.seqservice import SequenceCommandError, SequenceService
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
    # but inexpressible. The packet must survive (codec saturates the int
    # at pack time — liberal downlink), not vanish.
    store.write("far.ats", b"2126-01-01T00:00:00Z IMAGER_ON\n")
    service.handle_command("LOAD_ATS", _filename_arg("far.ats"))
    packet = simdef.packet_by_name("ATS_STATUS")
    payload = codec.pack_telemetry(packet, service.values_for(packet))
    unpacked = codec.unpack_telemetry(packet, payload)
    assert unpacked["ATS_NEXT_CMD_TIME"] == 0xFFFFFFFF


def test_a_lean_enumeration_skips_the_field_instead_of_raising(service, store, simdef):
    # A vehicle may declare CmdResultType without every label the sequencer
    # emits; the promise is a warning and a skipped field, not a dead
    # status packet (a raise here used to kill the server's waiter task).
    packet = simdef.packet_by_name("ATS_STATUS")
    lean_fields = [
        dataclasses.replace(
            f, enumerations={k: v for k, v in f.enumerations.items() if k != "PENDING"}
        )
        if f.name == "ATS_LAST_CMD_RESULT"
        else f
        for f in packet.fields
    ]
    lean = dataclasses.replace(packet, fields=lean_fields)
    values = service.values_for(lean)  # slot is IDLE: last_cmd_result is PENDING
    assert "ATS_LAST_CMD_RESULT" not in values
    assert values["ATS_STATE"] == 0  # every other field still maps
    codec.pack_telemetry(lean, values)  # and the packet still packs


def test_seqid_other_than_1_is_refused_by_the_service(service, store):
    # Defense in depth: the example ICD's ValidRange rejects SeqId=2 before
    # dispatch, but the single-slot rule is the simulator's own contract —
    # a vehicle whose XTCE forgot the range must still be refused here.
    store.write("plan.rts", b"+0 IMAGER_ON\n")
    service.handle_command("LOAD_RTS", _filename_arg("plan.rts"))
    with pytest.raises(SequenceCommandError, match="single ATS slot"):
        service.handle_command("START_RTS", {"SeqId": 3})
    assert service.sequencer.status("rts", T0)["state"] == "LOADED"  # untouched


def test_start_seqid_declares_valid_range_1_to_1(simdef):
    # The single-slot rule is also the ICD's: ValidRange 1..1 on SeqId means
    # the vehicle's own range validation rejects any other id before dispatch.
    for name in ("START_ATS", "START_RTS"):
        seq_id = next(p for p in simdef.command_by_name(name).params if p.name == "SeqId")
        assert (seq_id.valid_min, seq_id.valid_max) == (1, 1)


def test_enum_typed_seqid_is_judged_by_wire_value(store, simdef):
    # A vehicle may type SeqId as an enumeration; decode_command then hands
    # the service a LABEL. The single-slot rule judges the wire value, so
    # a label meaning 1 passes and a label meaning 2 refuses.
    start = simdef.command_by_name("START_RTS")
    enum_params = [
        dataclasses.replace(p, enumerations={"SLOT_1": 1, "SLOT_2": 2})
        if p.name == "SeqId"
        else p
        for p in start.params
    ]
    enum_def = dataclasses.replace(simdef, commands=[
        dataclasses.replace(start, params=enum_params) if c.name == "START_RTS" else c
        for c in simdef.commands
    ])
    service = SequenceService(store, enum_def, clock=lambda: T0)
    store.write("plan.rts", b"+0 IMAGER_ON\n")
    service.handle_command("LOAD_RTS", _filename_arg("plan.rts"))
    assert "started" in service.handle_command("START_RTS", {"SeqId": "SLOT_1"})
    service.handle_command("STOP_RTS", {})
    with pytest.raises(SequenceCommandError, match="single"):
        service.handle_command("START_RTS", {"SeqId": "SLOT_2"})


def test_a_lean_state_enumeration_is_reported_at_construction(store, simdef, caplog):
    # A SeqStateType without ERROR cannot downlink the one state a failed
    # LOAD reaches; the wire would read 0 = IDLE. The ICD author hears it
    # at construction, not from a runtime log after the first failure.
    packet = simdef.packet_by_name("ATS_STATUS")
    lean_fields = [
        dataclasses.replace(
            f, enumerations={k: v for k, v in f.enumerations.items() if k != "ERROR"}
        )
        if f.name == "ATS_STATE"
        else f
        for f in packet.fields
    ]
    lean_def = dataclasses.replace(simdef, packets=[
        dataclasses.replace(packet, fields=lean_fields) if p.name == "ATS_STATUS" else p
        for p in simdef.packets
    ])
    with caplog.at_level("WARNING"):
        SequenceService(store, lean_def, clock=lambda: T0)
    assert "missing label(s) ERROR" in caplog.text


def test_a_bare_integer_state_field_warns_and_skips_instead_of_dying(store, simdef, caplog):
    # The worst configuration: ATS_STATE declared as a plain uint8 with no
    # enumeration. No state is expressible — construction says so, and at
    # emission the field is skipped (the packet must still pack) rather
    # than utf-8 bytes reaching struct.pack and killing every downlink.
    packet = simdef.packet_by_name("ATS_STATUS")
    bare_fields = [
        dataclasses.replace(f, enumerations=None) if f.name == "ATS_STATE" else f
        for f in packet.fields
    ]
    bare_def = dataclasses.replace(simdef, packets=[
        dataclasses.replace(packet, fields=bare_fields) if p.name == "ATS_STATUS" else p
        for p in simdef.packets
    ])
    with caplog.at_level("WARNING"):
        service = SequenceService(store, bare_def, clock=lambda: T0)
    assert "not an enumeration" in caplog.text
    bare_packet = service.simdef.packet_by_name("ATS_STATUS")
    values = service.values_for(bare_packet)
    assert "ATS_STATE" not in values
    codec.pack_telemetry(bare_packet, values)  # still packs


def test_wrong_kind_refusal_is_case_insensitive(service, store):
    # Uppercase names are common flight-file convention; 'PLAN.RTS' must
    # get the crisp kind-mismatch refusal, not a confusing parse error.
    store.write("PLAN.RTS", b"+0 IMAGER_ON\n")
    args = _filename_arg("PLAN.RTS")
    with pytest.raises(SequenceCommandError, match="RTS plan"):
        service.handle_command("LOAD_ATS", args)


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


def test_a_misnamed_status_field_is_reported_at_construction(store, simdef, caplog):
    # A typo'd field name would downlink a plausible constant zero forever;
    # the mismatch must be said out loud where the ICD author will see it.
    packet = simdef.packet_by_name("ATS_STATUS")
    renamed = [
        dataclasses.replace(f, name="ATS_CMDS_EXECUTED")
        if f.name == "ATS_CMD_EXECUTED"
        else f
        for f in packet.fields
    ]
    typo_def = dataclasses.replace(simdef, packets=[
        dataclasses.replace(packet, fields=renamed) if p.name == "ATS_STATUS" else p
        for p in simdef.packets
    ])
    with caplog.at_level("WARNING"):
        SequenceService(store, typo_def, clock=lambda: T0)
    assert "ATS_CMDS_EXECUTED" in caplog.text
    assert "match no sequencer status key" in caplog.text


# ---------------------------------------------------------------------------
# Integration: the server's waiter, executor, and event-driven status
#
# beacon_interval is 30 s in every test below, so any telemetry that arrives
# within the assertions' window is event-driven — the beacon cannot be the
# sender.


def _command_packet(simdef, name: str, args: dict | None = None, *, validate=True) -> bytes:
    command = simdef.command_by_name(name)
    payload = codec.encode_command(command, args, validate=validate)
    return ccsds.build_command_packet(command.opcode, payload)


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
