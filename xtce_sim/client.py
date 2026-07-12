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
    that declares a FILE_RECEIPT packet, this then watches the downlink on
    that same connection for the receipt naming this file: a SUCCESS receipt
    is returned as its decoded field dict, a FAILED one raises
    ``UploadError``, and silence past ``timeout`` seconds raises too — an
    unconfirmed upload must not read as success. Without a receipt contract
    the transfer is fire-and-forget and returns None.
    """
    if not 1 <= chunk_size <= ccsds.FILE_CHUNK_MAX:
        raise ValueError(f"chunk_size must be in 1..{ccsds.FILE_CHUNK_MAX}, got {chunk_size}")
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
        if receipt_def is None:
            return None
        return _await_receipt(sock, receipt_def, filename, timeout)


def _await_receipt(sock, receipt_def: PacketDef, filename: str, timeout: float) -> dict:
    """Watch the downlink for this file's terminal receipt.

    The beacon's storage-status receipts carry an empty filename and event
    receipts are sent exactly once, so on a connection opened for this
    transfer, a receipt naming this file with a non-IN_PROGRESS status can
    only be the verdict on this upload.
    """
    status_field = next(
        (f for f in receipt_def.fields if f.name == "FR_TRANSFER_STATUS"), None
    )
    enums = status_field.enumerations if status_field is not None else {}
    in_progress = enums.get("IN_PROGRESS", 2)
    success = enums.get("SUCCESS", 0)
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
        packets, buffer = ccsds.deframe(buffer + chunk)
        verdict = _match_receipt(packets, receipt_def, filename, in_progress)
        if verdict is None:
            continue
        if verdict["FR_TRANSFER_STATUS"] != success:
            raise UploadError(f"vehicle refused {filename!r} — see the sim's log for why")
        return verdict


def _match_receipt(
    packets: list[bytes], receipt_def: PacketDef, filename: str, in_progress: int
) -> Optional[dict]:
    """The decoded terminal receipt for ``filename`` among ``packets``, if any."""
    wanted = filename.encode("utf-8")
    for packet in packets:
        if len(packet) < 6:
            continue
        if ccsds.CCSDSHeader.unpack(packet[:6]).apid != receipt_def.apid:
            continue
        try:
            values = codec.unpack_telemetry(receipt_def, packet[6:])
        except Exception:
            continue  # a runt receipt is the server's bug, not a verdict
        name = bytes(values.get("FR_FILENAME", b"")).split(b"\x00", 1)[0]
        if name == wanted and values.get("FR_TRANSFER_STATUS") != in_progress:
            return values
    return None
