"""The DSL's verbs, one module each; importing this package registers them.

Registration is explicit and ordered here — not a side effect of module
import — because the order is part of the loader's pinned error text
("exactly one of set/increment/ramp_to/oscillate/hold required").
"""

from xtce_sim.behavior.spec import register_verb
from xtce_sim.behavior.verbs import hold, increment, oscillate, ramp
from xtce_sim.behavior.verbs import set as set_
from xtce_sim.behavior.verbs.hold import HoldEffect, _ActiveHold
from xtce_sim.behavior.verbs.increment import IncrementEffect
from xtce_sim.behavior.verbs.oscillate import OscillateEffect, _ActiveOsc, _wave
from xtce_sim.behavior.verbs.ramp import RampEffect, _ActiveRamp
from xtce_sim.behavior.verbs.set import CopyArgEffect, SetEffect, scalar_effect

for _verb_module in (set_, increment, ramp, oscillate, hold):
    register_verb(_verb_module.VERB)

__all__ = [
    "CopyArgEffect",
    "HoldEffect",
    "IncrementEffect",
    "OscillateEffect",
    "RampEffect",
    "SetEffect",
    "scalar_effect",
    "_ActiveHold",
    "_ActiveOsc",
    "_ActiveRamp",
    "_wave",
]
