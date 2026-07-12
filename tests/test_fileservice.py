"""The onboard file service: store jail, quota, uplink reassembly, commands.

Everything here runs without a network: the service takes uplink packets and
returns the receipts it would downlink, exactly as the server feeds it.
"""

import zlib
from pathlib import Path

import pytest

from xtce_sim import ccsds, codec, fileservice
from xtce_sim.definition import SimDefinition
from xtce_sim.fileservice import FileService, FileStore, name_problem

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")


@pytest.fixture()
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "files")


@pytest.fixture()
def service(store, simdef) -> FileService:
    return FileService(store, simdef, clock=lambda: 1_700_000_000.0)


def _upload(service, source, name, data, *, declared_size=None, declared_crc=None):
    """Feed a complete START/DATA/FINISH transfer; returns all receipts."""
    size = len(data) if declared_size is None else declared_size
    crc = zlib.crc32(data) & 0xFFFFFFFF if declared_crc is None else declared_crc
    receipts = service.handle_uplink(source, ccsds.build_file_start(name, size, crc))
    if data:
        receipts += service.handle_uplink(source, ccsds.build_file_data(0, data))
    receipts += service.handle_uplink(source, ccsds.build_file_finish())
    return receipts


def _statuses(service, receipts) -> list[str]:
    """Receipt statuses as labels, decoded the way the ground would."""
    by_value = {v: k for k, v in service._status_values.items()}
    return [by_value[r["FR_TRANSFER_STATUS"]] for r in receipts]


# ------------------------------------------------------------------ names ----


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a/b",
        "a\\b",
        "a\x00b",
        "a\nb",
        ".",
        "..",
        "x" * 33,
        "é" * 17,  # 34 bytes UTF-8: the limit counts bytes, not characters
        "upload.part",
    ],
)
def test_name_problem_rejects(bad):
    assert name_problem(bad) is not None


@pytest.mark.parametrize("good", ["plan.ats", "x" * 32, "loop.rts", "café.ats"])
def test_name_problem_accepts(good):
    assert name_problem(good) is None


# ------------------------------------------------------------------ store ----


def test_store_write_list_delete(store):
    store.write("b.ats", b"BBBB")
    store.write("a.ats", b"AA")
    assert store.names() == ["a.ats", "b.ats"]  # sorted
    assert store.size("a.ats") == 2
    assert store.crc("b.ats") == zlib.crc32(b"BBBB") & 0xFFFFFFFF
    assert store.used() == 6
    assert store.available() == store.quota - 6
    assert store.delete("a.ats") == 2
    assert store.names() == ["b.ats"]
    with pytest.raises(FileNotFoundError):
        store.delete("a.ats")


def test_store_overwrite_replaces(store):
    store.write("f", b"old content")
    store.write("f", b"new")
    assert store.size("f") == 3
    assert store.used() == 3


def test_store_jail_refuses_bad_names(store):
    for op in (store.size, store.crc, store.delete):
        with pytest.raises(ValueError):
            op("../escape")
    with pytest.raises(ValueError):
        store.write("../escape", b"x")


def test_store_part_files_are_invisible(store):
    (store.root / "ghost.part").write_bytes(b"leftover from a crashed write")
    assert store.names() == []
    assert store.used() == 0


def test_store_quota_and_replacement_credit(tmp_path):
    store = FileStore(tmp_path / "files", quota=100)
    store.write("a", b"x" * 60)
    assert not store.room_for("b", 50)  # only 40 left
    assert store.room_for("a", 90)  # replacing a frees its 60
    assert store.room_for("b", 40)


def test_store_quota_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        FileStore(tmp_path / "files", quota=0)


# ----------------------------------------------------------------- uplink ----


def test_upload_lands_file_with_honest_receipts(service, store):
    data = b"2026-07-12T00:00:00Z NOOP\n"
    receipts = _upload(service, "conn-1", "plan.ats", data)
    assert _statuses(service, receipts) == ["IN_PROGRESS", "SUCCESS"]
    final = receipts[-1]
    assert final["FR_FILENAME"] == b"plan.ats"
    assert final["FR_FILE_SIZE"] == len(data)
    assert final["FR_CHECKSUM"] == zlib.crc32(data) & 0xFFFFFFFF
    assert final["FR_TIMESTAMP"] == 1_700_000_000
    assert final["FR_FILE_RECEIVED_COUNT"] == 1
    assert final["FR_STORAGE_USED"] == len(data)
    assert final["FR_STORAGE_AVAILABLE"] == store.quota - len(data)
    assert (store.root / "plan.ats").read_bytes() == data


def test_receipts_pack_through_the_real_codec(service, simdef):
    """Every receipt must pack into the declared FILE_RECEIPT packet — field
    names, byte strings, and enum wire values all line up with the XTCE."""
    receipts = _upload(service, "c", "plan.ats", b"hello")
    packet_def = simdef.packet_by_name("FILE_RECEIPT")
    for values in receipts:
        payload = codec.pack_telemetry(packet_def, values)
        decoded = codec.unpack_telemetry(packet_def, payload)
        assert decoded["FR_FILENAME"].rstrip(b"\x00") == values["FR_FILENAME"]
        assert decoded["FR_TRANSFER_STATUS"] == values["FR_TRANSFER_STATUS"]


def test_status_values_match_the_xtce_enumeration(service, simdef):
    packet_def = simdef.packet_by_name("FILE_RECEIPT")
    status = next(f for f in packet_def.fields if f.name == "FR_TRANSFER_STATUS")
    assert service._status_values == status.enumerations


def test_multi_chunk_upload(service, store):
    data = bytes(range(256)) * 10
    crc = zlib.crc32(data) & 0xFFFFFFFF
    service.handle_uplink("c", ccsds.build_file_start("big.bin", len(data), crc))
    for offset in range(0, len(data), 100):
        out = service.handle_uplink("c", ccsds.build_file_data(offset, data[offset : offset + 100]))
        assert out == []  # data chunks are silent
    receipts = service.handle_uplink("c", ccsds.build_file_finish())
    assert _statuses(service, receipts) == ["SUCCESS"]
    assert (store.root / "big.bin").read_bytes() == data


def test_empty_file_upload(service, store):
    receipts = _upload(service, "c", "empty.ats", b"")
    assert _statuses(service, receipts) == ["IN_PROGRESS", "SUCCESS"]
    assert store.size("empty.ats") == 0


def test_offset_mismatch_fails_transfer(service, store):
    service.handle_uplink("c", ccsds.build_file_start("f", 10, 0))
    receipts = service.handle_uplink("c", ccsds.build_file_data(5, b"xxxxx"))
    assert _statuses(service, receipts) == ["FAILED"]
    # The transfer is gone: FINISH now has nothing to finish.
    assert service.handle_uplink("c", ccsds.build_file_finish()) == []
    assert store.names() == []


def test_overflow_beyond_declared_size_fails(service):
    service.handle_uplink("c", ccsds.build_file_start("f", 4, 0))
    receipts = service.handle_uplink("c", ccsds.build_file_data(0, b"xxxxx"))
    assert _statuses(service, receipts) == ["FAILED"]


def test_short_transfer_fails_at_finish(service, store):
    receipts = _upload(service, "c", "f", b"xx", declared_size=10)
    assert _statuses(service, receipts) == ["IN_PROGRESS", "FAILED"]
    assert store.names() == []


def test_crc_mismatch_fails_and_stores_nothing(service, store):
    receipts = _upload(service, "c", "f", b"payload", declared_crc=0xDEADBEEF)
    assert _statuses(service, receipts) == ["IN_PROGRESS", "FAILED"]
    assert store.names() == []


def test_bad_filename_refused_at_start(service, store):
    receipts = service.handle_uplink("c", ccsds.build_file_start("a/b", 1, 0))
    assert _statuses(service, receipts) == ["FAILED"]
    assert service.handle_uplink("c", ccsds.build_file_finish()) == []
    assert store.names() == []


def test_too_long_filename_receipt_is_truncated(service):
    long_name = "n" * 40
    receipts = service.handle_uplink("c", ccsds.build_file_start(long_name, 1, 0))
    assert _statuses(service, receipts) == ["FAILED"]
    assert len(receipts[0]["FR_FILENAME"]) <= fileservice.MAX_NAME_BYTES


def test_quota_refused_at_start(tmp_path, simdef):
    service = FileService(FileStore(tmp_path / "files", quota=10), simdef)
    receipts = service.handle_uplink("c", ccsds.build_file_start("big", 11, 0))
    assert _statuses(service, receipts) == ["FAILED"]


def test_quota_rechecked_at_landing(tmp_path, simdef):
    """Two connections race for the same room; the second to land is refused."""
    service = FileService(FileStore(tmp_path / "files", quota=10), simdef)
    a, b = b"a" * 8, b"b" * 8
    service.handle_uplink("A", ccsds.build_file_start("a", 8, zlib.crc32(a)))
    service.handle_uplink("A", ccsds.build_file_data(0, a))
    receipts_b = _upload(service, "B", "b", b)
    assert _statuses(service, receipts_b) == ["IN_PROGRESS", "SUCCESS"]
    receipts_a = service.handle_uplink("A", ccsds.build_file_finish())
    assert _statuses(service, receipts_a) == ["FAILED"]


def test_replacement_upload_succeeds(service, store):
    _upload(service, "c", "f", b"version one")
    receipts = _upload(service, "c", "f", b"two")
    assert _statuses(service, receipts) == ["IN_PROGRESS", "SUCCESS"]
    assert receipts[-1]["FR_FILE_RECEIVED_COUNT"] == 2
    assert store.size("f") == 3


def test_new_start_supersedes_old_transfer(service):
    service.handle_uplink("c", ccsds.build_file_start("first", 10, 0))
    receipts = service.handle_uplink("c", ccsds.build_file_start("second", 2, zlib.crc32(b"ok")))
    assert _statuses(service, receipts) == ["FAILED", "IN_PROGRESS"]
    assert receipts[0]["FR_FILENAME"] == b"first"
    assert receipts[1]["FR_FILENAME"] == b"second"


def test_data_and_finish_without_start_are_ignored(service):
    assert service.handle_uplink("c", ccsds.build_file_data(0, b"x")) == []
    assert service.handle_uplink("c", ccsds.build_file_finish()) == []


def test_malformed_packet_aborts_transfer(service):
    service.handle_uplink("c", ccsds.build_file_start("f", 1, 0))
    garbage = ccsds.CCSDSHeader(apid=ccsds.FILE_UPLINK_APID).pack() + bytes([99])
    receipts = service.handle_uplink("c", garbage)
    assert _statuses(service, receipts) == ["FAILED"]


def test_malformed_packet_without_transfer_is_logged_only(service):
    garbage = ccsds.CCSDSHeader(apid=ccsds.FILE_UPLINK_APID).pack() + bytes([99])
    assert service.handle_uplink("c", garbage) == []


def test_connection_closed_fails_open_transfer(service, store):
    service.handle_uplink("c", ccsds.build_file_start("f", 4, 0))
    receipts = service.connection_closed("c")
    assert _statuses(service, receipts) == ["FAILED"]
    assert service.connection_closed("c") == []  # idempotent
    assert store.names() == []


def test_transfers_are_isolated_per_connection(service, store):
    da, db = b"from A", b"from B!"
    service.handle_uplink("A", ccsds.build_file_start("a", len(da), zlib.crc32(da)))
    service.handle_uplink("B", ccsds.build_file_start("b", len(db), zlib.crc32(db)))
    service.handle_uplink("B", ccsds.build_file_data(0, db))
    service.handle_uplink("A", ccsds.build_file_data(0, da))
    assert _statuses(service, service.handle_uplink("A", ccsds.build_file_finish())) == ["SUCCESS"]
    assert _statuses(service, service.handle_uplink("B", ccsds.build_file_finish())) == ["SUCCESS"]
    assert store.names() == ["a", "b"]


# --------------------------------------------------------------- commands ----


def test_handles_only_file_commands(service):
    assert service.handles("FILE_LIST")
    assert service.handles("FILE_DELETE")
    assert service.handles("FILE_STATUS")
    assert not service.handles("NOOP")
    with pytest.raises(ValueError):
        service.handle_command("NOOP", {})


def test_file_list_one_receipt_per_file(service, store):
    store.write("b.ats", b"BB")
    store.write("a.ats", b"A")
    receipts = service.handle_command("FILE_LIST", {})
    assert [r["FR_FILENAME"] for r in receipts] == [b"a.ats", b"b.ats"]
    assert [r["FR_FILE_SIZE"] for r in receipts] == [1, 2]
    assert receipts[0]["FR_CHECKSUM"] == zlib.crc32(b"A") & 0xFFFFFFFF


def test_file_list_empty_store_answers_with_status(service):
    receipts = service.handle_command("FILE_LIST", {})
    assert len(receipts) == 1
    assert receipts[0]["FR_FILENAME"] == b""
    assert _statuses(service, receipts) == ["SUCCESS"]


def test_file_list_survives_an_alien_file(service, store):
    """A hand-dropped name the jail refuses must not silence the listing."""
    (store.root / ("z" * 40)).write_bytes(b"dropped in by hand")
    store.write("ok.ats", b"fine")
    receipts = service.handle_command("FILE_LIST", {})
    assert len(receipts) == 2
    assert _statuses(service, receipts) == ["SUCCESS", "FAILED"]


def test_file_status_reports_storage_truth(service, store):
    store.write("f", b"12345")
    receipts = service.handle_command("FILE_STATUS", {})
    assert len(receipts) == 1
    assert receipts[0]["FR_FILENAME"] == b""
    assert receipts[0]["FR_STORAGE_USED"] == 5
    assert receipts[0]["FR_STORAGE_AVAILABLE"] == store.quota - 5


def test_file_delete_frees_the_file(service, store):
    store.write("dead.ats", b"xxxx")
    # The Filename argument arrives from the codec as NUL-padded bytes.
    receipts = service.handle_command("FILE_DELETE", {"Filename": b"dead.ats" + b"\x00" * 24})
    assert _statuses(service, receipts) == ["SUCCESS"]
    assert receipts[0]["FR_FILE_SIZE"] == 4  # bytes freed
    assert store.names() == []


def test_file_delete_missing_file_fails(service):
    receipts = service.handle_command("FILE_DELETE", {"Filename": b"ghost"})
    assert _statuses(service, receipts) == ["FAILED"]


def test_file_delete_refuses_traversal_and_missing_arg(service, store):
    store.write("safe", b"x")
    for args in ({"Filename": b"../escape"}, {}):
        receipts = service.handle_command("FILE_DELETE", args)
        assert _statuses(service, receipts) == ["FAILED"]
    assert store.names() == ["safe"]


# ------------------------------------------------------ degraded contracts ----


def test_vehicle_without_receipt_packet_still_stores(tmp_path):
    """my_vehicle declares no FILE_RECEIPT: uploads land, receipts are log-only."""
    simdef = SimDefinition.from_xtce(
        [
            EXAMPLES / "my_vehicle/my_vehicle_commands.xml",
            EXAMPLES / "my_vehicle/my_vehicle_telemetry.xml",
        ]
    )
    store = FileStore(tmp_path / "files")
    service = FileService(store, simdef)
    assert service.receipt_apid is None
    receipts = _upload(service, "c", "plan.ats", b"data")
    assert receipts == []
    assert store.names() == ["plan.ats"]
    assert service.handle_command("FILE_LIST", {}) == []


def test_beacon_values_show_storage_truth(service, store, simdef):
    store.write("f", b"1234")
    packet_def = simdef.packet_by_name("FILE_RECEIPT")
    values = service.beacon_values(packet_def)
    assert values["FR_FILENAME"] == b""
    assert values["FR_STORAGE_USED"] == 4
    assert values["FR_STORAGE_AVAILABLE"] == store.quota - 4
    # Other packets are not the file service's to write.
    other = next(p for p in simdef.packets if p.apid != packet_def.apid)
    assert service.beacon_values(other) == {}


def test_store_quota_bounded_by_the_receipt_fields(tmp_path):
    """Storage numbers downlink as uint32; a quota they cannot express is
    refused at construction rather than vanishing every receipt later."""
    with pytest.raises(ValueError):
        FileStore(tmp_path / "files", quota=2**32)
    FileStore(tmp_path / "files", quota=2**32 - 1)  # the boundary builds
