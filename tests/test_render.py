"""Tests for the monitor rendering helpers (color stripped for assertions)."""

import re

from xtce_sim import render

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(s: str) -> str:
    return ANSI.sub("", s)


META = [
    ("HK_TIMESTAMP", 1735689600, "s"),
    ("HK_SYSTEM_STATUS", 1, None),
    ("HK_BATTERY_VOLTAGE", 7.42, "V"),
    ("HK_CMD_RECV_COUNT", 137, None),
    ("HK_UPTIME", 86432, "s"),
]
PREFIX = "HK_"


def test_fmt_value():
    assert render.fmt_value(7.40) == "7.4"
    assert render.fmt_value(b"OK") == "'OK'"  # printable string, not hex
    assert render.fmt_value(b"") == "''"
    assert render.fmt_value(b"\x00" * 60) == "''"  # NUL-padded string field
    assert render.fmt_value(b"\x01\xff") == "01ff"  # non-printable -> hex
    assert render.fmt_value(42) == "42"


def test_common_prefix():
    assert render.common_prefix(["HK_A", "HK_B", "HK_C"]) == "HK_"
    assert render.common_prefix(["A", "B"]) == ""
    assert render.common_prefix(["HK_A", "EVT_B"]) == ""


def test_compact_truncates_and_strips_prefix():
    line = plain(render.render_compact("14:22:01", 0x01, "HOUSEKEEPING", 42, META, PREFIX))
    assert "0x01" in line and "HOUSEKEEPING" in line and "seq 42" in line
    # prefix stripped, unit shown, first 4 fields + "+1 more"
    assert "TIMESTAMP=1735689600 s" in line
    assert "HK_TIMESTAMP" not in line
    assert "+1 more" in line
    assert "UPTIME=" not in line  # 5th field hidden


def test_compact_show_all():
    line = plain(render.render_compact("14:22:01", 1, "HK", 42, META, PREFIX, show_all=True))
    assert "UPTIME=86432 s" in line
    assert "more" not in line


def test_table_lists_every_field_full_names():
    out = plain(render.render_table("14:22:01", 0x01, "HOUSEKEEPING", 42, META))
    lines = out.splitlines()
    assert lines[0].startswith("┌") and "APID 0x01" in lines[0]
    assert any("HK_BATTERY_VOLTAGE" in ln and "7.42" in ln and "V" in ln for ln in lines)
    assert lines[-1].startswith("└")


def test_dashboard_one_row_per_apid():
    latest = {
        0x01: ("HOUSEKEEPING", 42, META, PREFIX),
        0x03: ("SCIENCE", 42, [("SCI_CH1", 512, None)], "SCI_"),
    }
    out = plain(render.render_dashboard("127.0.0.1", 5000, "sat-a", latest, 1284))
    assert "127.0.0.1:5000" in out and "packets 1,284" in out
    assert "0x01 HOUSEKEEPING" in out
    assert "0x03 SCIENCE" in out
