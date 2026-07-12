"""Environment models against orbital-mechanics ground truth.

Every check here is against a closed form or a geometric invariant —
Kepler's third law, position ⊥ velocity on a circle, the dipole's
factor-of-two between equator and pole, the cylindrical shadow arc —
never against the model's own output.
"""

import math

import pytest

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.environment import (
    B_EQUATOR,
    MU_EARTH,
    OMEGA_EARTH,
    R_EARTH,
    CircularOrbit,
    Environment,
    reference_rate,
)

LEO = CircularOrbit(altitude=500e3, inclination=math.radians(51.6))


# ---------------------------------------------------------------------------
# Orbit geometry


def test_orbit_rejects_non_positive_altitude():
    with pytest.raises(ValueError, match="altitude"):
        CircularOrbit(altitude=0.0)


def test_keplers_third_law():
    # T = 2*pi*sqrt(r^3/mu): 500 km above the MEAN radius takes ~5669 s.
    r = LEO.radius
    assert LEO.period == pytest.approx(2.0 * math.pi * math.sqrt(r**3 / MU_EARTH))
    assert LEO.period == pytest.approx(5669.0, abs=5.0)


def test_position_stays_on_the_circle_and_velocity_is_tangent():
    for t in (0.0, 137.0, 1500.0, 4321.0):
        r = LEO.position(t)
        v = LEO.velocity_direction(t)
        assert al.v_norm(r) == pytest.approx(LEO.radius)
        assert al.v_norm(v) == pytest.approx(1.0)
        assert abs(al.v_dot(r, v)) < 1e-3  # ⊥ to within rounding at 6.9e6 m
        assert abs(al.v_dot(r, LEO.normal())) < 1e-3


def test_inclination_bounds_the_latitude_excursion():
    peak_z = max(abs(LEO.position(t)[2]) for t in range(0, int(LEO.period), 10))
    assert peak_z == pytest.approx(LEO.radius * math.sin(LEO.inclination), rel=1e-3)


def test_equatorial_orbit_starts_at_the_node():
    orbit = CircularOrbit(altitude=500e3, inclination=0.0)
    assert orbit.position(0.0) == pytest.approx((orbit.radius, 0.0, 0.0))


# ---------------------------------------------------------------------------
# Illumination


def test_sun_visibility_and_shadow_arc():
    env = Environment(orbit=CircularOrbit(altitude=500e3, inclination=0.0))
    # Subsolar point: lit. Antisolar point: dead center of the shadow.
    assert env.sun_visible(0.0)  # position (r, 0, 0), sun +x
    half_period = env.orbit.period / 2.0
    assert not env.sun_visible(half_period)
    # For a sun in the orbit plane the shadow arc is 2*asin(R/r): count it.
    samples = 2000
    lit = sum(env.sun_visible(env.orbit.period * i / samples) for i in range(samples))
    expected_lit = 1.0 - math.asin(R_EARTH / env.orbit.radius) / math.pi
    assert lit / samples == pytest.approx(expected_lit, abs=0.01)


def test_polar_orbit_over_the_terminator_never_eclipses():
    # Orbit plane ⊥ sun line: the spacecraft rides the terminator in
    # permanent sunlight (the dawn-dusk sun-synchronous geometry).
    env = Environment(
        orbit=CircularOrbit(
            altitude=500e3,
            inclination=math.radians(90.0),
            raan=math.radians(90.0),
        )
    )
    assert all(env.sun_visible(env.orbit.period * i / 200) for i in range(200))


# ---------------------------------------------------------------------------
# Magnetic field


def _untilted(orbit):
    return Environment(orbit=orbit, dipole_axis=(0.0, 0.0, -1.0))


def test_dipole_equator_points_north_at_textbook_strength():
    env = _untilted(CircularOrbit(altitude=500e3, inclination=0.0))
    b = env.magnetic_field(0.0)  # position (r, 0, 0), on the dipole equator
    expected = B_EQUATOR * (R_EARTH / env.orbit.radius) ** 3
    assert b[0] == pytest.approx(0.0, abs=1e-12)
    assert b[1] == pytest.approx(0.0, abs=1e-12)
    assert b[2] == pytest.approx(expected)  # northward, ~2.4e-5 T at 500 km


def test_dipole_pole_is_twice_the_equator_and_points_down():
    orbit = CircularOrbit(altitude=500e3, inclination=math.radians(90.0))
    env = _untilted(orbit)
    t_pole = orbit.period / 4.0  # position ~(0, 0, +r)
    b = env.magnetic_field(t_pole)
    expected = 2.0 * B_EQUATOR * (R_EARTH / orbit.radius) ** 3
    assert b[2] == pytest.approx(-expected, rel=1e-6)  # into the north pole
    assert abs(b[0]) < 1e-9 * expected
    assert abs(b[1]) < 1e-9 * expected


def test_tilted_dipole_rotates_with_the_earth():
    env = Environment(orbit=LEO)  # default 11.5-degree tilt
    same_spot_later = env.orbit.period  # orbit repeats, Earth has turned
    b0 = env.magnetic_field(0.0)
    b1 = env.magnetic_field(same_spot_later)
    assert al.v_norm(al.v_sub(b0, b1)) > 0.05 * al.v_norm(b0)


# ---------------------------------------------------------------------------
# Reference attitudes


def test_nadir_attitude_points_z_down_and_x_along_track():
    env = Environment(orbit=LEO)
    for t in (0.0, 700.0, 2900.0):
        q = env.nadir_attitude(t)
        r_hat = al.v_unit(env.orbit.position(t))
        z_body = al.quat_rotate(q, (0.0, 0.0, 1.0))
        x_body = al.quat_rotate(q, (1.0, 0.0, 0.0))
        assert z_body == pytest.approx(al.v_scale(r_hat, -1.0), abs=1e-9)
        assert x_body == pytest.approx(env.orbit.velocity_direction(t), abs=1e-9)


def test_nadir_reference_rate_is_orbit_rate_about_minus_y():
    # LVLH turns once per orbit about the negative body-y axis (which
    # sits on -orbit-normal): the analytic feedforward the finite
    # difference must reproduce.
    env = Environment(orbit=LEO)
    rate = reference_rate(env.nadir_attitude, 1234.0)
    assert rate[0] == pytest.approx(0.0, abs=1e-8)
    assert rate[1] == pytest.approx(-env.orbit.rate, rel=1e-4)
    assert rate[2] == pytest.approx(0.0, abs=1e-8)


def test_sun_attitude_places_the_panel_axis_on_the_sun():
    env = Environment(orbit=LEO, sun_direction=(0.3, -0.8, 0.52))
    for axis in ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
        q = env.sun_attitude(axis)
        pointed = al.quat_rotate(q, axis)
        assert pointed == pytest.approx(env.sun_direction, abs=1e-9)


def test_sun_attitude_is_a_fixed_reference():
    env = Environment(orbit=LEO)
    q = env.sun_attitude((0.0, 0.0, -1.0))
    assert reference_rate(lambda t: q, 0.0) == (0.0, 0.0, 0.0)


def test_target_position_rotates_with_the_earth():
    env = Environment(orbit=LEO)
    assert env.target_position(0.0, 0.0, 0.0) == pytest.approx((R_EARTH, 0.0, 0.0))
    # A quarter sidereal day later the point has moved EASTWARD to +Y —
    # this pins the rotation direction, which a half-day check cannot
    # (cos(+pi) and cos(-pi) coincide).
    quarter_day = (math.pi / 2.0) / OMEGA_EARTH
    assert env.target_position(quarter_day, 0.0, 0.0) == pytest.approx(
        (0.0, R_EARTH, 0.0), abs=1e-3
    )
    half_day = math.pi / OMEGA_EARTH
    assert env.target_position(half_day, 0.0, 0.0) == pytest.approx((-R_EARTH, 0.0, 0.0), abs=1e-3)
    assert env.target_position(0.0, math.pi / 2.0, 0.0) == pytest.approx(
        (0.0, 0.0, R_EARTH), abs=1e-6
    )


def test_dipole_axis_rotates_eastward():
    # Dipole tilted toward +X at t = 0. A quarter sidereal day later the
    # tilt azimuth points +Y; with the orbit phased to sit at (r, 0, 0)
    # at that moment, the field there is -B0'*m_hat, whose y-component is
    # NEGATIVE for eastward rotation (a westward mutation flips its sign).
    quarter_day = (math.pi / 2.0) / OMEGA_EARTH
    orbit = CircularOrbit(
        altitude=500e3,
        inclination=0.0,
        phase0=-CircularOrbit(altitude=500e3).rate * quarter_day,
    )
    env = Environment(orbit=orbit)  # default axis: tilted toward +X
    assert env.orbit.position(quarter_day) == pytest.approx((orbit.radius, 0.0, 0.0), abs=1e-3)
    b = env.magnetic_field(quarter_day)
    expected = B_EQUATOR * (R_EARTH / orbit.radius) ** 3
    tilt = math.radians(11.5)
    assert b[1] == pytest.approx(-expected * math.sin(tilt), rel=1e-6)
    assert b[2] == pytest.approx(expected * math.cos(tilt), rel=1e-6)


def test_target_attitude_points_z_along_the_line_of_sight():
    env = Environment(orbit=LEO)
    lat, lon = math.radians(35.0), math.radians(12.0)
    for t in (0.0, 900.0, 3100.0):
        q = env.target_attitude(t, lat, lon)
        los = al.v_unit(al.v_sub(env.target_position(t, lat, lon), env.orbit.position(t)))
        assert al.quat_rotate(q, (0.0, 0.0, 1.0)) == pytest.approx(los, abs=1e-9)


def test_target_near_the_horizon_still_builds_a_clean_frame():
    # The nastiest yaw geometry: a target far ahead on the equator, near
    # the horizon, with the line of sight closest to the velocity vector.
    # The frame must stay well-formed (the inward radial component of the
    # line of sight guarantees it — documented in target_attitude).
    env = Environment(orbit=CircularOrbit(altitude=500e3, inclination=0.0))
    for t in range(0, 600, 25):
        q = env.target_attitude(float(t), 0.0, math.radians(20.0))
        assert al.quat_norm(q) == pytest.approx(1.0)
