"""End-to-end ADCS: sensors → estimator → mode → torque → real motion.

These run the full closed loop — the controller flies on ESTIMATES while
the assertions check TRUTH — over hundreds of simulated seconds, so what
passes here is what a ground operator will actually watch: nadir lock
held through the orbit, a ground target tracked through Earth rotation,
tumble momentum bled out through the magnetorquers, wheel momentum
dumped while the pointing barely moves.
"""

import math

import pytest

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.control import AttitudeController, ControlLaw, PDGains
from xtce_sim.dynamics.environment import CircularOrbit, Environment
from xtce_sim.dynamics.modes import AdcsMode, Magnetorquer, ModeMachine, latlon_degrees
from xtce_sim.dynamics.plant import Plant, PlantState, WheelParams
from xtce_sim.dynamics.sensors import EstimatorState

INERTIA = al.m_diag(12.0, 14.0, 9.0)
PYRAMID = tuple(
    WheelParams(axis=ax, inertia=0.02, max_torque=0.05, max_speed=600.0)
    for ax in [
        (0.6, 0.0, 0.8),
        (-0.6, 0.0, 0.8),
        (0.0, 0.6, 0.8),
        (0.0, -0.6, 0.8),
    ]
)


def _machine(state=None, **kwargs):
    plant = Plant(inertia=INERTIA, wheels=PYRAMID, state=state or PlantState())
    controller = AttitudeController(plant=plant, gains=PDGains.critically_damped(INERTIA, 0.1))
    env = Environment(orbit=CircularOrbit(altitude=500e3))
    return ModeMachine(plant=plant, controller=controller, environment=env, **kwargs)


def _fly(machine, seconds, t0=0.0, dt=0.1):
    t = t0
    for _ in range(round(seconds / dt)):
        machine.tick(t, dt)
        machine.plant.step(dt)
        t += dt
    return t


def _truth_error(machine, reference_quat):
    return al.quat_angle(al.quat_error(reference_quat, machine.plant.state.quat))


# ---------------------------------------------------------------------------
# Pointing modes, judged on TRUTH


def test_nadir_mode_acquires_and_holds_through_the_orbit():
    m = _machine()
    m.set_mode(AdcsMode.NADIR)
    t = _fly(m, 400.0)
    # Acquired: truth within 0.2 degrees of the LVLH frame...
    err = _truth_error(m, m.environment.nadir_attitude(t))
    assert math.degrees(err) < 0.2
    # ...and STAYS there while the frame sweeps on (the feedforward test:
    # without omega_ref this would settle near 2*n/wn ~ 1.3 degrees).
    worst = 0.0
    for _ in range(6):
        t = _fly(m, 50.0, t0=t)
        worst = max(worst, _truth_error(m, m.environment.nadir_attitude(t)))
    assert math.degrees(worst) < 0.2
    assert m.estimator.state is EstimatorState.VALID


def test_target_track_follows_a_ground_site():
    m = _machine()
    lat, lon = latlon_degrees(35.0, 5.0)
    m.set_ground_target(lat, lon)
    assert m.mode is AdcsMode.TARGET_TRACK
    t = _fly(m, 400.0)
    for _ in range(4):
        t = _fly(m, 50.0, t0=t)
        err = _truth_error(m, m.environment.target_attitude(t, lat, lon))
        assert math.degrees(err) < 0.3


def test_target_track_without_a_target_is_refused():
    m = _machine()
    with pytest.raises(ValueError, match="no ground target"):
        m.set_mode(AdcsMode.TARGET_TRACK)


def test_sunsafe_puts_the_panel_axis_on_the_sun():
    m = _machine()
    m.set_mode(AdcsMode.SUNSAFE)
    _fly(m, 400.0)
    pointed = al.quat_rotate(m.plant.state.quat, m.sun_axis_body)
    off_sun = math.acos(max(-1.0, min(1.0, al.v_dot(pointed, m.environment.sun_direction))))
    assert math.degrees(off_sun) < 0.3


def test_inertial_point_holds_a_commanded_attitude():
    m = _machine()
    target = al.quat_from_axis_angle((1.0, 0.0, 0.0), math.radians(30.0))
    m.set_inertial_target(target)
    m.set_mode(AdcsMode.INERTIAL_POINT)
    _fly(m, 300.0)
    assert math.degrees(_truth_error(m, target)) < 0.2


def test_standby_leaves_manual_wheel_commands_alone():
    m = _machine()
    m.set_mode(AdcsMode.STANDBY)
    m.plant.command_speed(0, 40.0)
    _fly(m, 30.0)
    assert m.controller.law is ControlLaw.IDLE
    assert m.plant.wheel_speed(0) == pytest.approx(40.0, rel=0.05)


# ---------------------------------------------------------------------------
# Magnetics


def test_detumble_bleeds_momentum_through_the_magnetorquers():
    tumbling = PlantState(omega=(0.02, -0.03, 0.025))  # ~2 deg/s tumble
    m = _machine(state=tumbling)
    m.set_mode(AdcsMode.DETUMBLE)
    momentum0 = al.v_norm(m.plant.total_momentum_reference())
    _fly(m, 3000.0, dt=0.5)
    momentum1 = al.v_norm(m.plant.total_momentum_reference())
    assert momentum1 < 0.5 * momentum0
    assert al.v_norm(m.plant.state.omega) < 0.5 * al.v_norm(tumbling.omega)


def test_detumble_does_nothing_with_the_mtq_chain_disabled():
    tumbling = PlantState(omega=(0.02, -0.03, 0.025))
    m = _machine(state=tumbling, mtq=Magnetorquer(enabled=False))
    m.set_mode(AdcsMode.DETUMBLE)
    momentum0 = al.v_norm(m.plant.total_momentum_reference())
    _fly(m, 300.0, dt=0.5)
    assert al.v_norm(m.plant.total_momentum_reference()) == pytest.approx(momentum0, rel=1e-9)


def test_desaturation_dumps_wheel_momentum_while_holding_attitude():
    # Preload the pyramid: the +z components add to ~0.16 N·m·s of stored
    # momentum. The dump must drain it through the magnetorquers while the
    # hold loop keeps the vehicle pointed.
    loaded = PlantState(wheel_momentum=(0.05, 0.05, 0.05, 0.05))
    m = _machine(state=loaded)
    m.set_mode(AdcsMode.INERTIAL_POINT)
    t = _fly(m, 100.0)  # settle the hold first
    h0 = m.momentum_total()
    assert h0 == pytest.approx(0.16, rel=0.05)
    m.request_desaturation()
    t = _fly(m, 1200.0, t0=t, dt=0.2)
    assert m.momentum_total() < 0.6 * h0
    assert math.degrees(_truth_error(m, m.inertial_target)) < 1.0


def test_desaturation_disengages_below_the_threshold():
    m = _machine()
    m.set_mode(AdcsMode.INERTIAL_POINT)
    m.request_desaturation()  # nothing stored: h < desat_stop immediately
    _fly(m, 1.0)
    assert not m.desaturating


def test_desaturation_survives_a_zero_field_measurement():
    # A dead magnetometer channel reporting an exactly-zero field must not
    # divide by zero in the dump law — the dipole request just goes empty.
    loaded = PlantState(wheel_momentum=(0.05, 0.05, 0.05, 0.05))
    m = _machine(state=loaded)
    m.controller.hold_attitude(al.QUAT_IDENTITY)  # dump gating needs a hold
    m.desaturating = True
    m.mag_body = (0.0, 0.0, 0.0)
    assert m._mtq_command(0.1) == (0.0, 0.0, 0.0)
    assert m.desaturating  # still trying; the field will come back


def test_desaturation_stays_pending_without_attitude_control():
    # Dumping without a hold would only spin the vehicle (the wheels never
    # absorb the reaction): in STANDBY the request must stay pending, the
    # momentum untouched, the body calm — and engage once a hold begins.
    loaded = PlantState(wheel_momentum=(0.05, 0.05, 0.05, 0.05))
    m = _machine(state=loaded)
    m.set_mode(AdcsMode.STANDBY)
    m.request_desaturation()
    h0 = m.momentum_total()
    _fly(m, 200.0, dt=0.2)
    assert m.desaturating
    assert m.momentum_total() == pytest.approx(h0, rel=1e-9)
    assert al.v_norm(m.plant.state.omega) < 1e-6
    m.set_mode(AdcsMode.INERTIAL_POINT)
    t = _fly(m, 600.0, t0=200.0, dt=0.2)
    assert m.momentum_total() < 0.8 * h0
    assert t > 0.0


def test_magnetorquer_clamps_per_axis_and_disables():
    mtq = Magnetorquer(max_dipole=5.0)
    b = (0.0, 0.0, 3e-5)
    torque, dipole = mtq.actuate((100.0, -100.0, 1.0), b)
    assert dipole == (5.0, -5.0, 1.0)
    assert torque == pytest.approx(al.v_cross((5.0, -5.0, 1.0), b))
    mtq.enabled = False
    torque, dipole = mtq.actuate((100.0, 0.0, 0.0), b)
    assert torque == (0.0, 0.0, 0.0)
    assert dipole == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Estimator-in-the-loop effects


def test_gyro_bias_override_visibly_degrades_the_hold():
    m = _machine()
    target = al.quat_from_axis_angle((0.0, 1.0, 0.0), 0.3)
    m.set_inertial_target(target)
    m.set_mode(AdcsMode.INERTIAL_POINT)
    t = _fly(m, 300.0)
    clean = math.degrees(_truth_error(m, target))
    assert clean < 0.1
    # A bogus 0.001 rad/s bias estimate: the vehicle settles where the
    # kp term cancels the phantom rate — kp*sin(phi/2) = kd*bias, i.e.
    # phi = 2*bias/bandwidth = 1.15 degrees. The operator sees a real,
    # predictable effect.
    m.estimator.set_bias_estimate((0.001, 0.0, 0.0))
    _fly(m, 300.0, t0=t)
    biased = math.degrees(_truth_error(m, target))
    assert biased == pytest.approx(math.degrees(2.0 * 0.001 / 0.1), rel=0.2)


def test_momentum_flag_observables():
    m = _machine()
    assert m.momentum_total() == pytest.approx(0.0, abs=1e-12)
    assert not m.near_saturation()
    fast = PlantState(wheel_momentum=(0.02 * 500.0, 0.0, 0.0, 0.0))
    m2 = _machine(state=fast)  # wheel 0 at 500 of 600 rad/s
    assert m2.near_saturation()
