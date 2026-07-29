"""Population satellites: a propagated catalog beside the hero sims.

The bridge serves two kinds of satellite. A *hero* satellite is a full
simulation in its own process — XTCE-defined telemetry, commands, the
whole vehicle — and the bridge connects to it and decodes what it sends.
A *population* satellite is what this module provides: an orbit and a
name, propagated inside the bridge process itself. There is no telemetry
and nothing to connect to; the bridge computes where each one is and
publishes that. On the viewer the two are indistinguishable, which is
the point — a tracked foreign satellite does not send you telemetry
either.

A ``[[constellations]]`` entry in the bridge roster describes a whole
family at once: how many satellites, how many orbital planes, and one
orbit spelled exactly like a model's ``[orbit]`` table (``altitude_km``
for a circle, ``perigee_km``/``apogee_km`` for an ellipse). The planes
are spaced evenly in right ascension around the Earth's axis and the
satellites evenly in phase within each plane — the standard even-spacing
(Walker-style) arrangement real constellations use. ``raan_deg`` and
``phase_deg``, when given, rotate the whole pattern so two
constellations can interleave instead of overlapping.

Honesty note: population satellites fly pure, unperturbed two-body
orbits. Nothing here maneuvers yet; a future stage will apply maneuvers
to chosen satellites so that pattern-of-life analysis has something real
to detect. This stage builds the crowd those maneuvers will hide in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from xtce_sim.dynamics.environment import KeplerianOrbit
from xtce_sim.dynamics.model import parse_orbit

#: Default seconds between updates for one population satellite. The
#: viewer's ground segment declares a satellite lost when it has heard
#: nothing for its time-to-live (15 s by default in molniya-viewer), so
#: the period must stay comfortably inside that window.
DEFAULT_UPDATE_PERIOD_S = 5.0

_ORBIT_KEYS = {
    "altitude_km", "perigee_km", "apogee_km", "argp_deg",
    "inclination_deg", "raan_deg", "phase_deg",
}
_KNOWN_KEYS = _ORBIT_KEYS | {
    "nation", "name_prefix", "sat_id_prefix", "count", "planes",
    "update_period_s", "color", "pixel_size", "fov_half_angle_deg",
    "cone_enabled", "label_enabled",
}


@dataclass(frozen=True)
class PopulationSat:
    """One catalog entry: an identity and the orbit it flies."""

    sat_id: str
    name: str
    orbit: KeplerianOrbit
    display: dict


@dataclass(frozen=True)
class Constellation:
    """A family of population satellites sharing one orbit shape."""

    name_prefix: str
    nation: str | None
    update_period_s: float
    sats: tuple[PopulationSat, ...]

    def describe(self) -> str:
        """The roster line: name, headcount, and the shared orbit."""
        return f"{self.name_prefix} x{len(self.sats)} ({self.sats[0].orbit.describe()})"


def state_km(sat: PopulationSat, t: float) -> tuple[dict, dict]:
    """Position (km) and velocity (km/s) dicts in the contract's shape."""
    pos = sat.orbit.position(t)
    vel = sat.orbit.velocity(t)
    return (
        {"x": pos[0] / 1000.0, "y": pos[1] / 1000.0, "z": pos[2] / 1000.0},
        {"vx": vel[0] / 1000.0, "vy": vel[1] / 1000.0, "vz": vel[2] / 1000.0},
    )


def parse_display(entry: dict, where: str, err) -> dict:
    """The presentation hints shared by both roster entry kinds: color,
    dot size, and the sensor-cone half-angle. The viewer draws a default
    cone for any satellite that sends no hint, so the roster must be able
    to say what the cone is — omitting the keys here surrenders the cone
    width to the viewer's default, it does not remove the cone.
    ``cone_enabled = false`` and ``label_enabled = false`` are the
    contract's off switches (sensor cone, name text), published
    verbatim; the configured width stays in the file for an easy flip
    back to true."""
    display = {}
    color = entry.get("color")
    if color is not None:
        display["color"] = str(color)
    pixel_size = entry.get("pixel_size")
    if pixel_size is not None:
        if isinstance(pixel_size, bool) or not isinstance(pixel_size, int) or pixel_size < 1:
            err(f"{where}: pixel_size must be a positive integer")
        else:
            display["pixel_size"] = pixel_size
    fov = entry.get("fov_half_angle_deg")
    if fov is not None:
        if isinstance(fov, bool) or not isinstance(fov, (int, float)) or not 0.0 < fov <= 90.0:
            err(f"{where}: fov_half_angle_deg must be a number in (0, 90]")
        else:
            display["fov_half_angle_deg"] = float(fov)
    for switch in ("cone_enabled", "label_enabled"):
        value = entry.get(switch)
        if value is not None:
            if not isinstance(value, bool):
                err(f"{where}: {switch} must be true or false")
            else:
                display[switch] = value
    return display


def parse_constellation(entry, n: int, err) -> Constellation | None:
    """One ``[[constellations]]`` entry -> a fully spread constellation.

    Strict and total like every config parser here: unknown keys are
    refused and every problem is reported through ``err``.
    """
    where = f"constellations #{n}"
    if not isinstance(entry, dict):
        err(f"{where}: must be a table")
        return None
    for key in sorted(set(entry) - _KNOWN_KEYS):
        err(f"{where}: unknown key {key!r}")

    name_prefix = entry.get("name_prefix")
    if not isinstance(name_prefix, str) or not name_prefix:
        err(f"{where}: name_prefix must be a non-empty string")
        return None
    sat_id_prefix = entry.get("sat_id_prefix", name_prefix)
    if not isinstance(sat_id_prefix, str) or not sat_id_prefix:
        err(f"{where}: sat_id_prefix must be a non-empty string")
        return None
    nation = entry.get("nation")
    if nation is not None and not isinstance(nation, str):
        err(f"{where}: nation must be a string")
        return None

    count = _positive_int(entry.get("count"), f"{where}: count", err)
    planes = _positive_int(entry.get("planes", 1), f"{where}: planes", err)
    period = _positive_number(
        entry.get("update_period_s", DEFAULT_UPDATE_PERIOD_S),
        f"{where}: update_period_s",
        err,
    )
    display = parse_display(entry, where, err)
    base = _parse_base_orbit(entry, where, err)
    if count is None or planes is None or period is None or base is None:
        return None
    if count % planes != 0:
        err(f"{where}: count ({count}) must divide evenly among planes ({planes})")
        return None

    return Constellation(
        name_prefix=name_prefix,
        nation=nation,
        update_period_s=period,
        sats=_spread(base, count, planes, name_prefix, sat_id_prefix, display),
    )


def _spread(
    base: KeplerianOrbit,
    count: int,
    planes: int,
    name_prefix: str,
    sat_id_prefix: str,
    display: dict,
) -> tuple[PopulationSat, ...]:
    """Even spacing: planes around the axis, satellites around each plane.

    The base orbit's ``raan`` and ``mean_anomaly0`` (the roster's
    ``raan_deg``/``phase_deg``) survive as a whole-pattern rotation."""
    per_plane = count // planes
    width = max(3, len(str(count)))
    sats = []
    for i in range(count):
        plane, slot = divmod(i, per_plane)
        orbit = replace(
            base,
            raan=base.raan + 2.0 * math.pi * plane / planes,
            mean_anomaly0=base.mean_anomaly0 + 2.0 * math.pi * slot / per_plane,
        )
        number = f"{i + 1:0{width}d}"
        sats.append(
            PopulationSat(
                sat_id=f"{sat_id_prefix}-{number}",
                name=f"{name_prefix}-{number}",
                orbit=orbit,
                display=display,
            )
        )
    return tuple(sats)


def _parse_base_orbit(entry: dict, where: str, err) -> KeplerianOrbit | None:
    """The shared orbit, spelled flat in the entry (no nested table).

    ``parse_orbit`` prefixes its messages with ``.orbit`` for the model
    configs' nested table; the roster spelling is flat, so that suffix is
    stripped back out of every message."""
    table = {key: entry[key] for key in _ORBIT_KEYS if key in entry}
    return parse_orbit(table, where, lambda msg: err(msg.replace(f"{where}.orbit", where, 1)))


def _positive_int(value, where: str, err) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        err(f"{where} must be a positive integer")
        return None
    return value


def _positive_number(value, where: str, err) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        err(f"{where} must be a positive number")
        return None
    return float(value)
