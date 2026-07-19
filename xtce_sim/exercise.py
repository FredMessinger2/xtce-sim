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

import math
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from xtce_sim import ccsds, client, codec, fileservice
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
    if (param.valid_min is not None and param.valid_min > hi) or (
        param.valid_max is not None and param.valid_max < lo
    ):
        # Disjoint: every wire-encodable value violates the declared range,
        # so the vehicle would reject any instance of this command. That is a
        # definition problem — diagnose it instead of stumbling into a
        # clamped value the encoder then refuses.
        raise ValueError(
            f"{param.name}: no wire-encodable {param.python_type} value "
            f"satisfies ValidRange [{param.valid_min}, {param.valid_max}]"
        )
    return _dedupe([max(lo, min(hi, v)) for v in declared])


def _invalid_value(param: ParamInfo):
    """A wire-encodable value that VIOLATES the param's declared constraints.

    None when no such value exists (an unconstrained param, or a range that
    already spans its whole wire type) — such a param can't be probed.
    """
    if param.enumerations:
        return _invalid_enum_value(param)
    if param.python_type in ("float32", "float64"):
        return _invalid_float_value(param)
    return _invalid_int_value(param)


def _invalid_enum_value(param: ParamInfo):
    values = set(param.enumerations.values())
    lo, hi = _int_wire_bounds(param)  # enums can ride signed wire types too
    above = max(values) + 1
    if above <= hi and above not in values:
        return above
    below = min(values) - 1
    if below >= lo and below not in values:
        return below
    return None


def _invalid_float_value(param: ParamInfo):
    limit = _FLOAT32_MAX if param.python_type == "float32" else float("inf")
    # A non-finite bound (maxInclusive="INF" parses) can't be exceeded —
    # skip that side rather than emit a probe the vehicle would accept.
    vmax, vmin = param.valid_max, param.valid_min
    if vmax is not None and math.isfinite(vmax):
        candidate = float(vmax) + max(1.0, abs(vmax) * 0.5)
        if candidate <= limit:
            return candidate
    if vmin is not None and math.isfinite(vmin):
        candidate = float(vmin) - max(1.0, abs(vmin) * 0.5)
        if candidate >= -limit:
            return candidate
    return None


def _invalid_int_value(param: ParamInfo):
    lo, hi = _int_wire_bounds(param)
    vmax, vmin = param.valid_max, param.valid_min
    # isfinite guards int(inf) blowing up on a maxInclusive="INF" definition.
    if vmax is not None and math.isfinite(vmax) and int(vmax) + 1 <= hi:
        return int(vmax) + 1
    if vmin is not None and math.isfinite(vmin) and int(vmin) - 1 >= lo:
        return int(vmin) - 1
    return None


def reject_probe(command: CommandDef) -> Optional[tuple[str, dict]]:
    """A deliberately-invalid ``(label, args)`` for one command, or None.

    A valid baseline with exactly one argument pushed outside its declared
    range/enum — the vehicle must reject it (REJECTED echo, no effects).
    """
    for param in command.params:
        bad = _invalid_value(param)
        if bad is None:
            continue
        try:
            args = {p.name: example_values(p)[0] for p in command.params}
        except (ValueError, OverflowError):
            return None  # unsatisfiable/non-finite definition; reported by the sweep
        args[param.name] = bad
        return f"reject-probe {param.name}={bad}", args
    return None


def build_send_plan(
    targets: list[CommandDef], *, reject_probes: int = 0, seed: int = 0
) -> tuple[list, list]:
    """The sweep's send plan: ``[(command, label, args, validate), ...]``.

    Normal sends validate on the ground as usual. ``reject_probes`` sprinkles
    that many deliberately out-of-range sends (validate=False — transmitted
    anyway, for the vehicle to reject) at seeded, deterministic positions
    among the normal sends: same seed, same sprinkle, so runs reproduce
    exactly, and each --loop sweep (seeded by its index) scatters anew.

    Also returns ``problems``: (command_name, error) for commands whose
    declared ranges no wire value can satisfy.
    """
    plan: list = []
    problems: list = []
    for command in targets:
        try:
            plan.extend(
                (command, label, args, True) for label, args in command_arg_sets(command)
            )
        except (ValueError, OverflowError) as exc:
            # OverflowError covers a non-finite declared bound (int(inf)) —
            # a definition problem, reported instead of tracebacked.
            problems.append((command.name, str(exc)))
    if reject_probes > 0:
        candidates = []
        for command in targets:
            probe = reject_probe(command)
            if probe is not None:
                candidates.append((command, *probe))
        for i in range(reject_probes if candidates else 0):
            command, label, args = candidates[_scatter(seed, i, len(candidates))]
            position = _scatter(seed ^ 0x5EED, i, len(plan) + 1)
            plan.insert(position, (command, label, args, False))
    return plan, problems


def _scatter(seed: int, i: int, n: int) -> int:
    """The i-th deterministic scatter index in [0, n) for a seed.

    Knuth multiplicative hashing — randomness QUALITY was never the
    requirement here, only good spread and exact reproducibility, so this
    replaces a PRNG outright: no state, no security-scanner conversations,
    same index for the same (seed, i, n) forever.
    """
    return ((seed * 2654435761 + i * 40503 + 12345) & 0xFFFFFFFF) % n


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

    Returns early once one full beacon cycle has been seen. Event-only
    packets (FILE_RECEIPT — see fileservice.event_only_apids) are excluded
    from that wait: they arrive only when something happens, and a healthy
    idle vehicle never sends one. A connection failure is reported in
    ``TelemetryHealth.error`` rather than raised, so the caller can still
    report the send results.
    """
    health = TelemetryHealth()
    want = {p.apid for p in simdef.packets} - fileservice.event_only_apids(simdef)
    # Per-packet periods: full coverage takes at least one lap of the
    # SLOWEST declared period, so the read window scales with the ICD
    # instead of silently under-reading a slow packet.
    slowest = max((p.period_s or 0.0 for p in simdef.packets), default=0.0)
    timeout = max(timeout, 2.5 * slowest)
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
    _flag_missing_coverage(health, want, simdef, timeout)
    return health


def _flag_missing_coverage(health: TelemetryHealth, want, simdef, timeout: float) -> None:
    """Silence and coverage gaps are health errors, not quiet greens.

    Both became reachable states: ENABLE_BEACON can silence the vehicle
    entirely, and per-packet periods (or a SET_TLM_RATE retime) can push
    one packet past any fixed read window.
    """
    if health.error is not None:
        return
    if not health.apids:
        health.error = "no telemetry received (beacon disabled, or downlink dead)"
        return
    if want.issubset(health.apids):
        return
    names = {p.apid: p.name for p in simdef.packets}
    missing = ", ".join(names.get(a, hex(a)) for a in sorted(want - health.apids))
    health.error = f"packet(s) never arrived within {timeout:g}s: {missing}"


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
    reject_probes: int = 0,
    probe_seed: int = 0,
) -> ExerciseReport:
    """Send every command's arg-sets, then (optionally) check telemetry health.

    ``commands`` limits the run to a set of command names; None exercises all.
    Each send is recorded as a SendResult; a per-send failure (bad encode or a
    dropped connection) is captured, not raised, so one bad command doesn't
    abort the sweep.

    ``pause`` waits that many seconds after each send, so a human watching
    telemetry can see each command's effect land. ``on_send`` is called with
    each SendResult as it happens (per-send narration for interactive use).

    ``reject_probes`` sprinkles that many deliberately out-of-range sends
    among the sweep (see build_send_plan) — transmitted with the ground
    check bypassed, so the vehicle's own rejection path gets exercised.
    A probe's SendResult records that it was TRANSMITTED; the vehicle's
    verdict rides the command echo (the web console shows ✗ rejected).
    """
    report = ExerciseReport()
    targets = [
        c for c in simdef.commands if commands is None or c.name in commands
    ]
    plan, problems = build_send_plan(
        targets, reject_probes=reject_probes, seed=probe_seed
    )
    for name, error in problems:
        # Unsatisfiable definition (range disjoint from the wire type) —
        # one FAIL entry for the command; the sweep continues.
        result = SendResult(name, "definition", False, error)
        report.sends.append(result)
        if on_send is not None:
            on_send(result)
    first = True
    for command, label, args, validate in plan:
        # Pacing happens *between* sends (never before the first or after
        # the last), so a paused run doesn't sit idle after its final command.
        if pause > 0 and not first:
            time.sleep(pause)
        first = False
        try:
            client.send_command(host, port, command, args, apid=apid, validate=validate)
            result = SendResult(command.name, label, True)
        except (OSError, ValueError, struct.error, OverflowError) as exc:
            result = SendResult(command.name, label, False, str(exc))
        report.sends.append(result)
        if on_send is not None:
            on_send(result)
    if verify:
        report.telemetry = check_telemetry(host, port, simdef, timeout=verify_timeout)
    return report
