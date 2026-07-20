"""The DSL's verbs, one module each; importing this package registers them.

Registration is explicit and ordered here — not a side effect of module
import — because the order is part of the loader's pinned error text
("exactly one of set/increment/ramp_to/oscillate/hold required").
"""

from xtce_sim.behavior.spec import register_verb
from xtce_sim.behavior.verbs.hold import VERB as _HOLD
from xtce_sim.behavior.verbs.hold import HoldEffect
from xtce_sim.behavior.verbs.increment import VERB as _INCREMENT
from xtce_sim.behavior.verbs.increment import IncrementEffect
from xtce_sim.behavior.verbs.oscillate import VERB as _OSCILLATE
from xtce_sim.behavior.verbs.oscillate import OscillateEffect
from xtce_sim.behavior.verbs.ramp import VERB as _RAMP
from xtce_sim.behavior.verbs.ramp import RampEffect
from xtce_sim.behavior.verbs.set import VERB as _SET
from xtce_sim.behavior.verbs.set import CopyArgEffect, SetEffect, scalar_effect

for _verb in (_SET, _INCREMENT, _RAMP, _OSCILLATE, _HOLD):
    register_verb(_verb)

__all__ = [
    "CopyArgEffect",
    "HoldEffect",
    "IncrementEffect",
    "OscillateEffect",
    "RampEffect",
    "SetEffect",
    "scalar_effect",
]
