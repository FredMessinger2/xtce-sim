"""Declarative command→telemetry behavior, loaded from a TOML sidecar.

The XTCE defines the command/telemetry *interface*; behavior TOML defines
what each command *does* to telemetry. A satellite is a directory: every
``.toml`` beside the XTCE is merged (or ``--behavior`` points at a directory
or single file). Files contain one table per command plus optional
``[_initial]`` start-up values and ``[_signals]`` boot behaviors:

    [_initial]
    THM_HEATER1_TEMP = 20.0

    [HEATER_ON]
    "THM_HEATER{HeaterId}_STATE" = 1                     # set
    "THM_HEATER{HeaterId}_TEMP" = { ramp_to = "@THM_HEATER{HeaterId}_SETPOINT", tau = 30.0 }

    [SET_EXPOSURE]
    IMG_EXPOSURE_MS = "@arg:ExposureMs"                  # copy an argument

Verbs: a bare scalar sets the field; ``"@arg:Name"`` copies a command
argument; ``{ increment = n }`` adds; ``{ ramp_to = X, tau = S }`` starts a
first-order approach toward X (a number, or ``"@FIELD"`` read live each
tick); ``{ oscillate = C, amplitude = A, period = P }`` runs a continuous
wave around center C (``shape`` = "sine"/"triangle"/"sawtooth", optional
``phase`` seconds); ``{ hold = V }`` keeps re-asserting V. An ``@FIELD``
reference must not name the field itself — feeding a field its own output
turns noise/waves into unbounded drift — so literal self-references are
load errors and a template that resolves to its own field is skipped at
execution. Continuous verbs (ramp_to/oscillate/hold) accept ``noise =
stddev`` — one seeded RNG per field per engine, so separate runs reproduce
each other while a restarted behavior continues its stream — and a
completed noisy ramp degrades into a noisy hold at its target. An optional ``[_signals]`` table starts continuous
behaviors at boot (ambient realism: orbit thermal cycles, bus ripple) with
no command needed; a command's behavior on the same field replaces a
signal, and a direct set cancels it, exactly like ramps. ``{ArgName}`` inside a field name or ``@`` target is filled at
execution time: an enumerated argument substitutes its **label** (so
``"PWR_{SubsystemId}_STATE"`` resolves to ``PWR_COMMS_STATE``), a plain
integer argument its raw value (``"THM_HEATER{HeaterId}_STATE"`` to
``THM_HEATER1_STATE``); a raw enum value with no declared label — or a
string that is not one of the declared labels — refuses to resolve and the
effect is skipped with a warning. An instant effect
(set/copy/increment) may carry ``emit = "immediate"``: the packet containing
the field is emitted out-of-cycle the moment the command executes, while the
beacon keeps its own schedule — for a copy that is written
``{ set = "@arg:Name", emit = "immediate" }``. Continuous verbs reject it
(they pace with the beacon by nature). Booleans are rejected as values:
write ``0``/``1`` or an enum label.

Behavior values are ENGINEERING UNITS. A field whose XTCE declares a
calibrator transmits raw counts, but the sidecar speaks the calibrated
meaning — a setpoint of 25.5 means degrees, not counts — and the engine
converts at the wire boundary (inverting on write, calibrating on read for
``@FIELD`` references and increments). A behavior-governed calibrated field
therefore needs an invertible calibrator (affine polynomial or monotonic
spline); anything else is a load error.

Validation is strict and total: every command table, field name, argument
reference, enum label, and verb key is checked against the resolved
SimDefinition, and *all* problems are reported in one BehaviorError.

BehaviorEngine executes a loaded spec at runtime: it keeps an overlay of
field values that wins over the synthetic generator when packets are packed,
applies set/copy/increment effects when commands execute, seeds the
``[_initial]`` values at start, and advances active ramps each beacon tick
(closed-form first-order step, so trajectories are identical at any tick
size; ``@FIELD`` targets are re-read live; a new ramp on a field replaces
the old one).
"""

from xtce_sim.behavior.engine import (
    BehaviorEngine,
    _ActiveHold,
    _ActiveOsc,
    _wave,
)
from xtce_sim.behavior.loader import describe, load_behavior, sidecar_path
from xtce_sim.behavior.spec import (
    BehaviorError,
    BehaviorSpec,
    CopyArgEffect,
    Effect,
    HoldEffect,
    IncrementEffect,
    OscillateEffect,
    RampEffect,
    Scalar,
    SetEffect,
)

__all__ = [
    "BehaviorEngine",
    "BehaviorError",
    "BehaviorSpec",
    "CopyArgEffect",
    "Effect",
    "HoldEffect",
    "IncrementEffect",
    "OscillateEffect",
    "RampEffect",
    "Scalar",
    "SetEffect",
    "describe",
    "load_behavior",
    "sidecar_path",
    "_ActiveHold",
    "_ActiveOsc",
    "_wave",
]
