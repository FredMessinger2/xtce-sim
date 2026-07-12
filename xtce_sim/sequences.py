"""
Sequence files: ATS (Absolute Time Sequence) and RTS (Relative Time Sequence).

An ATS file schedules commands at absolute UTC times, one per line; an RTS
file schedules them at offsets relative to whenever the sequence is started:

    # burn plan, rev 3                       # comment lines are ignored
    2026-03-15T14:30:00Z  ADCS_SET_MODE Mode=NADIR
    2026-03-15T14:31:30Z  IMAGER_ON          # trailing comments too

    +0    ADCS_SET_MODE Mode=SUNSAFE
    +30   HEATER_OFF HeaterId=1

Validation is strict and total, in the behavior engine's style: every
problem in the file is reported at once, and a file with any problem is
rejected whole — a sequence that half-loads is worse than one that does
not load. When a SimDefinition is supplied, every entry is additionally
encoded through the same machinery real uplink uses (`codec.encode_command`)
so an unknown command, a misspelled argument, a bad enum label, or an
out-of-range value is caught when the file is read, not when the line
comes due.

RTS entries store the DELAY, never an absolute time: re-basing against a
start time is the scheduler's job, done in memory at START. (The prior
implementation re-read the file from disk at START to re-base — a file
deleted or edited between LOAD and START silently swapped the sequence's
content or burst-fired all of it.)

Timestamps accept ISO-8601 with a ``Z`` suffix, an explicit UTC offset, or
no zone at all — a naked timestamp is taken as UTC, because sequence
planning in local time is how vehicles get commanded at the wrong hour.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

from xtce_sim import codec

#: File-extension → sequence kind. The kind decides how the time token on
#: each line is interpreted.
KINDS = {".ats": "ats", ".rts": "rts"}

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)?$")
_DURATION_SCALE = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


class SequenceError(ValueError):
    """A sequence file was rejected; ``problems`` lists every reason."""

    def __init__(self, name: str, problems: list[str]) -> None:
        self.name = name
        self.problems = list(problems)
        listing = "\n  - ".join(self.problems)
        super().__init__(f"{name}: {len(self.problems)} problem(s):\n  - {listing}")


@dataclass(frozen=True)
class SequenceEntry:
    """One command in a sequence.

    ``time`` is epoch seconds UTC for an ATS entry, and seconds after START
    for an RTS entry. ``args`` holds the raw ``KEY=VALUE`` strings from the
    file; coercion to wire types happens inside ``codec.encode_command``,
    exactly as it does for a command typed at the console.
    """

    time: float
    command: str
    args: dict[str, str] = field(default_factory=dict)
    line: int = 0  # 1-based source line, for status reporting


@dataclass(frozen=True)
class Sequence:
    kind: str  # "ats" or "rts"
    name: str
    entries: tuple[SequenceEntry, ...]  # sorted by time; file order breaks ties

    @property
    def span(self) -> float:
        """Seconds between the first and last entry."""
        return self.entries[-1].time - self.entries[0].time


def parse_ats(text: str, name: str, simdef=None) -> Sequence:
    """Parse ATS text; raise SequenceError listing every problem."""
    return _parse(text, name, "ats", simdef)


def parse_rts(text: str, name: str, simdef=None) -> Sequence:
    """Parse RTS text; raise SequenceError listing every problem."""
    return _parse(text, name, "rts", simdef)


def _parse(text: str, name: str, kind: str, simdef) -> Sequence:
    # Problems carry their line number so the final report reads in file
    # order, no matter which validation pass found each one.
    problems, entries = _scan_lines(text, kind)
    if not entries and not problems:
        problems.append((0, "no commands (a sequence must schedule at least one)"))
    if simdef is not None:
        problems += _definition_problems(entries, simdef)
    if problems:
        problems.sort(key=lambda p: p[0])
        raise SequenceError(name, [f"line {n}: {msg}" if n else msg for n, msg in problems])
    entries.sort(key=lambda e: e.time)  # stable: file order breaks ties
    return Sequence(kind=kind, name=name, entries=tuple(entries))


def _scan_lines(text: str, kind: str) -> tuple[list[tuple[int, str]], list[SequenceEntry]]:
    """Every syntactic problem and every well-formed entry, one pass.

    Lines are split on '\\n' only (never splitlines) so numbering always
    matches what the operator's editor shows.
    """
    problems: list[tuple[int, str]] = []
    entries: list[SequenceEntry] = []
    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        entry = _parse_line(line, lineno, kind, problems)
        if entry is not None:
            entries.append(entry)
    return problems, entries


def _definition_problems(entries: list[SequenceEntry], simdef) -> list[tuple[int, str]]:
    problems = []
    for entry in entries:
        problem = _entry_problem(entry, simdef)
        if problem is not None:
            problems.append((entry.line, problem))
    return problems


def _parse_line(
    line: str, lineno: int, kind: str, problems: list[tuple[int, str]]
) -> SequenceEntry | None:
    parts = line.split()
    if len(parts) < 2:
        expected = "<utc-time> <COMMAND> [KEY=VALUE ...]"
        if kind == "rts":
            expected = "+<seconds> <COMMAND> [KEY=VALUE ...]"
        problems.append((lineno, f"expected '{expected}', got {line!r}"))
        return None
    when = _parse_ats_time(parts[0]) if kind == "ats" else _parse_rts_delay(parts[0])
    if isinstance(when, str):
        problems.append((lineno, when))
        return None
    args: dict[str, str] = {}
    ok = True
    for token in parts[2:]:
        key, sep, value = token.partition("=")
        if not sep or not key:
            problems.append((lineno, f"expected KEY=VALUE, got {token!r}"))
            ok = False
        elif key in args:
            problems.append((lineno, f"duplicate argument {key!r}"))
            ok = False
        else:
            args[key] = value
    if not ok:
        return None
    return SequenceEntry(time=when, command=parts[1], args=args, line=lineno)


def _parse_ats_time(token: str) -> float | str:
    """Epoch seconds from an ISO-8601 token, or a problem description."""
    try:
        dt = datetime.fromisoformat(token)
    except ValueError:
        return f"invalid UTC timestamp {token!r} (want ISO-8601, e.g. 2026-03-15T14:30:00Z)"
    if "T" not in token and ":" not in token:
        # fromisoformat happily parses a bare date as midnight — and a line
        # written '2026-03-15 14:30:00' tokenizes as a bare date followed by
        # a bogus command, silently discarding the intended time of day.
        return (
            f"timestamp {token!r} has no time of day — "
            "write 2026-03-15T14:30:00Z (T between date and time, not a space)"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # naked timestamps are UTC
    return dt.timestamp()


_DELAY_RE = re.compile(r"^\+\d+(?:\.\d+)?$")


def _parse_rts_delay(token: str) -> float | str:
    """Delay seconds from a ``+N`` token, or a problem description."""
    if not token.startswith("+"):
        return f"delay must start with '+', got {token!r}"
    if _DELAY_RE.match(token) is None:
        # Plain decimal seconds only: "+-5" would float-parse as negative,
        # and "+1e3"/"+1_0" are not how anyone writes a command plan.
        return f"delay must be a non-negative number of seconds, got {token!r}"
    return float(token[1:])


def _entry_problem(entry: SequenceEntry, simdef) -> str | None:
    """Why this entry could not be uplinked, or None if it encodes cleanly."""
    command = simdef.command_by_name(entry.command)
    if command is None:
        return f"unknown command {entry.command!r}"
    try:
        codec.encode_command(command, dict(entry.args))
    except (ValueError, struct.error) as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# Ground-side time shifting


def shift_ats(text: str, name: str, delta: float) -> str:
    """The same ATS text with every timestamp moved by ``delta`` seconds.

    Comments, blank lines, spacing, line endings, and the command half of
    every entry are preserved byte-for-byte; only the timestamp tokens are
    rewritten. Splitting on '\\n' keeps a CRLF file's carriage returns in
    place (they ride along inside each line and strip() ignores them). The
    text must already parse (raises SequenceError otherwise).
    """
    parse_ats(text, name)  # reject broken files before touching them
    out: list[str] = []
    for raw in text.split("\n"):
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            out.append(raw)
            continue
        token = stripped.split()[0]
        when = _parse_ats_time(token)
        assert not isinstance(when, str)  # parse_ats above guarantees this
        out.append(raw.replace(token, format_utc(when + delta), 1))
    return "\n".join(out)


def format_utc(epoch: float) -> str:
    """An epoch as ISO-8601 UTC with a Z, at the precision it needs."""
    dt = datetime.fromtimestamp(round(epoch, 6), tz=timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration(text: str) -> float:
    """Seconds from a duration like '30s', '500ms', '5m', '1h', or '90'.

    Bare numbers are seconds. Raises ValueError on anything else — timing
    knobs in this project take durations, never rates.
    """
    match = _DURATION_RE.match(text.strip())
    if match is None:
        raise ValueError(f"invalid duration {text!r} (want e.g. 30s, 500ms, 5m, 1h)")
    return float(match.group("value")) * _DURATION_SCALE[match.group("unit")]
