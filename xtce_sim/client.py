"""
Blocking TCP client helpers for talking to a running simulator.

`send_command` fires a single CCSDS command frame; `stream_packets` yields
telemetry packets from a live connection; `upload_file` moves a file to the
vehicle's store over the chunked file uplink and waits for its receipt. All
share the wire framing in `xtce_sim.ccsds`, so anything the sim serves,
these read — and vice versa.
"""

from __future__ import annotations

import socket
import time
import zlib
from typing import Iterator, Optional

from xtce_sim import ccsds, codec
from xtce_sim.definition import CommandDef, PacketDef, SimDefinition
from xtce_sim.fileservice import name_problem as _name_problem


def send_command(
    host: str,
    port: int,
    command: CommandDef,
    args: Optional[dict] = None,
    *,
    apid: int = 1,
    validate: bool = True,
) -> bytes:
    """Connect, send one command frame, and disconnect. Returns the CCSDS packet.

    ``validate=False`` skips the ground-side ValidRange check and transmits
    anyway — for testing the vehicle's own argument guards.
    """
    payload = codec.encode_command(command, args, validate=validate)
    packet = (
        ccsds.CCSDSHeader(packet_type=int(ccsds.PacketType.COMMAND), apid=apid).pack()
        + bytes([command.opcode])
        + payload
    )
    with socket.create_connection((host, port)) as sock:
        sock.sendall(ccsds.frame(packet))
    return packet


def stream_packets(
    host: str, port: int, *, timeout: Optional[float] = None
) -> Iterator[bytes]:
    """Yield CCSDS packets (CRC-stripped) from a live server until it closes."""
    sock = socket.create_connection((host, port))
    if timeout is not None:
        sock.settimeout(timeout)
    buffer = b""
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            packets, buffer = ccsds.deframe(buffer + data)
            yield from packets
    finally:
        sock.close()


#: Default file-uplink chunk size. Small enough to demonstrate real chunking
#: on real files, far under the wire frame's ceiling (ccsds.FILE_CHUNK_MAX).
UPLOAD_CHUNK_SIZE = 4096


class UploadError(Exception):
    """The vehicle refused the upload, or never confirmed it."""


def _confirmable(receipt_def: Optional[PacketDef]) -> bool:
    """Whether this packet can carry a verdict the client can read: the
    matcher correlates on every one of these fields, so a packet missing any
    of them gets the honest "unconfirmed" answer, not a guess (and not a
    guaranteed timeout from receipts that can never match)."""
    if receipt_def is None:
        return False
    names = {f.name for f in receipt_def.fields}
    return {
        "FR_FILENAME",
        "FR_FILE_SIZE",
        "FR_CHECKSUM",
        "FR_TRANSFER_STATUS",
        "FR_FILE_RECEIVED_COUNT",
    } <= names


def upload_file(
    host: str,
    port: int,
    filename: str,
    data: bytes,
    *,
    simdef: Optional[SimDefinition] = None,
    chunk_size: int = UPLOAD_CHUNK_SIZE,
    timeout: float = 10.0,
) -> Optional[dict]:
    """Send ``data`` to the vehicle's file store as ``filename`` and wait for
    the vehicle's verdict.

    The transfer is START / DATA... / FINISH frames on the reserved file
    uplink APID (see ccsds.py), all on one connection. With a ``simdef``
    whose FILE_RECEIPT packet carries the correlation fields, this then
    watches the downlink on that same connection for THIS transfer's
    receipt: a SUCCESS receipt is returned as its decoded field dict, a
    FAILED one raises ``UploadError``, and silence past ``timeout`` seconds
    raises too — an unconfirmed upload must not read as success. Without a
    usable receipt contract the transfer is fire-and-forget and returns None.

    A name the vehicle's store would refuse raises before anything is sent —
    both ends check, like the codec's range validation. Note the frames all
    go out before the receipt watch begins; on a link where sending itself
    takes longer than the beacon fills the return buffers, the vehicle could
    drop this client as unresponsive mid-send. Unreachable at this
    simulator's localhost scale, but worth knowing on a slower transport.
    """
    if not 1 <= chunk_size <= ccsds.FILE_CHUNK_MAX:
        raise ValueError(f"chunk_size must be in 1..{ccsds.FILE_CHUNK_MAX}, got {chunk_size}")
    problem = _name_problem(filename)
    if problem is not None:
        raise UploadError(f"{filename!r}: {problem}")
    receipt_def = simdef.packet_by_name("FILE_RECEIPT") if simdef else None
    crc = zlib.crc32(data) & 0xFFFFFFFF

    frames = [ccsds.frame(ccsds.build_file_start(filename, len(data), crc, seq_count=0))]
    for seq, offset in enumerate(range(0, len(data), chunk_size), start=1):
        frames.append(
            ccsds.frame(
                ccsds.build_file_data(offset, data[offset : offset + chunk_size], seq_count=seq)
            )
        )
    frames.append(ccsds.frame(ccsds.build_file_finish(seq_count=len(frames))))

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(b"".join(frames))
        if not _confirmable(receipt_def):
            return None
        return _await_receipt(sock, receipt_def, filename, len(data), crc, timeout)


class _ReceiptMatcher:
    """Correlates downlinked FILE_RECEIPTs to ONE transfer.

    Receipts are broadcast to every client (the console must see them too),
    and FILE_LIST / FILE_DELETE answer with receipts that can carry this same
    filename — so the name alone is not identity. A transfer's receipts all
    echo the declared (size, CRC) pair from START, and only a *landed* file
    bumps FR_FILE_RECEIVED_COUNT; the matcher therefore requires the full
    triple, and accepts SUCCESS only once the count has advanced past the
    value observed on this transfer's own IN_PROGRESS receipt (a FILE_LIST
    of an identical, already-stored copy repeats the triple but not the
    bump). A FAILED receipt bearing the triple is taken as the refusal; for
    a second ground uploading the identical file at the same moment, that
    errs toward reporting failure rather than success. One corner is weaker:
    a ZERO-BYTE upload declares (name, 0, 0), the same shape FILE_DELETE
    receipts carry, so a concurrent delete of that name can be misread as
    this transfer's verdict. Real flight protocols close this with a
    transaction id (CFDP); this receipt contract has no such field, so the
    residue is documented instead — it needs an empty file, a same-name
    delete from another ground, and (for a false SUCCESS) an interleaved
    landing plus our own landing then failing, all in one receipt window.
    """

    def __init__(self, receipt_def: PacketDef, filename: str, size: int, crc: int):
        self._receipt_def = receipt_def
        self._wanted = (filename.encode("utf-8"), size, crc)
        enums = next(
            f for f in receipt_def.fields if f.name == "FR_TRANSFER_STATUS"
        ).enumerations or {}
        self._in_progress = enums.get("IN_PROGRESS", 2)
        self._success = enums.get("SUCCESS", 0)
        self._baseline: Optional[int] = None  # count seen on our IN_PROGRESS

    def _ours(self, packet: bytes) -> Optional[dict]:
        """Decode ``packet`` if it is a receipt echoing our declared triple."""
        if len(packet) < 6:
            return None
        if ccsds.CCSDSHeader.unpack(packet[:6]).apid != self._receipt_def.apid:
            return None
        try:
            values = codec.unpack_telemetry(self._receipt_def, packet[6:])
        except Exception:
            return None  # a runt receipt is the server's bug, not a verdict
        triple = (
            bytes(values.get("FR_FILENAME", b"")).split(b"\x00", 1)[0],
            values.get("FR_FILE_SIZE"),
            values.get("FR_CHECKSUM"),
        )
        return values if triple == self._wanted else None

    def verdict(self, packets: list[bytes]) -> Optional[dict]:
        """The terminal receipt for our transfer among ``packets``, if any."""
        for packet in packets:
            values = self._ours(packet)
            if values is None:
                continue
            status = values.get("FR_TRANSFER_STATUS")
            count = values.get("FR_FILE_RECEIVED_COUNT")
            if status == self._in_progress:
                self._baseline = count
            elif status != self._success:
                return values  # FAILED (or an unknown status): the refusal
            elif self._baseline is not None and count > self._baseline:
                return values
        return None

    def is_success(self, values: dict) -> bool:
        return values.get("FR_TRANSFER_STATUS") == self._success


def _await_receipt(
    sock, receipt_def: PacketDef, filename: str, size: int, crc: int, timeout: float
) -> dict:
    """Watch the downlink until this transfer's terminal receipt arrives."""
    matcher = _ReceiptMatcher(receipt_def, filename, size, crc)
    deadline = time.monotonic() + timeout

    buffer = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UploadError(
                f"no receipt for {filename!r} within {timeout:g} s — "
                "the upload is not confirmed"
            )
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue  # the deadline check above delivers the honest error
        if not chunk:
            raise UploadError(f"link closed before a receipt for {filename!r} arrived")
        try:
            packets, buffer = ccsds.deframe(buffer + chunk)
        except ccsds.FrameError as exc:
            raise UploadError(f"corrupt downlink while awaiting the receipt: {exc}") from exc
        verdict = matcher.verdict(packets)
        if verdict is None:
            continue
        if not matcher.is_success(verdict):
            raise UploadError(f"vehicle refused {filename!r} — see the sim's log for why")
        return verdict
