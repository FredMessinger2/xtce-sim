"""
Synthetic "live" telemetry.

An optional telemetry source (enabled by `run --live`) that produces values which
move over time — counters rise, temperatures and voltages drift on slow sines,
rates wobble around zero — so `monitor` shows changing data instead of zeros.

This is a light stand-in for XTCE-driven telemetry physics: values are chosen by
field-name heuristics and clamped to each field's integer range, not derived from
the XTCE. Strings/binary stay empty. Deterministic given a time `t`, so it's
testable and reproducible.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from xtce_sim.definition import PacketDef

# Representable range per integer python_type, so packed values never overflow.
_INT_BOUNDS = {
    "uint8": (0, 255),
    "int8": (-128, 127),
    "uint16": (0, 65535),
    "int16": (-32768, 32767),
    "uint32": (0, 4294967295),
    "int32": (-2147483648, 2147483647),
    "uint64": (0, 18446744073709551615),
    "int64": (-9223372036854775808, 9223372036854775807),
}


# Ordered field-name -> signal rules (first match wins), preserving the
# original keyword precedence. Each keyword tuple maps to a function of time t;
# a field whose upper-cased name contains any keyword uses that rule.
_SIGNAL_RULES: list[tuple[tuple[str, ...], Callable[[float], float]]] = [
    (("TIMESTAMP",), lambda t: 1735689600 + t),
    (("COUNT", "UPTIME", "TOTAL", "SEQUENCE", "SEQ", "NUM"), lambda t: t * 2),  # rising
    (("VOLT",), lambda t: 7.4 + 0.15 * math.sin(t / 5)),
    (("CURRENT",), lambda t: 0.8 + 0.2 * math.sin(t / 4)),
    (("TEMP", "THERM"), lambda t: 22 + 4 * math.sin(t / 20)),
    (("FUEL", "PERCENT"), lambda t: max(0.0, 95 - t * 0.05)),  # slow drain
    (("ALT",), lambda t: 500 + 8 * math.sin(t / 30)),
    (("VEL",), lambda t: 7.6 + 0.05 * math.sin(t / 10)),
    (("RATE", "ANGULAR", "POINT", "DELTA"), lambda t: 0.5 * math.sin(t / 3)),  # wobble ~0
    (("WHEEL", "RPM", "SPEED"), lambda t: 1500 + 200 * math.sin(t / 8)),
    (("FREQ",), lambda t: 2200 + 5 * math.sin(t / 12)),
    (("POWER", "DBM"), lambda t: 20 + 2 * math.sin(t / 9)),
    (("MODE", "STATE", "STATUS", "REGIME", "TYPE", "PHASE"), lambda t: (t / 6) % 4),  # steps
    (
        ("FLAG", "ENABLED", "SEVERITY", "RESULT", "ERROR", "FAULT"),
        lambda t: 1.0 if math.sin(t / 7) > 0.6 else 0.0,
    ),
]


def _base_signal(name: str, t: float) -> float:
    """A plausible moving value for a field, chosen by name keywords.

    Rules are checked in order; the first whose keyword appears in ``name``
    wins. Unmatched fields get a generic slow sine.
    """
    for keywords, fn in _SIGNAL_RULES:
        if any(k in name for k in keywords):
            return fn(t)
    return 50 + 40 * math.sin(t / 6)


def _synth_value(field, t: float):
    if field.python_type in ("string", "bytes"):
        return b""
    value = _base_signal(field.name.upper(), t)
    # The heuristics think in engineering units ("about 8 volts"). A field
    # with a calibrator transmits raw counts, so send the count that
    # calibrates back to the plausible value (when the inverse exists).
    if field.calibrator is not None:
        raw = field.calibrator.invert(value)
        if raw is not None:
            value = raw
    if field.python_type in ("float32", "float64"):
        return value
    lo, hi = _INT_BOUNDS.get(field.python_type, (0, 255))
    # Saturate (clamp) into range — NOT modulo-wrap, so a near-zero negative
    # value on an unsigned field reads as ~0, not as the type's maximum.
    return max(lo, min(hi, int(round(value))))


class LiveTelemetry:
    """A callable telemetry source: ``source(packet) -> {field: value}``.

    Pass to ``SimServer(telemetry_source=...)``. Time is read from ``clock``
    (default ``time.monotonic``) relative to construction, so values start near
    their nominal and evolve from there.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start = clock()

    def __call__(self, packet: PacketDef) -> dict:
        return self.values_at(packet, self._clock() - self._start)

    def values_at(self, packet: PacketDef, t: float) -> dict:
        """Pure, time-parameterized values for a packet (used by tests)."""
        return {f.name: _synth_value(f, t) for f in packet.fields}
