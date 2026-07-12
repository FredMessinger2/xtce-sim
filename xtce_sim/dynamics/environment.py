"""
Orbital environment: where the spacecraft is, what it sees, what acts on it.

Everything here is a function of time since epoch (seconds), deterministic
and side-effect free, in an Earth-centered inertial frame (Z along the
rotation axis, X/Y equatorial). Honest simplifications, each chosen
because its error is invisible from a ground console and each recorded
here rather than hidden:

- The orbit is CIRCULAR and unperturbed (no J2 drift, no drag decay).
- The sun direction is FIXED in inertial space (it really moves ~1°/day;
  over a simulation session that is noise).
- Eclipse is the CYLINDRICAL Earth shadow (no penumbra).
- The magnetic field is a TILTED CENTERED DIPOLE (no IGRF harmonics),
  rotating with the Earth like the real geomagnetic field does.
- Ground targets sit on a spherical Earth of radius R_EARTH and rotate
  with it at OMEGA_EARTH.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from xtce_sim.dynamics import algebra as al

MU_EARTH = 3.986004418e14  # m³/s², WGS-84 gravitational parameter
R_EARTH = 6.371e6  # m, mean radius
OMEGA_EARTH = 7.2921159e-5  # rad/s, sidereal rotation rate
B_EQUATOR = 3.12e-5  # T, dipole field magnitude at the equatorial surface
DIPOLE_TILT = math.radians(11.5)  # geomagnetic axis tilt from the spin axis


@dataclass(frozen=True)
class CircularOrbit:
    """A circular orbit fixed by altitude, inclination, RAAN, and phase."""

    altitude: float  # m above R_EARTH
    inclination: float = math.radians(51.6)
    raan: float = 0.0  # right ascension of the ascending node, rad
    phase0: float = 0.0  # argument of latitude at t = 0, rad

    def __post_init__(self) -> None:
        if self.altitude <= 0.0:
            raise ValueError("altitude must be positive")

    @property
    def radius(self) -> float:
        return R_EARTH + self.altitude

    @property
    def rate(self) -> float:
        """Orbital angular rate n = sqrt(mu/r³), rad/s."""
        return math.sqrt(MU_EARTH / self.radius**3)

    @property
    def period(self) -> float:
        return 2.0 * math.pi / self.rate

    def _basis(self) -> tuple[al.Vec3, al.Vec3, al.Vec3]:
        """In-plane axes p̂ (toward the ascending node), q̂ (90° ahead),
        and the orbit normal ĥ, all in ECI."""
        co, so = math.cos(self.raan), math.sin(self.raan)
        ci, si = math.cos(self.inclination), math.sin(self.inclination)
        p = (co, so, 0.0)
        q = (-so * ci, co * ci, si)
        h = (so * si, -co * si, ci)
        return p, q, h

    def position(self, t: float) -> al.Vec3:
        """Spacecraft position in ECI, meters."""
        p, q, _ = self._basis()
        u = self.phase0 + self.rate * t
        return al.v_add(
            al.v_scale(p, self.radius * math.cos(u)),
            al.v_scale(q, self.radius * math.sin(u)),
        )

    def velocity_direction(self, t: float) -> al.Vec3:
        """Unit along-track direction (exactly ⊥ position on a circle)."""
        p, q, _ = self._basis()
        u = self.phase0 + self.rate * t
        return al.v_add(al.v_scale(p, -math.sin(u)), al.v_scale(q, math.cos(u)))

    def normal(self) -> al.Vec3:
        """Orbit normal ĥ (constant for an unperturbed orbit)."""
        return self._basis()[2]


def _complete_frame(z: al.Vec3) -> tuple[al.Vec3, al.Vec3]:
    """Deterministic x, y completing unit vector `z` to a right-handed
    orthonormal triad. The yaw choice is arbitrary but stable: helper
    vector Z_eci unless z is nearly polar, then X_eci."""
    helper = (0.0, 0.0, 1.0) if abs(z[2]) < 0.9 else (1.0, 0.0, 0.0)
    y = al.v_unit(al.v_cross(z, helper))
    x = al.v_cross(y, z)
    return x, y


@dataclass(frozen=True)
class Environment:
    """The world model: orbit, sun, magnetic field, and the reference
    attitudes the pointing modes track."""

    orbit: CircularOrbit
    sun_direction: al.Vec3 = (1.0, 0.0, 0.0)  # ECI unit vector, fixed
    # Earth's dipole moment axis points roughly toward geographic SOUTH
    # (that is what makes the surface field point north); tilted 11.5°.
    dipole_axis: al.Vec3 = (
        math.sin(DIPOLE_TILT),
        0.0,
        -math.cos(DIPOLE_TILT),
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "sun_direction", al.v_unit(self.sun_direction))
        object.__setattr__(self, "dipole_axis", al.v_unit(self.dipole_axis))

    # -- illumination ---------------------------------------------------------

    def sun_visible(self, t: float) -> bool:
        """False inside the cylindrical Earth shadow."""
        r = self.orbit.position(t)
        along = al.v_dot(r, self.sun_direction)
        if along >= 0.0:
            return True  # sunward of the terminator plane
        perp = al.v_sub(r, al.v_scale(self.sun_direction, along))
        return al.v_norm(perp) >= R_EARTH

    # -- magnetic field -------------------------------------------------------

    def magnetic_field(self, t: float) -> al.Vec3:
        """Dipole field at the spacecraft position, ECI, Tesla.

        The geomagnetic axis rotates with the Earth about +Z, so the field
        a spacecraft sees varies over both the orbit and the day — which
        is exactly what B-dot detumbling and momentum dumping live on.
        """
        r = self.orbit.position(t)
        r_mag = al.v_norm(r)
        r_hat = al.v_scale(r, 1.0 / r_mag)
        rot = OMEGA_EARTH * t
        c, s = math.cos(rot), math.sin(rot)
        ax, ay, az = self.dipole_axis
        m_hat = (c * ax - s * ay, s * ax + c * ay, az)
        strength = B_EQUATOR * (R_EARTH / r_mag) ** 3
        return al.v_scale(
            al.v_sub(al.v_scale(r_hat, 3.0 * al.v_dot(m_hat, r_hat)), m_hat),
            strength,
        )

    # -- reference attitudes --------------------------------------------------

    def nadir_attitude(self, t: float) -> al.Quat:
        """LVLH: body +Z at nadir, +X along-track, +Y = −orbit normal."""
        r = self.orbit.position(t)
        z = al.v_scale(al.v_unit(r), -1.0)
        x = self.orbit.velocity_direction(t)
        y = al.v_cross(z, x)
        return al.frame_to_quat(x, y, z)

    def sun_attitude(self, sun_axis_body: al.Vec3) -> al.Quat:
        """An attitude placing the body `sun_axis_body` on the sun line.
        The rotation about the sun line is free; the choice here is
        deterministic (see _complete_frame) and constant, so sun-safe
        mode holds still rather than wandering in yaw."""
        axis = al.v_unit(sun_axis_body)
        sx, sy = _complete_frame(self.sun_direction)
        q_ref = al.frame_to_quat(sx, sy, self.sun_direction)
        bx, by = _complete_frame(axis)
        q_body = al.frame_to_quat(bx, by, axis)
        return al.quat_multiply(q_ref, al.quat_conjugate(q_body))

    def target_position(self, t: float, lat: float, lon: float) -> al.Vec3:
        """A ground target on the spherical rotating Earth, ECI, meters.
        lat/lon in radians; longitude is measured from the ECI X axis at
        t = 0 (the sim has no calendar, so there is no GMST offset)."""
        angle = lon + OMEGA_EARTH * t
        cl = math.cos(lat)
        return al.v_scale((cl * math.cos(angle), cl * math.sin(angle), math.sin(lat)), R_EARTH)

    def target_attitude(self, t: float, lat: float, lon: float) -> al.Quat:
        """Body +Z along the line of sight to the target; +X as close to
        along-track as the geometry allows. No horizon check: like the
        real article, the tracker points where it is told.

        The along-track yaw choice can never degenerate: a ground target
        seen from orbit always lies strictly below the local horizontal
        (the line of sight has an inward radial component of at least
        (radius − R_EARTH)/range), while the velocity direction is exactly
        horizontal — so the two are never parallel.
        """
        sat = self.orbit.position(t)
        los = al.v_unit(al.v_sub(self.target_position(t, lat, lon), sat))
        v = self.orbit.velocity_direction(t)
        x = al.v_unit(al.v_sub(v, al.v_scale(los, al.v_dot(v, los))))
        y = al.v_cross(los, x)
        return al.frame_to_quat(x, y, los)


def reference_rate(attitude_fn: Callable[[float], al.Quat], t: float, h: float = 0.5) -> al.Vec3:
    """Body-frame angular rate of a moving reference attitude at time t,
    by forward finite difference over [t, t+h] — the feedforward the
    controller's hold law wants. h trades truncation against rounding;
    0.5 s is ~5e-4 rad of rotation at orbit rate, comfortably in the
    accurate middle (the h/2 lag is ~1e-5 rad/s even for target-track's
    fastest pass geometry)."""
    err = al.quat_error(attitude_fn(t + h), attitude_fn(t))
    angle = al.quat_angle(err)
    if angle < 1e-12:
        return (0.0, 0.0, 0.0)
    axis = al.v_unit((err[0], err[1], err[2]))
    return al.v_scale(axis, angle / h)
