"""
Rendering helpers for the three `xtce-sim monitor` display styles.

Pure string builders (color via click.style, which auto-strips when the output
isn't a TTY), kept out of the CLI so they can be unit-tested. Each takes the
decoded packet metadata: a list of ``(field_name, value, unit)`` tuples in
packet order.
"""

from __future__ import annotations

import click

Meta = list  # list[tuple[str, object, str | None]]


def fmt_value(value) -> str:
    """Format a decoded field value compactly.

    Fixed-length string/binary fields arrive as NUL-padded bytes: trailing NULs
    are trimmed, printable ASCII shows as a quoted string, and anything else
    falls back to hex.
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, bytes):
        trimmed = value.rstrip(b"\x00")
        if not trimmed:
            return "''"
        if all(32 <= b < 127 for b in trimmed):
            return repr(trimmed.decode("ascii"))
        return trimmed.hex()
    return str(value)


def common_prefix(names: list[str]) -> str:
    """The shared ``FOO_`` prefix of a packet's field names, if any.

    Telemetry fields are conventionally prefixed by packet (HK_, EVT_, SCI_);
    stripping that prefix keeps the compact/dashboard rows readable.
    """
    if len(names) < 2:
        return ""
    first = names[0]
    cut = first.find("_")
    if cut == -1:
        return ""
    prefix = first[: cut + 1]
    return prefix if all(n.startswith(prefix) for n in names) else ""


def _short(name: str, prefix: str) -> str:
    return name[len(prefix):] if prefix and name.startswith(prefix) else name


def _field_bits(meta: Meta, prefix: str) -> list[str]:
    bits = []
    for name, value, unit in meta:
        unit_str = click.style(f" {unit}", fg="bright_black") if unit else ""
        bits.append(f"{_short(name, prefix)}={fmt_value(value)}{unit_str}")
    return bits


def render_compact(
    ts: str,
    apid: int,
    name: str,
    seq: int,
    meta: Meta,
    prefix: str,
    *,
    max_fields: int = 4,
    show_all: bool = False,
) -> str:
    """One aligned, colored line per packet (first few fields + '+N more')."""
    shown = meta if show_all else meta[:max_fields]
    bits = "  ".join(_field_bits(shown, prefix))
    more = 0 if show_all else max(0, len(meta) - len(shown))
    tail = click.style(f"  +{more} more", fg="bright_black") if more else ""
    return (
        f"{click.style(ts, fg='bright_black')}  "
        f"{click.style(f'0x{apid:02X}', fg='cyan')} "
        f"{click.style(name.ljust(16), bold=True)} "
        f"{click.style('seq', fg='bright_black')} {seq:<6} "
        f"{bits}{tail}"
    )


def render_table(ts: str, apid: int, name: str, seq: int, meta: Meta, prefix: str) -> str:
    """A boxed header plus an aligned row per field (value + unit)."""
    title = f" {name} · APID 0x{apid:02X} · seq {seq} · {ts} "
    width = max((len(n) for n, _, _ in meta), default=0)
    lines = [click.style("┌" + title, fg="cyan")]
    for fname, value, unit in meta:
        unit_str = click.style(f"  {unit}", fg="bright_black") if unit else ""
        lines.append(
            "│ "
            + f"{fname.ljust(width)}  "
            + click.style(fmt_value(value), fg="green")
            + unit_str
        )
    lines.append("└" + "─" * len(title))
    return "\n".join(lines)


def render_dashboard(
    host: str,
    port: int,
    instance: str,
    latest: dict,
    total: int,
    *,
    max_fields: int = 5,
) -> str:
    """A full-screen frame: a header and one row per APID (latest values)."""
    header = click.style(
        f"xtce-sim monitor · {instance} · {host}:{port}", bold=True
    ) + click.style(f"     packets {total:,}", fg="bright_black")
    lines = [header, click.style("─" * 66, fg="bright_black")]
    for apid in sorted(latest):
        name, seq, meta, prefix = latest[apid]
        bits = "  ".join(_field_bits(meta[:max_fields], prefix))
        more = max(0, len(meta) - max_fields)
        tail = click.style(f"  +{more}", fg="bright_black") if more else ""
        lines.append(
            f"{click.style(f'0x{apid:02X}', fg='cyan')} "
            f"{click.style(name.ljust(14), bold=True)} "
            f"{click.style('seq', fg='bright_black')} {seq:<6} {bits}{tail}"
        )
    return "\n".join(lines)
