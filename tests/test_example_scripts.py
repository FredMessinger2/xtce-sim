"""The example shell scripts must stay true to the definitions they drive.

set_all_fields.sh is a wall of `send` lines; nothing at runtime checks it
until a user runs it against a live sim. This cross-validates every send in
the script against the shipped XTCE: the command exists, every argument
name is real, and the values encode into a packet.
"""

import re
import shlex
from pathlib import Path

import pytest

from xtce_sim import codec
from xtce_sim.definition import SimDefinition

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
SCRIPT = EXAMPLES / "imaging_sat/set_all_fields.sh"


def _script_sends() -> list[tuple[str, dict]]:
    sends = []
    for line in SCRIPT.read_text().splitlines():
        line = line.strip()
        if not re.match(r"^send\s", line):
            continue
        tokens = shlex.split(line)[1:]  # drop the `send` function name
        name, pairs = tokens[0], tokens[1:]
        args = dict(pair.split("=", 1) for pair in pairs)
        sends.append((name, args))
    return sends


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(EXAMPLES / "imaging_sat/imaging_sat.xml")


def test_script_has_a_meaningful_number_of_sends():
    assert len(_script_sends()) >= 15


def test_every_script_send_encodes_against_the_definition(simdef):
    for name, args in _script_sends():
        command = simdef.command_by_name(name)
        assert command is not None, f"script sends unknown command {name!r}"
        param_names = {p.name for p in command.params}
        unknown = set(args) - param_names
        assert not unknown, f"{name}: unknown arg(s) {sorted(unknown)}"
        # The same encode path `xtce-sim send` uses; raises on a bad value.
        codec.encode_command(command, args)


def test_script_reaches_every_command_wired_in_the_sidecar(simdef):
    """Every command the behavior files wire must appear in the script —
    if someone adds a new effect, the set-all-fields sweep must not
    silently fall behind."""
    from xtce_sim.behavior import load_behavior

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    scripted = {name for name, _ in _script_sends()}
    missing = set(spec.commands) - scripted
    assert not missing, f"sidecar-wired commands absent from the script: {sorted(missing)}"
