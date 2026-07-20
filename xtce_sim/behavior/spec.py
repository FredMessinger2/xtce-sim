"""Shared behavior vocabulary: the verb registry, effect bases, and spec.

Each verb module under ``verbs/`` owns everything about its verb — the
effect dataclass, the TOML parsing, the runtime math — and registers a
``Verb`` here. The loader and engine dispatch through the registry and
the effect/active base classes, so adding a verb never touches them.
The full DSL documentation is the package docstring
(``xtce_sim/behavior/__init__.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Optional

from xtce_sim.dynamics.model import AdcsModelConfig

_TEMPLATE_RE = re.compile(r"\{(\w+)\}")
_EMIT_VALUES = ("interval", "immediate")
# Attributes every verb accepts, on top of its own (declared per Verb).
_UNIVERSAL_ATTRS = {"emit"}
# Templated args are expanded for load-time validation up to this many
# combinations; beyond it (or for unbounded args) field checks defer to
# execution time.
_MAX_EXPANSIONS = 100

Scalar = int | float | bool | str


class BehaviorError(ValueError):
    """A behavior file failed validation; the message lists every problem."""


@dataclass
class Effect:
    """Base of every verb's effect (the validated, loadable form).

    Verbs subclass InstantEffect or ContinuousEffect, declare their own
    fields, and implement the respective hooks. ``describe()`` is the
    narration line, without the universal emit tail.
    """

    continuous: ClassVar[bool] = False

    def describe(self) -> str:
        raise NotImplementedError


@dataclass
class InstantEffect(Effect):
    """An effect applied once when its command executes (set/copy/increment)."""

    def value_for(self, engine, command, args, where, fname):
        """The value to store for *fname*, or None to skip this effect
        (the implementation has already warned about why)."""
        raise NotImplementedError


@dataclass
class ContinuousEffect(Effect):
    """A tick-driven behavior the engine registers and advances."""

    continuous: ClassVar[bool] = True

    @property
    def reference(self):
        """The number-or-@FIELD operand (ramp target, wave center, held value)."""
        raise NotImplementedError

    def describe_active(self, ref) -> str:
        """Start-log phrasing for the registered behavior."""
        raise NotImplementedError

    def make_active(self, fname: str, ref, rng) -> "ActiveBehavior":
        raise NotImplementedError


@dataclass
class ActiveBehavior:
    """Base of a running continuous behavior (the engine registry entry).

    Subclasses implement ``advance(engine, dt)``, using the engine's
    shared services (``_store``, ``_live_number``, ``_noisy``) and
    mutating ``engine._behaviors`` on retirement.
    """

    field: str

    def advance(self, engine, dt: float) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class Verb:
    """One DSL verb: its grammar row and its parser.

    ``parse(where, fname, spec, command, emit, ctx)`` validates the
    verb-specific attributes and returns the Effect (None after reporting
    errors to ctx). Registration order is meaningful: it is the order
    verbs are listed in error messages.
    """

    name: str  # the TOML key ("set", "ramp_to", ...)
    attrs: frozenset[str]  # verb-specific attribute keys
    continuous: bool
    parse: Callable[..., Optional[Effect]]


#: The verb registry, populated by the ``verbs`` package at import time.
VERBS: dict[str, Verb] = {}


def register_verb(verb: Verb) -> None:
    VERBS[verb.name] = verb


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
