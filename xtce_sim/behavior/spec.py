"""Shared behavior vocabulary: the verb registry, effect bases, and spec.

Each verb module under ``verbs/`` owns everything about its verb — the
effect dataclass, the TOML parsing, the runtime math — and declares a
``Verb`` entry that ``verbs/__init__`` registers here. The loader and
engine dispatch through the registry and the effect/active base classes,
so adding a verb never touches them. The full DSL documentation is the
package docstring (``xtce_sim/behavior/__init__.py``).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Callable, ClassVar, Optional

from xtce_sim.dynamics.model import AdcsModelConfig

_TEMPLATE_RE = re.compile(r"\{(\w+)\}")

#: Sentinel returned by ``InstantEffect.value_for`` to skip an effect the
#: implementation has already warned about. Distinct from None, which is a
#: real (unstorable) value that must still reach ``_store`` so the skip is
#: logged there — every skipped effect leaves a trace somewhere.
_SKIP = object()

Scalar = int | float | bool | str


class BehaviorError(ValueError):
    """A behavior file failed validation; the message lists every problem."""


@dataclass
class Effect:
    """Base of every verb's effect (the validated, loadable form).

    Declares the structure every effect shares — the (possibly templated)
    target ``field`` and the universal ``emit`` attribute — so verb
    modules only add their own operands. Verbs subclass InstantEffect or
    ContinuousEffect and implement the respective hooks; ``describe()``
    is the narration line, without the universal emit tail.
    """

    field: str  # possibly templated
    emit: str = dc_field(default="interval", kw_only=True)

    continuous: ClassVar[bool] = False

    def describe(self) -> str:
        raise NotImplementedError


@dataclass
class InstantEffect(Effect):
    """An effect applied once when its command executes (set/copy/increment)."""

    def value_for(self, engine, command, args, where, fname):
        """The value to store for *fname*, or the ``_SKIP`` sentinel to skip
        this effect (the implementation has already warned about why)."""
        raise NotImplementedError


@dataclass
class ContinuousEffect(Effect):
    """A tick-driven behavior the engine registers and advances.

    All continuous verbs accept ``noise`` (gaussian stddev added to the
    emitted value), so it lives here.
    """

    noise: float = dc_field(default=0.0, kw_only=True)

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

    def _noise_suffix(self) -> str:
        return f" ±noise({self.noise})" if self.noise else ""


@dataclass
class ActiveBehavior:
    """Base of a running continuous behavior (the engine registry entry).

    Declares the state every active shares: the resolved ``field``, the
    ``noise``/``rng`` pair driving ``engine._noisy``, and the ``warned``
    once-per-behavior latch driving ``engine._live_number``. Subclasses
    implement ``advance(engine, fname, dt)`` — *fname* is the engine
    registry key the behavior runs under, and is what stores and
    retirements must use (``self.field`` is narration/warning identity).
    """

    field: str
    noise: float = dc_field(default=0.0, kw_only=True)
    warned: bool = dc_field(default=False, kw_only=True)
    rng: Optional[random.Random] = dc_field(default=None, kw_only=True)

    def advance(self, engine, fname: str, dt: float) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class Verb:
    """One DSL verb: its grammar row and its parser.

    ``parse(where, fname, spec, command, emit, ctx)`` validates the
    verb-specific attributes and returns the Effect (None after reporting
    errors to ctx). ``continuous`` must match the ``continuous`` flag of
    every Effect class the parser returns — the loader gates on the Verb,
    the engine on the Effect (pinned by test). Registration order is
    meaningful: it is the order verbs are listed in error messages.
    """

    name: str  # the TOML key ("set", "ramp_to", ...)
    attrs: frozenset[str]  # verb-specific attribute keys
    continuous: bool
    parse: Callable[..., Optional[Effect]]


#: The verb registry, populated by the ``verbs`` package at import time.
#: Late registration (a new verb from outside the package) is supported:
#: the loader derives its key sets from the live dict.
VERBS: dict[str, Verb] = {}


def register_verb(verb: Verb, *, replace: bool = False) -> None:
    """Add a verb to the registry; a duplicate name is refused.

    ``replace`` is for the code that owns the name — the verbs package
    re-executing its registration loop (importlib.reload) must be
    idempotent, while an outside registration colliding with a built-in
    stays an error.
    """
    if not replace and verb.name in VERBS:
        raise ValueError(f"verb {verb.name!r} is already registered")
    VERBS[verb.name] = verb


@dataclass
class BehaviorSpec:
    """Validated behavior: initial values, boot signals, command effects."""

    path: Path  # the source as given: a satellite directory or one file
    initial: dict[str, Scalar]
    commands: dict[str, list[Effect]]  # command name -> effects
    # Continuous behaviors started at boot.
    signals: list[Effect] = dc_field(default_factory=list)
    files: list[Path] = dc_field(default_factory=list)  # the merged .toml files
    # Physics models declared under [_models]: each owns its output fields
    # (no other table may write them) and consumes its bound commands.
    models: list[AdcsModelConfig] = dc_field(default_factory=list)

    @property
    def source_label(self) -> str:
        """Human label: 'dir/ (3 files)' for a directory, the path for a file."""
        if len(self.files) > 1 or (self.path and Path(self.path).is_dir()):
            return f"{self.path} ({len(self.files)} file(s))"
        return str(self.path)
