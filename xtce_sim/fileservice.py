"""
The onboard file service: a jailed per-instance store fed by chunked uplink.

The ground sends a file over the existing TCP command link as START / DATA /
FINISH packets on a reserved link-protocol APID (see ccsds.py); the vehicle
reassembles the chunks, verifies the declared size and CRC-32 against what
actually arrived, and only then lands the file in its storage directory —
``runs/<id>/files/`` — atomically. Every outcome is reported on the downlink
as a FILE_RECEIPT packet. A vehicle whose definition declares no
FILE_RECEIPT packet still stores files; its receipts are log-only.

Honesty rules, stated plainly:

- The vehicle does not trust the ground. Filenames are validated against the
  store's jail (no separators, no dot-names, 32 bytes at most — the receipt
  field's capacity), sizes against the storage quota, and content against the
  declared CRC. A transfer that violates any of these is refused with a
  FAILED receipt, never partially applied.
- A SUCCESS receipt carries measured numbers: the byte count that arrived
  and the CRC computed over it (verified equal to the declaration). A FAILED
  receipt echoes the *declared* size and CRC — they identify the transfer as
  commanded, which is what the ground correlates on — and the log line
  carries the measured values that refused it.
- A receipt with an empty FR_FILENAME is a *storage-status view* (the answer
  to FILE_STATUS), not a transfer event.
- FILE_RECEIPT is event telemetry: it downlinks when something happens and
  is never beaconed (the server skips it), so the last event stays on every
  console until the next one, exactly as a latched HK page would.
- FR_FILE_RECEIVED_COUNT counts files landed since this boot; storage used /
  available are measured from disk every time, so they stay true even across
  restarts of a persistent store.

The service owns no I/O loop and no sockets: the server feeds it uplink
packets and file-management commands, and every handler returns the list of
receipt value-dicts to downlink — the same injected-dependency shape the
sequencer uses, so the whole machine tests without a network.

Scale bounds, so nobody meets them as surprises: a transfer buffers its
declared size in memory (at most the quota, per uploading connection), and
CRC/write work runs synchronously on the server's event loop — bounded by
the quota to well under a beacon interval on local disks. Replacing a file
briefly holds old + new on disk (the atomic ``.part`` write), so peak disk
is the quota plus the largest replaced file. If stores ever outgrow these
numbers, move the CRC/write work to a thread and stream to disk.
"""

from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from xtce_sim import ccsds
from xtce_sim.definition import PacketDef, SimDefinition

logger = logging.getLogger(__name__)

#: Storage quota (bytes) when none is given: a small satellite's file volume.
DEFAULT_QUOTA = 100 * 1024 * 1024

#: Longest allowed filename, in UTF-8 bytes — the FILE_RECEIPT packet's
#: FR_FILENAME field holds 32 bytes, and a file the vehicle cannot report
#: honestly is a file it should not accept.
MAX_NAME_BYTES = 32

#: Commands the file service claims by name when the definition declares them.
FILE_COMMANDS = ("FILE_LIST", "FILE_DELETE", "FILE_STATUS")

#: Transfer status labels, pinned to the XTCE TransferStatusType values. The
#: bound packet's own enumeration is consulted first; these are the fallback
#: (and the wire truth) when a definition omits a label.
_STATUS_VALUES = {"SUCCESS": 0, "FAILED": 1, "IN_PROGRESS": 2}

#: Suffix of the atomic-write temporary; such names are excluded from
#: listings and refused as filenames so a crash mid-write can neither be
#: listed as a file nor be overwritten by one.
_PART_SUFFIX = ".part"


def event_only_apids(simdef: SimDefinition) -> set[int]:
    """APIDs that downlink on events rather than in the periodic beacon.

    The single source of truth for who treats FILE_RECEIPT as event
    telemetry: the server's beacon skips these, and ground-side health
    checks must not wait for them (nothing arrives while nothing happens).
    """
    receipt = simdef.packet_by_name("FILE_RECEIPT")
    return {receipt.apid} if receipt is not None else set()


def name_problem(name: str) -> Optional[str]:
    """Why ``name`` is not a legal store filename, or None if it is.

    Used on BOTH ends of the link, like the codec's range checks: the ground
    refuses to start a doomed upload, and the vehicle refuses one that
    arrives anyway.
    """
    if not name:
        return "filename is empty"
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        return f"filename exceeds {MAX_NAME_BYTES} bytes ({len(name.encode('utf-8'))})"
    bad = set("/\\\x00") & set(name)
    if bad or any(ord(c) < 0x20 for c in name):
        return "filename contains a path separator or control character"
    if name in (".", ".."):
        return f"filename {name!r} is a directory reference"
    if name.endswith(_PART_SUFFIX):
        return f"filename may not end in {_PART_SUFFIX!r} (reserved for atomic writes)"
    return None


class FileStore:
    """A directory jail holding one vehicle's uploaded files, under a quota.

    All accounting reads the disk at call time — the directory may persist
    across runs, and the numbers in a receipt must describe what is actually
    there, not what this process remembers putting there.
    """

    def __init__(self, root: Path, quota: int = DEFAULT_QUOTA) -> None:
        if not 0 < quota <= 0xFFFFFFFF:
            # The receipt contract downlinks storage numbers as uint32; a
            # quota those fields cannot express would make every receipt
            # unpackable and vanish them silently.
            raise ValueError(f"quota must be in 1..{0xFFFFFFFF} bytes, got {quota}")
        self.root = root
        self.quota = quota
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        """The jailed path for ``name``; raises on a name the jail refuses."""
        problem = name_problem(name)
        if problem is not None:
            raise ValueError(problem)
        return self.root / name

    def names(self) -> list[str]:
        """Sorted names of stored files (atomic-write leftovers excluded)."""
        return sorted(
            p.name for p in self.root.iterdir() if p.is_file() and not p.name.endswith(_PART_SUFFIX)
        )

    def size(self, name: str) -> int:
        return self._path(name).stat().st_size

    def crc(self, name: str) -> int:
        """CRC-32 of the file's current content, read from disk."""
        return zlib.crc32(self._path(name).read_bytes()) & 0xFFFFFFFF

    def used(self) -> int:
        """Bytes occupied by stored files, measured now."""
        return sum(
            p.stat().st_size
            for p in self.root.iterdir()
            if p.is_file() and not p.name.endswith(_PART_SUFFIX)
        )

    def available(self) -> int:
        return max(0, self.quota - self.used())

    def room_for(self, name: str, size: int) -> bool:
        """Whether ``size`` bytes fit, crediting the file being replaced."""
        existing = 0
        target = self._path(name)
        if target.is_file():
            existing = target.stat().st_size
        return size <= self.available() + existing

    def read(self, name: str) -> bytes:
        """The stored file's content. Raises FileNotFoundError if absent
        (and ValueError on a name the jail refuses, like every accessor)."""
        return self._path(name).read_bytes()

    def write(self, name: str, data: bytes) -> None:
        """Land ``data`` as ``name`` all-or-nothing: a crash mid-write must
        not leave a half file where a whole one is expected."""
        target = self._path(name)
        tmp = target.with_name(target.name + _PART_SUFFIX)
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def delete(self, name: str) -> int:
        """Remove ``name``; returns the bytes freed. Raises FileNotFoundError."""
        target = self._path(name)
        size = target.stat().st_size  # raises FileNotFoundError if absent
        target.unlink()
        return size


@dataclass
class _Transfer:
    """One in-progress upload on one connection."""

    filename: str
    size: int
    crc: int
    buffer: bytearray = field(default_factory=bytearray)


class FileService:
    """The vehicle's file-management brain: uplink reassembly + FILE_* commands.

    Bound to the definition's FILE_RECEIPT packet (by name) at construction;
    receipt field values are keyed by the FR_* names that packet declares.
    Every handler returns the receipt value-dicts to downlink — the caller
    (the server) owns the actual sending, and ``receipt_apid`` tells it where.
    The clock is injected for the same reason the sequencer's is: receipts
    carry timestamps, and tests must be able to hold time still.
    """

    def __init__(
        self,
        store: FileStore,
        simdef: SimDefinition,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.store = store
        self._clock = clock
        # Injected like SimServer's: `run` gives every module the instance's
        # own logger, so file events land in the sim window Fred watches.
        self._logger = logger or logging.getLogger(__name__)
        self._received_count = 0
        self._transfers: dict[object, _Transfer] = {}
        self._receipt: Optional[PacketDef] = simdef.packet_by_name("FILE_RECEIPT")
        if self._receipt is None:
            self._logger.warning(
                "definition declares no FILE_RECEIPT packet — files will be "
                "stored, but receipts are log-only"
            )
        self._status_values = dict(_STATUS_VALUES)
        if self._receipt is not None:
            self._bind_status_labels()

    def _bind_status_labels(self) -> None:
        """Prefer the packet's own FR_TRANSFER_STATUS enumeration values."""
        status = next((f for f in self._receipt.fields if f.name == "FR_TRANSFER_STATUS"), None)
        if status is None or not status.enumerations:
            self._logger.warning(
                "FILE_RECEIPT has no enumerated FR_TRANSFER_STATUS field — "
                "receipts will use the canonical status values %s",
                _STATUS_VALUES,
            )
            return
        for label, fallback in _STATUS_VALUES.items():
            if label in status.enumerations:
                self._status_values[label] = status.enumerations[label]
            else:
                self._logger.warning(
                    "FR_TRANSFER_STATUS declares no %r label — using value %d",
                    label,
                    fallback,
                )

    @property
    def receipt_apid(self) -> Optional[int]:
        """APID receipts downlink on, or None when the vehicle declares none."""
        return self._receipt.apid if self._receipt else None

    # -- receipts ------------------------------------------------------------------

    def _receipt_values(self, filename: str, size: int, crc: int, status: str) -> dict:
        """One receipt as pack-ready field values (bytes for the name, wire
        ints for the enum — pack_telemetry maps no labels)."""
        return {
            "FR_FILENAME": filename.encode("utf-8"),
            "FR_FILE_SIZE": size,
            "FR_CHECKSUM": crc,
            "FR_TIMESTAMP": int(self._clock()),
            "FR_TRANSFER_STATUS": self._status_values[status],
            "FR_FILE_RECEIVED_COUNT": self._received_count,
            "FR_STORAGE_USED": self.store.used(),
            "FR_STORAGE_AVAILABLE": self.store.available(),
        }

    def _event(self, filename: str, size: int, crc: int, status: str, why: str) -> list[dict]:
        """Log one file event and return its receipt (empty if log-only)."""
        log = self._logger.info if status != "FAILED" else self._logger.warning
        log("file %s: %s — %s", status, filename or "(storage status)", why)
        if self._receipt is None:
            return []
        return [self._receipt_values(filename, size, crc, status)]

    # -- uplink --------------------------------------------------------------------

    def handle_uplink(self, source: object, packet: bytes) -> list[dict]:
        """Feed one file-uplink packet from connection ``source``; returns
        receipts to downlink. ``source`` only needs to be hashable and stable
        per connection — transfers never cross connections."""
        try:
            chunk_type, fields = ccsds.parse_file_uplink(packet)
        except ValueError as exc:
            return self._abort(source, f"malformed uplink packet: {exc}")
        if chunk_type == ccsds.FILE_START:
            return self._on_start(source, fields)
        if chunk_type == ccsds.FILE_DATA:
            return self._on_data(source, fields)
        return self._on_finish(source)

    def _abort(self, source: object, why: str) -> list[dict]:
        """Discard ``source``'s transfer (if any) with a FAILED receipt."""
        transfer = self._transfers.pop(source, None)
        if transfer is None:
            self._logger.warning("file uplink: %s (no transfer in progress)", why)
            return []
        return self._event(transfer.filename, transfer.size, transfer.crc, "FAILED", why)

    def _on_start(self, source: object, fields: dict) -> list[dict]:
        receipts: list[dict] = []
        if source in self._transfers:
            receipts += self._abort(source, "superseded by a new START")
        name, size, crc = fields["filename"], fields["size"], fields["crc"]
        problem = name_problem(name)
        if problem is not None:
            # The refused name still gets a receipt, truncated to what the
            # field can carry — refusing silently would hide the event.
            reportable = name.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore")
            return receipts + self._event(reportable, size, crc, "FAILED", problem)
        if not self.store.room_for(name, size):
            return receipts + self._event(
                name,
                size,
                crc,
                "FAILED",
                f"{size} bytes exceed available storage ({self.store.available()})",
            )
        self._transfers[source] = _Transfer(filename=name, size=size, crc=crc)
        return receipts + self._event(
            name, size, crc, "IN_PROGRESS", f"transfer started ({size} bytes declared)"
        )

    def _on_data(self, source: object, fields: dict) -> list[dict]:
        transfer = self._transfers.get(source)
        if transfer is None:
            self._logger.warning("file uplink: DATA with no transfer in progress")
            return []
        offset, chunk = fields["offset"], fields["chunk"]
        if offset != len(transfer.buffer):
            return self._abort(source, f"chunk at offset {offset}, expected {len(transfer.buffer)}")
        if len(transfer.buffer) + len(chunk) > transfer.size:
            return self._abort(
                source,
                f"received {len(transfer.buffer) + len(chunk)} bytes, "
                f"START declared {transfer.size}",
            )
        transfer.buffer.extend(chunk)
        return []

    def _on_finish(self, source: object) -> list[dict]:
        transfer = self._transfers.pop(source, None)
        if transfer is None:
            self._logger.warning("file uplink: FINISH with no transfer in progress")
            return []
        received = len(transfer.buffer)
        if received != transfer.size:
            return self._event(
                transfer.filename,
                transfer.size,
                transfer.crc,
                "FAILED",
                f"received {received} bytes, START declared {transfer.size}",
            )
        actual_crc = zlib.crc32(bytes(transfer.buffer)) & 0xFFFFFFFF
        if actual_crc != transfer.crc:
            return self._event(
                transfer.filename,
                transfer.size,
                transfer.crc,
                "FAILED",
                f"CRC-32 mismatch: received 0x{actual_crc:08X}, declared 0x{transfer.crc:08X}",
            )
        # Re-checked at landing: another connection may have consumed the
        # room this transfer was promised at START.
        if not self.store.room_for(transfer.filename, transfer.size):
            return self._event(
                transfer.filename,
                transfer.size,
                transfer.crc,
                "FAILED",
                f"storage filled during transfer ({self.store.available()} available)",
            )
        try:
            self.store.write(transfer.filename, bytes(transfer.buffer))
        except OSError as exc:
            self._logger.exception("file store write failed: %s", transfer.filename)
            return self._event(
                transfer.filename, transfer.size, transfer.crc, "FAILED", f"write failed: {exc}"
            )
        self._received_count += 1
        return self._event(
            transfer.filename,
            transfer.size,
            actual_crc,
            "SUCCESS",
            f"landed ({received} bytes, CRC-32 0x{actual_crc:08X})",
        )

    def connection_closed(self, source: object) -> list[dict]:
        """The link dropped; a transfer it carried is over, and honestly so."""
        if source not in self._transfers:
            return []
        return self._abort(source, "link dropped mid-transfer")

    # -- commands ------------------------------------------------------------------

    def handles(self, command_name: str) -> bool:
        return command_name in FILE_COMMANDS

    def handle_command(self, command_name: str, args: dict) -> list[dict]:
        """Execute one file-management command; returns receipts to downlink."""
        if command_name == "FILE_LIST":
            return self._cmd_list()
        if command_name == "FILE_DELETE":
            return self._cmd_delete(args)
        if command_name == "FILE_STATUS":
            return self._cmd_status()
        raise ValueError(f"file service does not handle {command_name!r}")

    def _cmd_list(self) -> list[dict]:
        """One receipt per stored file (name, size, current CRC); an empty
        store answers with the storage-status view so LIST is never silence."""
        names = self.store.names()
        if not names:
            return self._cmd_status()
        receipts: list[dict] = []
        for name in names:
            # Guarded per file: the directory is real disk, so a file dropped
            # in by hand (a name the jail refuses) or deleted mid-listing
            # must not silence the rest of the answer.
            try:
                receipts += self._event(
                    name, self.store.size(name), self.store.crc(name), "SUCCESS", "listed"
                )
            except (OSError, ValueError) as exc:
                reportable = name.encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore")
                receipts += self._event(reportable, 0, 0, "FAILED", f"unreadable: {exc}")
        return receipts

    def _cmd_status(self) -> list[dict]:
        return self._event(
            "",
            0,
            0,
            "SUCCESS",
            f"{len(self.store.names())} file(s), {self.store.used()} bytes used, "
            f"{self.store.available()} available",
        )

    def _cmd_delete(self, args: dict) -> list[dict]:
        name = decode_filename_arg(args.get("Filename"))
        problem = name_problem(name) if name else "no Filename argument"
        if problem is not None:
            return self._event(name, 0, 0, "FAILED", f"delete refused: {problem}")
        try:
            freed = self.store.delete(name)
        except FileNotFoundError:
            return self._event(name, 0, 0, "FAILED", "delete refused: no such file")
        return self._event(name, freed, 0, "SUCCESS", f"deleted ({freed} bytes freed)")


def decode_filename_arg(value) -> str:
    """A Filename command argument as text: string args arrive from the codec
    as NUL-padded bytes; anything else is best-effort text. Shared with the
    sequence service, whose LOAD commands carry the same argument shape."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b"\x00", 1)[0].decode("utf-8", "replace")
    if value is None:
        return ""
    return str(value)
