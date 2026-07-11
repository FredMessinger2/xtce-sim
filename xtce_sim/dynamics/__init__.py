"""
Dynamics models — real physics behind the behavior engine.

This package holds the purpose-built rigid-body machinery for the ADCS
dynamics arc: pure-Python vector/quaternion algebra, the spacecraft plant,
attitude control, and environment models. No numpy, no external physics
engine — at simulator rates (one rigid body, a handful of substeps per
beacon) plain tuples of floats cost nothing measurable, and the project
keeps its pip-installable, three-small-dependencies footprint.
"""
