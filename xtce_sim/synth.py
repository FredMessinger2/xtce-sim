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


def _base_signal(name: str, t: float) -> float:
    """A plausible moving value for a field, chosen by name keywords."""
    s = math.sin
    if "TIMESTAMP" in name:
        return 1735689600 + t
    if any(k in name for k in ("COUNT", "UPTIME", "TOTAL", "SEQUENCE", "SEQ", "NUM")):
        return t * 2  # monotonically rising
    if "VOLT" in name:
        return 7.4 + 0.15 * s(t / 5)
    if "CURRENT" in name:
        return 0.8 + 0.2 * s(t / 4)
    if "TEMP" in name or "THERM" in name:
        return 22 + 4 * s(t / 20)
    if "FUEL" in name or "PERCENT" in name:
        return max(0.0, 95 - t * 0.05)  # slow drain
    if "ALT" in name:
        return 500 + 8 * s(t / 30)
    if "VEL" in name:
        return 7.6 + 0.05 * s(t / 10)
    if any(k in name for k in ("RATE", "ANGULAR", "POINT", "DELTA")):
        return 0.5 * s(t / 3)  # small wobble around 0
    if "WHEEL" in name or "RPM" in name or "SPEED" in name:
        return 1500 + 200 * s(t / 8)
    if "FREQ" in name:
        return 2200 + 5 * s(t / 12)
    if "POWER" in name or "DBM" in name:
        return 20 + 2 * s(t / 9)
    if any(k in name for k in ("MODE", "STATE", "STATUS", "REGIME", "TYPE", "PHASE")):
        return (t / 6) % 4  # steps through a few values
    if any(k in name for k in ("FLAG", "ENABLED", "SEVERITY", "RESULT", "ERROR", "FAULT")):
        return 1.0 if s(t / 7) > 0.6 else 0.0
    return 50 + 40 * s(t / 6)


def _synth_value(python_type: str, name: str, t: float):
    if python_type in ("string", "bytes"):
        return b""
    value = _base_signal(name.upper(), t)
    if python_type in ("float32", "float64"):
        return value
    lo, hi = _INT_BOUNDS.get(python_type, (0, 255))
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
        return {f.name: _synth_value(f.python_type, f.name, t) for f in packet.fields}
