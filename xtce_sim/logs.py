"""
Per-instance colored logging.

Each running instance keys a stable color off its ``--id`` (a given id is always
the same color, across processes and restarts) so that when a fleet's logs
interleave in one terminal, the ``[id]`` tag is easy to tell apart. Warnings and
errors are colored regardless of instance so they still stand out.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from typing import Optional, TextIO

import click

# Distinct, readable foreground colors (click names). Red is reserved for errors,
# so it is deliberately absent here.
_PALETTE = [
    "cyan",
    "green",
    "yellow",
    "magenta",
    "blue",
    "bright_cyan",
    "bright_green",
    "bright_magenta",
    "bright_yellow",
    "bright_blue",
]


def instance_color(instance_id: str) -> str:
    """Deterministic color name for an instance id.

    Hashes the id to distribute instances across the palette so a fleet is
    easier to tell apart in interleaved logs. This is not a security context;
    SHA-256 is used simply because it is not flagged as a weak hash. The
    mapping is best-effort: with a fixed palette, distinct ids can still
    collide onto the same color.
    """
    digest = hashlib.sha256(instance_id.encode("utf-8")).digest()
    return _PALETTE[int.from_bytes(digest, "big") % len(_PALETTE)]


class InstanceFormatter(logging.Formatter):
    """Formats log lines as ``HH:MM:SS [id] message`` with optional color."""

    def __init__(self, instance_id: str, *, color: bool = True) -> None:
        super().__init__()
        self.instance_id = instance_id
        self.color = color
        self._tag_color = instance_color(instance_id)

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        tag = f"[{self.instance_id}]"
        msg = record.getMessage()
        if record.exc_info:
            msg = f"{msg}\n{self.formatException(record.exc_info)}"

        if self.color:
            ts = click.style(ts, fg="bright_black")
            tag = click.style(tag, fg=self._tag_color, bold=True)
            if record.levelno >= logging.ERROR:
                msg = click.style(msg, fg="red")
            elif record.levelno >= logging.WARNING:
                msg = click.style(msg, fg="yellow")

        return f"{ts} {tag} {msg}"


def _use_color(mode: str, stream: TextIO) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class _TraceFormatter(logging.Formatter):
    """Bare messages for the parse trace; warnings/errors get a ``!`` marker.

    Trace lines are already shaped by their emitters (indentation, a leading
    ``~`` on inference/leniency lines), so no timestamp/level prefix — the
    output should read like an annotated walk of the XTCE document.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        return f"! {msg}" if record.levelno >= logging.WARNING else msg


class _TraceHandler(logging.StreamHandler):
    """StreamHandler that stays quiet when the reader hangs up.

    Trace output is made to be piped (``inspect file.xml | head``); the pipe
    closing early is normal usage, not an error worth a traceback per record.
    """

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - stdlib API name
        exc = sys.exc_info()[1]
        if not isinstance(exc, BrokenPipeError):
            super().handleError(record)


def enable_trace(level: int = logging.INFO, stream: Optional[TextIO] = None) -> None:
    """Route the parse/build trace (``xtce_sim.*`` module logs) to *stream*.

    INFO shows decisions and inferences (lines the parser marks with ``~``);
    DEBUG adds the full per-element firehose. Normal runs never see either:
    the ``xtce_sim`` logger tree has no handler until this is called.
    """
    trace_logger = logging.getLogger("xtce_sim")
    handler = _TraceHandler(stream or sys.stdout)
    handler.setFormatter(_TraceFormatter())
    trace_logger.handlers.clear()
    trace_logger.addHandler(handler)
    trace_logger.setLevel(level)


def setup_logging(
    instance_id: str,
    *,
    color: str = "auto",
    level: int = logging.INFO,
    stream: Optional[TextIO] = None,
) -> logging.Logger:
    """Configure and return a logger for one simulator instance.

    ``color`` is "auto" (color only on a TTY, honoring NO_COLOR), "always", or
    "never". The logger does not propagate, so repeated setup for the same id
    won't duplicate lines.
    """
    stream = stream or sys.stderr
    handler = logging.StreamHandler(stream)
    handler.setFormatter(InstanceFormatter(instance_id, color=_use_color(color, stream)))

    logger = logging.getLogger(f"xtce-sim:{instance_id}")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger
