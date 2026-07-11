"""Command-surface exerciser.

Sends a valid instance of every command in a definition — thoroughly: one send
per enum label and per numeric min/max boundary, varying a single parameter at
a time — and optionally confirms the sim keeps serving valid telemetry
throughout. A smoke test for a whole XTCE command set.

"Verify" here means telemetry *health* — the sim stayed alive and every
packet still decoded — not per-command effects. (When a behavior sidecar is
loaded, commands do change telemetry, but this exerciser does not yet check
that each command produced its declared effect.)
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from xtce_sim import ccsds, client, codec
from xtce_sim.definition import CommandDef, ParamInfo, SimDefinition

_STRING_TYPES = ("string", "bytes")


def _dedupe(seq: list) -> list:
    out: list = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


# Largest finite magnitude a 32-bit float can hold; float64 needs no clamp.
_FLOAT32_MAX = 3.4028234663852886e38


def _int_wire_bounds(param: ParamInfo) -> tuple[int, int]:
    """(min, max) the parameter's integer wire type can actually hold."""
    bits = param.size_bits or 8
    if param.python_type.startswith("u"):
        return 0, (1 << bits) - 1
    return -(1 << (bits - 1)), (1 << (bits - 1)) - 1


def example_values(param: ParamInfo) -> list:
    """Valid values to exercise for one parameter; the first is the baseline.

    Enums yield every label; strings/binary yield one short sample; numeric
    params yield their declared min/max (or ``0`` / ``0.0`` when unbounded).

    Declared bounds come from XTCE and are *clamped* to what the field's wire
    type can hold: a ValidRange stated in engineering units can exceed the raw
    type (``encode_command`` packs raw, uncalibrated), which would otherwise
    overflow ``struct.pack``. Clamping keeps every generated value packable.
    """
    if param.enumerations:
        return list(param.enumerations)
    if param.python_type in _STRING_TYPES:
        # Clamp to the field's byte capacity — command encoding rejects
        # oversized values, and a 2-byte field must not get a 4-byte "TEST".
        # (ASCII, so character slicing == byte slicing.)
        return ["TEST"[: param.size_bits // 8]]
    if param.python_type in ("float32", "float64"):
        declared = [float(v) for v in (param.valid_min, param.valid_max) if v is not None]
        if not declared:
            return [0.0]
        if param.python_type == "float32":
            declared = [max(-_FLOAT32_MAX, min(_FLOAT32_MAX, v)) for v in declared]
        return _dedupe(declared)
    declared = [int(v) for v in (param.valid_min, param.valid_max) if v is not None]
    if not declared:
        return [0]
    lo, hi = _int_wire_bounds(param)
    return _dedupe([max(lo, min(hi, v)) for v in declared])


def command_arg_sets(command: CommandDef) -> list[tuple[str, dict]]:
    """The ``(label, args)`` pairs to send for one command.

    A baseline (one valid value per parameter) followed by each parameter
    varied one at a time across its remaining example values, so coverage grows
    linearly rather than as the cross-product. A parameterless command yields a
    single ``("baseline", {})``.
    """
    params = command.params
    baseline = {p.name: example_values(p)[0] for p in params}
    arg_sets: list[tuple[str, dict]] = [("baseline", dict(baseline))]
    for p in params:
        for value in example_values(p)[1:]:
            args = dict(baseline)
            args[p.name] = value
            arg_sets.append((f"{p.name}={value}", args))
    return arg_sets


@dataclass
class SendResult:
    command: str
    label: str  # which arg-set (e.g. "baseline" or "PowerState=ON")
    ok: bool
    error: str = ""


@dataclass
class TelemetryHealth:
    packets: int = 0
    apids: set = field(default_factory=set)
    decode_failures: int = 0
    sample: Optional[str] = None
    error: Optional[str] = None  # set when telemetry could not be read at all


@dataclass
class ExerciseReport:
    sends: list[SendResult] = field(default_factory=list)
    telemetry: Optional[TelemetryHealth] = None

    @property
    def failures(self) -> list[SendResult]:
        return [s for s in self.sends if not s.ok]

    @property
    def ok(self) -> bool:
        if self.failures:
            return False
        t = self.telemetry
        return not (t is not None and (t.error or t.decode_failures))


def check_telemetry(
    host: str, port: int, simdef: SimDefinition, *, timeout: float = 3.0
) -> TelemetryHealth:
    """Read telemetry for up to *timeout* seconds and confirm each packet decodes.

    Returns early once one full beacon cycle (every known APID) has been seen.
    A connection failure is reported in ``TelemetryHealth.error`` rather than
    raised, so the caller can still report the send results.
    """
    health = TelemetryHealth()
    want = {p.apid for p in simdef.packets}
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        health.error = str(exc)
        return health
    sock.settimeout(timeout)
    deadline = time.monotonic() + timeout
    buffer = b""
    try:
        while time.monotonic() < deadline and not want.issubset(health.apids):
            try:
                data = sock.recv(4096)
            except OSError:  # includes socket timeout
                break
            if not data:
                break
            try:
                packets, buffer = ccsds.deframe(buffer + data)
            except ccsds.FrameError as exc:
                # A corrupt/unresyncable stream is itself a health failure.
                health.error = f"framing error: {exc}"
                break
            for pkt in packets:
                _tally(pkt, simdef, health)
    finally:
        sock.close()
    return health


def _tally(pkt: bytes, simdef: SimDefinition, health: TelemetryHealth) -> None:
    if len(pkt) < 6:
        return
    header = ccsds.CCSDSHeader.unpack(pkt[:6])
    if header.apid == ccsds.CMD_ECHO_APID:
        # Command echoes (see ccsds.py) are link infrastructure, not the
        # satellite's telemetry — counting them would pad the APID tally.
        return
    health.packets += 1
    health.apids.add(header.apid)
    packet_def = simdef.packet_by_apid(header.apid)
    if packet_def is None:
        return
    try:
        values = codec.unpack_telemetry(packet_def, pkt[6:])
    except struct.error:
        health.decode_failures += 1
        return
    if health.sample is None:
        shown = ", ".join(f"{k}={values[k]}" for k in list(values)[:3])
        health.sample = f"{packet_def.name}: {shown}"


def run_exercise(
    simdef: SimDefinition,
    host: str,
    port: int,
    *,
    apid: int = 1,
    commands: Optional[set] = None,
    verify: bool = True,
    verify_timeout: float = 3.0,
    pause: float = 0.0,
    on_send=None,
) -> ExerciseReport:
    """Send every command's arg-sets, then (optionally) check telemetry health.

    ``commands`` limits the run to a set of command names; None exercises all.
    Each send is recorded as a SendResult; a per-send failure (bad encode or a
    dropped connection) is captured, not raised, so one bad command doesn't
    abort the sweep.

    ``pause`` waits that many seconds after each send, so a human watching
    telemetry can see each command's effect land. ``on_send`` is called with
    each SendResult as it happens (per-send narration for interactive use).
    """
    report = ExerciseReport()
    for command in simdef.commands:
        if commands is not None and command.name not in commands:
            continue
        _send_arg_sets(command, host, port, apid, report, pause, on_send)
    if verify:
        report.telemetry = check_telemetry(host, port, simdef, timeout=verify_timeout)
    return report


def _send_arg_sets(command, host, port, apid, report, pause, on_send) -> None:
    """Fire every arg-set of one command, recording (and narrating) each send.

    Pacing happens *between* sends (never before the first or after the last
    of a sweep), so a paused run doesn't sit idle after its final command.
    """
    for label, args in command_arg_sets(command):
        if pause > 0 and report.sends:
            time.sleep(pause)
        try:
            client.send_command(host, port, command, args, apid=apid)
            result = SendResult(command.name, label, True)
        except (OSError, ValueError, struct.error, OverflowError) as exc:
            result = SendResult(command.name, label, False, str(exc))
        report.sends.append(result)
        if on_send is not None:
            on_send(result)
