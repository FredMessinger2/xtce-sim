"""
Resolved simulator definition.

`SimDefinition` is the in-memory model the simulator runs from: a flat list of
commands (with opcodes and user parameters) and telemetry packets (with APIDs
and fields). It is built directly from a parsed XTCE file — no generated Python
source is required to run.

    definition = SimDefinition.from_xtce("spacecraft.xml")

Use `xtce_sim.generate` to dump a definition to disk (cmd_tlm.txt / cmd_tlm.json)
or emit an optional importable snapshot (generated.py).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ParamInfo:
    """A user-configurable command argument."""

    name: str
    size_bits: int
    python_type: str  # 'uint8', 'int16', 'float32', 'string', ...
    unit: Optional[str] = None
    description: Optional[str] = None
    valid_min: Optional[float] = None
    valid_max: Optional[float] = None
    enumerations: Optional[dict[str, int]] = None


@dataclass
class CalibratorInfo:
    """Raw-count → engineering-unit conversion for one telemetry field.

    Exactly one of the two forms is populated: polynomial ``coefficients``
    as (coefficient, exponent) pairs, or piecewise-linear ``spline_points``
    as (raw, calibrated) pairs sorted by raw. Spline evaluation clamps to
    the end points outside the declared range (XTCE's no-extrapolation
    default). The wire always carries the raw counts; this is a view.
    """

    coefficients: list[tuple[float, int]] = field(default_factory=list)
    spline_points: list[tuple[float, float]] = field(default_factory=list)

    def apply(self, raw: float) -> float:
        """The engineering value for a raw wire count."""
        if self.spline_points:
            return self._apply_spline(raw)
        # Negative exponents are rejected at every ingress (parser, from_dict),
        # so raw=0 can never hit a division by zero here.
        return float(sum(c * raw**e for c, e in self.coefficients))

    def _apply_spline(self, raw: float) -> float:
        pts = self.spline_points
        if math.isnan(raw):
            return raw  # propagate — never dress NaN up as a reading
        if raw <= pts[0][0]:
            return pts[0][1]
        if raw >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= raw <= x1:
                return y0 if x1 == x0 else y0 + (y1 - y0) * (raw - x0) / (x1 - x0)
        return pts[-1][1]  # total: the between-clamps loop always matches

    def invert(self, engineering: float) -> Optional[float]:
        """The raw count that calibrates to *engineering*, when well-defined.

        Covers the affine polynomial case and monotonic splines (clamped to
        the table ends); returns None when there is no unique inverse
        (higher-order polynomials, non-monotonic splines).
        """
        if self.spline_points:
            return self._invert_spline(engineering)
        by_exp: dict[int, float] = {}
        for c, e in self.coefficients:
            by_exp[e] = by_exp.get(e, 0.0) + c
        linear = by_exp.get(1, 0.0)
        if linear and all(e in (0, 1) for e in by_exp):
            return (engineering - by_exp.get(0, 0.0)) / linear
        return None

    def _invert_spline(self, engineering: float) -> Optional[float]:
        pts = self.spline_points
        cals = [c for _, c in pts]
        ascending = all(a < b for a, b in zip(cals, cals[1:]))
        descending = all(a > b for a, b in zip(cals, cals[1:]))
        if not (ascending or descending):
            return None
        first, last = pts[0], pts[-1]
        past_first = engineering <= first[1] if ascending else engineering >= first[1]
        past_last = engineering >= last[1] if ascending else engineering <= last[1]
        if past_first:
            return first[0]
        if past_last:
            return last[0]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if min(y0, y1) <= engineering <= max(y0, y1):
                if y1 == y0:
                    return x0
                return x0 + (x1 - x0) * (engineering - y0) / (y1 - y0)
        return None

    @classmethod
    def from_dict(cls, data: dict) -> Optional["CalibratorInfo"]:
        """A CalibratorInfo from its JSON form, or None if it is empty.

        Negative-exponent polynomial terms are dropped (mirroring the
        parser): they would make a raw count of 0 undefined.
        """
        cal = cls(
            coefficients=[
                (float(c), int(e))
                for c, e in data.get("coefficients") or []
                if int(e) >= 0
            ],
            spline_points=sorted(
                (float(r), float(v)) for r, v in data.get("spline_points") or []
            ),
        )
        return cal if (cal.coefficients or cal.spline_points) else None


@dataclass
class FieldInfo:
    """A single field in a telemetry packet payload."""

    name: str
    size_bits: int
    python_type: str  # 'uint8', 'int16', 'float32', 'string', ...
    unit: Optional[str] = None
    description: Optional[str] = None
    enumerations: Optional[dict[str, int]] = None  # label -> raw value
    calibrator: Optional[CalibratorInfo] = None  # raw counts -> engineering units


@dataclass
class CommandDef:
    """A concrete command: opcode + the arguments an operator can set."""

    name: str
    opcode: int
    description: Optional[str] = None
    synthetic: bool = False  # opcode assigned by us, not present in the XTCE
    params: list[ParamInfo] = field(default_factory=list)


@dataclass
class PacketDef:
    """A concrete telemetry packet: APID + payload fields."""

    name: str
    apid: int
    description: Optional[str] = None
    fields: list[FieldInfo] = field(default_factory=list)
    struct_format: str = ">"  # big-endian struct format for the payload


@dataclass
class SimDefinition:
    """Everything the simulator needs to serve one satellite, resolved in memory."""

    space_system_name: str
    commands: list[CommandDef] = field(default_factory=list)
    packets: list[PacketDef] = field(default_factory=list)

    # ---- lookups -----------------------------------------------------------

    def command_by_name(self, name: str) -> Optional[CommandDef]:
        return next((c for c in self.commands if c.name == name), None)

    def command_by_opcode(self, opcode: int) -> Optional[CommandDef]:
        return next((c for c in self.commands if c.opcode == opcode), None)

    def packet_by_name(self, name: str) -> Optional[PacketDef]:
        return next((p for p in self.packets if p.name == name), None)

    def packet_by_apid(self, apid: int) -> Optional[PacketDef]:
        return next((p for p in self.packets if p.apid == apid), None)

    # ---- construction ------------------------------------------------------

    @classmethod
    def from_xtce(cls, xtce: str | Path | list) -> "SimDefinition":
        """Parse one or more XTCE files and build a resolved SimDefinition.

        Accepts a single path or a list of paths. Multiple files are merged
        additively (later files override earlier ones), matching the parser's
        `parse_multiple` semantics.
        """
        # Imported here to avoid a circular import (generate imports this module).
        from xtce_sim.generate import build_sim_definition
        from xtce_sim.parser import XTCEParser

        paths = [xtce] if isinstance(xtce, (str, Path)) else list(xtce)
        if not paths:
            raise ValueError("At least one XTCE file is required")

        parser = XTCEParser()
        if len(paths) == 1:
            xtce_def = parser.parse(paths[0])
        else:
            xtce_def = parser.parse_multiple(paths)

        return build_sim_definition(xtce_def)

    @classmethod
    def from_dict(cls, data: dict) -> "SimDefinition":
        """Reconstruct a SimDefinition from a `generate.to_dict` mapping.

        This lets a client rebuild the definition from a dumped cmd_tlm.json
        without re-parsing the source XTCE.
        """
        commands = [
            CommandDef(
                name=c["name"],
                opcode=c["opcode"],
                description=c.get("description"),
                synthetic=c.get("synthetic_opcode", False),
                params=[
                    ParamInfo(
                        name=p["name"],
                        size_bits=p["size_bits"],
                        python_type=p["python_type"],
                        unit=p.get("unit"),
                        description=p.get("description"),
                        valid_min=p.get("valid_min"),
                        valid_max=p.get("valid_max"),
                        enumerations=p.get("enumerations"),
                    )
                    for p in c.get("params", [])
                ],
            )
            for c in data.get("commands", [])
        ]
        packets = [
            PacketDef(
                name=t["name"],
                apid=t["apid"],
                description=t.get("description"),
                fields=[
                    FieldInfo(
                        name=f["name"],
                        size_bits=f["size_bits"],
                        python_type=f["python_type"],
                        unit=f.get("unit"),
                        description=f.get("description"),
                        enumerations=f.get("enumerations"),
                        calibrator=(
                            CalibratorInfo.from_dict(f["calibrator"])
                            if f.get("calibrator")
                            else None
                        ),
                    )
                    for f in t.get("fields", [])
                ],
                struct_format=t.get("struct_format", ">"),
            )
            for t in data.get("telemetry", [])
        ]
        return cls(
            space_system_name=data.get("space_system", "Unknown"),
            commands=commands,
            packets=packets,
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "SimDefinition":
        """Load a SimDefinition from a dumped cmd_tlm.json file."""
        return cls.from_dict(json.loads(Path(path).read_text()))
