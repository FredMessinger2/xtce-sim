"""Shared behavior vocabulary: the verb grammar, effect classes, and spec.

Single source of truth for what verbs exist and what each accepts; the
loader validates against these tables and the engine executes the effect
classes. The full DSL documentation is the package docstring
(``xtce_sim/behavior/__init__.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from xtce_sim.dynamics.model import AdcsModelConfig

_TEMPLATE_RE = re.compile(r"\{(\w+)\}")
_EMIT_VALUES = ("interval", "immediate")
# The verb grammar, single-sourced: per-verb attributes, the universal
# attributes every verb accepts, and which verbs are continuous (tick-driven).
# Everything else — the full key set, the signals filter, the dispatch order —
# derives from these three, so a new verb or attribute is added in one place.
_VERB_ATTRS = {
    "set": set(),
    "increment": set(),
    "ramp_to": {"tau", "noise"},
    "oscillate": {"amplitude", "period", "shape", "phase", "noise"},
    "hold": {"noise"},
}
_UNIVERSAL_ATTRS = {"emit"}
_CONTINUOUS_VERBS = ("ramp_to", "oscillate", "hold")
_VERB_KEYS = set(_VERB_ATTRS) | _UNIVERSAL_ATTRS | set().union(*_VERB_ATTRS.values())
_WAVE_SHAPES = ("sine", "triangle", "sawtooth")
# Templated args are expanded for load-time validation up to this many
# combinations; beyond it (or for unbounded args) field checks defer to
# execution time.
_MAX_EXPANSIONS = 100

Scalar = int | float | bool | str


class BehaviorError(ValueError):
    """A behavior file failed validation; the message lists every problem."""


@dataclass
class SetEffect:
    field: str  # possibly templated
    value: Scalar  # number/bool, enum label, or string payload
    emit: str = "interval"


@dataclass
class CopyArgEffect:
    field: str
    arg: str
    emit: str = "interval"


@dataclass
class IncrementEffect:
    field: str
    by: float
    emit: str = "interval"


@dataclass
class RampEffect:
    field: str
    target: float | str  # number, or "@FIELD" (possibly templated)
    tau: float
    noise: float = 0.0  # gaussian stddev added to the emitted value
    emit: str = "interval"


@dataclass
class OscillateEffect:
    field: str
    center: float | str  # number, or "@FIELD" (possibly templated)
    amplitude: float
    period: float  # seconds (a period, never a frequency)
    shape: str = "sine"  # sine | triangle | sawtooth
    phase: float = 0.0  # seconds of offset into the cycle
    noise: float = 0.0
    emit: str = "interval"


@dataclass
class HoldEffect:
    field: str
    value: float | str  # number, or "@FIELD" (tracked live)
    noise: float = 0.0
    emit: str = "interval"


Effect = SetEffect | CopyArgEffect | IncrementEffect | RampEffect | OscillateEffect | HoldEffect
# The effect classes behind _CONTINUOUS_VERBS: registered as active behaviors
# and advanced by tick(), versus the instant set/copy/increment.
_CONTINUOUS_EFFECTS = (RampEffect, OscillateEffect, HoldEffect)


@dataclass
class BehaviorSpec:
    """Validated behavior: initial values, boot signals, command effects."""

    path: Path  # the source as given: a satellite directory or one file
    initial: dict[str, Scalar]
    commands: dict[str, list[Effect]]  # command name -> effects
    # Continuous behaviors started at boot.
    signals: list[Effect] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)  # the merged .toml files
    # Physics models declared under [_models]: each owns its output fields
    # (no other table may write them) and consumes its bound commands.
    models: list[AdcsModelConfig] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        """Human label: 'dir/ (3 files)' for a directory, the path for a file."""
        if len(self.files) > 1 or (self.path and Path(self.path).is_dir()):
            return f"{self.path} ({len(self.files)} file(s))"
        return str(self.path)
