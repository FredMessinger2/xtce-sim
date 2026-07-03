"""Tests for per-instance colored logging."""

import io
import logging

from xtce_sim import logs


def _record(msg, level=logging.INFO):
    return logging.LogRecord("t", level, __file__, 1, msg, None, None)


def test_instance_color_is_deterministic():
    assert logs.instance_color("sat-a") == logs.instance_color("sat-a")
    assert logs.instance_color("sat-a") in logs._PALETTE


def test_sequential_ids_get_distinct_colors():
    ids = ["sat-a", "sat-b", "sat-c", "sat-d", "sat-e", "sat-f"]
    colors = {logs.instance_color(i) for i in ids}
    assert len(colors) == len(ids)  # sha1 spread keeps a small fleet distinct


def test_formatter_plain():
    line = logs.InstanceFormatter("sat-a", color=False).format(_record("hello"))
    assert line.endswith("[sat-a] hello")
    assert "\x1b[" not in line  # no ANSI when color disabled


def test_formatter_colors_tag_and_errors():
    fmt = logs.InstanceFormatter("sat-a", color=True)
    info = fmt.format(_record("up"))
    assert "\x1b[" in info and "[sat-a]" in info
    err = fmt.format(_record("boom", logging.ERROR))
    assert "\x1b[31m" in err  # errors are red regardless of instance color


def test_setup_logging_configures_named_logger():
    stream = io.StringIO()
    log = logs.setup_logging("sat-z", color="never", stream=stream)
    assert log.name == "xtce-sim:sat-z"
    assert log.propagate is False
    assert len(log.handlers) == 1

    log.info("ready")
    assert "[sat-z] ready" in stream.getvalue()

    # Re-setup must not duplicate handlers (so lines aren't doubled).
    logs.setup_logging("sat-z", color="never", stream=stream)
    assert len(logging.getLogger("xtce-sim:sat-z").handlers) == 1


def test_color_mode_never_and_always():
    stream = io.StringIO()
    assert logs._use_color("never", stream) is False
    assert logs._use_color("always", stream) is True
