"""Shared test scaffolding: the repo paths and the one shipped vehicle.

The example satellite is a deliberate test fixture — CI proves on every
commit that the thing a new user runs first parses, validates, and flies
(see the shipped-sidecar pins in test_behavior). These are the single
definitions of where it lives; test modules import them instead of each
declaring their own copy.

``simdef`` is the shared imaging_sat definition, parsed once per session:
SimDefinition is immutable in practice (consumers copy what they change),
so every module that used to parse its own copy shares this one. A module
that tests a DIFFERENT vehicle (my_vehicle, the synthetic edge-case
files) declares its own ``simdef`` fixture, which shadows this one.
"""

from pathlib import Path

import pytest

from xtce_sim.definition import SimDefinition

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"
IMAGING = EXAMPLES / "imaging_sat/imaging_sat.xml"


@pytest.fixture(scope="session")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(IMAGING)
