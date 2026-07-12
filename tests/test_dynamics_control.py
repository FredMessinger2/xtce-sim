"""Attitude controller: closed-loop behavior against analytic truth.

The controller only acts through the wheel motors, so these tests run the
REAL loop — controller update, then plant RK4 step — and check what a
ground operator would see: slews that follow the critically damped
closed form while below saturation, large slews that go the short way
around, detumbles that move tumble momentum into the wheels, and a
degraded cluster that keeps doing its best instead of crashing.
"""

import math

import pytest

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.control import (
    AttitudeController,
    ControlLaw,
    PDGains,
    WheelAllocator,
)
from xtce_sim.dynamics.plant import Plant, PlantState, WheelParams

INERTIA = al.m_diag(12.0, 14.0, 9.0)

# The ImagingSat-style four-wheel pyramid.
PYRAMID = tuple(
    WheelParams(axis=ax, inertia=0.02, max_torque=0.05, max_speed=600.0)
    for ax in [
        (0.6, 0.0, 0.8),
        (-0.6, 0.0, 0.8),
        (0.0, 0.6, 0.8),
        (0.0, -0.6, 0.8),
    ]
)


def _loop(plant, controller, seconds, dt=0.01):
    """One control update per physics step — the tightest, simplest loop."""
    for _ in range(round(seconds / dt)):
        controller.update()
        plant.step(dt)


def _make(state=None, wheels=PYRAMID, bandwidth=0.05):
    plant = Plant(inertia=INERTIA, wheels=wheels, state=state or PlantState())
    gains = PDGains.critically_damped(INERTIA, bandwidth)
    return plant, AttitudeController(plant=plant, gains=gains)


# ---------------------------------------------------------------------------
# Gains


def test_gains_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        PDGains(kp=0.0, kd=1.0)
    with pytest.raises(ValueError, match="positive"):
        PDGains(kp=1.0, kd=-1.0)
    with pytest.raises(ValueError, match="bandwidth"):
        PDGains.critically_damped(INERTIA, 0.0)


def test_critically_damped_sizing_uses_the_largest_moment():
    g = PDGains.critically_damped(INERTIA, 0.1)
    assert g.kp == pytest.approx(2.0 * 14.0 * 0.01)
    assert g.kd == pytest.approx(2.0 * 14.0 * 0.1)


# ---------------------------------------------------------------------------
# Allocation


def test_allocation_reproduces_the_requested_torque_exactly():
    alloc = WheelAllocator(PYRAMID)
    torque = (0.01, -0.02, 0.015)
    u = alloc.allocate(torque, [True] * 4)
    produced = (0.0, 0.0, 0.0)
    for w, ui in zip(PYRAMID, u):
        produced = al.v_add(produced, al.v_scale(w.axis, -ui))
    assert produced == pytest.approx(torque, abs=1e-15)


def test_allocation_is_minimum_norm():
    # Adding any amount of the pyramid's null-space vector must not shrink
    # the solution: u is already the smallest that produces the torque.
    alloc = WheelAllocator(PYRAMID)
    u = alloc.allocate((0.01, -0.02, 0.015), [True] * 4)
    # Null vector of the pyramid: equal-and-opposite pairs cancel exactly.
    null = (1.0, 1.0, -1.0, -1.0)
    produced = (0.0, 0.0, 0.0)
    for w, ni in zip(PYRAMID, null):
        produced = al.v_add(produced, al.v_scale(w.axis, ni))
    assert produced == pytest.approx((0.0, 0.0, 0.0), abs=1e-15)
    norm_u = math.sqrt(sum(x * x for x in u))
    for alpha in (0.01, -0.01, 0.1):
        bumped = math.sqrt(sum((x + alpha * n) ** 2 for x, n in zip(u, null)))
        assert bumped > norm_u


def test_allocation_excludes_disabled_wheels_and_stays_exact():
    # Any three wheels of the pyramid still span 3D: exact, with zero
    # torque on the disabled wheel.
    alloc = WheelAllocator(PYRAMID)
    enabled = [True, True, True, False]
    torque = (0.01, -0.02, 0.015)
    u = alloc.allocate(torque, enabled)
    assert u[3] == pytest.approx(0.0, abs=0.0)
    produced = (0.0, 0.0, 0.0)
    for w, ui in zip(PYRAMID, u):
        produced = al.v_add(produced, al.v_scale(w.axis, -ui))
    assert produced == pytest.approx(torque, abs=1e-15)


def test_degraded_cluster_allocates_within_its_span():
    # Wheels 0 and 1 span only the x-z plane: a y-axis request is
    # physically impossible, and the allocator must degrade, not raise.
    alloc = WheelAllocator(PYRAMID)
    enabled = [True, True, False, False]
    u = alloc.allocate((0.0, 0.02, 0.0), enabled)
    produced = (0.0, 0.0, 0.0)
    for w, ui in zip(PYRAMID, u):
        produced = al.v_add(produced, al.v_scale(w.axis, -ui))
    assert abs(produced[1]) < 1e-9  # nothing about y — it can't
    # An in-span request still comes out nearly exact despite the damping.
    u = alloc.allocate((0.012, 0.0, 0.0), enabled)
    produced = (0.0, 0.0, 0.0)
    for w, ui in zip(PYRAMID, u):
        produced = al.v_add(produced, al.v_scale(w.axis, -ui))
    assert produced[0] == pytest.approx(0.012, rel=0.03)


def test_all_wheels_disabled_allocates_nothing():
    alloc = WheelAllocator(PYRAMID)
    assert alloc.allocate((0.01, 0.0, 0.0), [False] * 4) == (0.0,) * 4


# ---------------------------------------------------------------------------
# Closed-loop attitude hold


def test_small_slew_follows_the_critically_damped_closed_form():
    # 2 degrees about z, gains sized on Izz so that axis is exactly
    # critically damped: phi(t) = phi0 (1 + wn t) e^(-wn t) while torques
    # stay far below saturation and angles stay small.
    izz = al.m_diag(9.0, 9.0, 9.0)
    plant = Plant(inertia=izz, wheels=PYRAMID)
    wn = 0.05
    ctl = AttitudeController(plant=plant, gains=PDGains.critically_damped(izz, wn))
    phi0 = math.radians(2.0)
    ctl.hold_attitude(al.quat_from_axis_angle((0.0, 0.0, 1.0), phi0))
    for t_check in (20.0, 40.0):
        _loop(plant, ctl, 20.0)
        expected = phi0 * (1.0 + wn * t_check) * math.exp(-wn * t_check)
        assert ctl.pointing_error() == pytest.approx(expected, rel=0.02)


def test_large_slew_converges_with_saturated_wheels():
    # Stiff gains: at 120 degrees of error the PD requests ~0.5 N·m from
    # 0.05 N·m motors, so the whole transient runs saturated.
    plant, ctl = _make(bandwidth=0.2)
    target = al.quat_from_axis_angle(al.v_unit((1.0, 2.0, -1.0)), math.radians(120.0))
    ctl.hold_attitude(target)
    ctl.update()
    # The PD wants far more than 0.05 N·m at 120 degrees of error: the
    # wheels must report the clamped value, not the request.
    assert max(abs(plant.wheel_torque(i)) for i in range(4)) == pytest.approx(0.05)
    before = plant.total_momentum_reference()
    _loop(plant, ctl, 400.0)
    assert ctl.pointing_error_degrees() < 0.1
    assert al.v_norm(plant.state.omega) < 1e-4
    # The controller acted only through momentum exchange.
    after = plant.total_momentum_reference()
    for b, a in zip(before, after):
        assert a == pytest.approx(b, abs=1e-8)


def test_beyond_180_degrees_goes_the_short_way():
    # A 200-degree commanded rotation is a 160-degree slew the other way.
    # With canonicalized error the pointing error can only shrink; an
    # unwinding controller would drive it up through 180 first.
    plant, ctl = _make()
    ctl.hold_attitude(al.quat_from_axis_angle((0.0, 0.0, 1.0), math.radians(200.0)))
    initial = ctl.pointing_error()
    assert initial == pytest.approx(math.radians(160.0))
    worst = 0.0
    for _ in range(40):
        _loop(plant, ctl, 10.0)
        worst = max(worst, ctl.pointing_error())
    assert worst <= initial + 1e-9
    assert ctl.pointing_error_degrees() < 0.1


# ---------------------------------------------------------------------------
# Rate null and idle


def test_rate_null_moves_tumble_momentum_into_the_wheels():
    plant, ctl = _make(state=PlantState(omega=(0.05, -0.08, 0.06)))
    momentum0 = plant.total_momentum_reference()
    ctl.rate_null()
    _loop(plant, ctl, 300.0)
    assert al.v_norm(plant.state.omega) < 1e-5
    # The body stopped, so the whole tumble momentum now lives in the
    # wheels — same magnitude, carried by the cluster.
    assert al.v_norm(plant.wheel_momentum_body()) == pytest.approx(al.v_norm(momentum0), rel=1e-3)


def test_idle_hands_the_wheels_back_and_zeroes_its_torques():
    plant, ctl = _make()
    ctl.hold_attitude(al.quat_from_axis_angle((0.0, 0.0, 1.0), 0.5))
    _loop(plant, ctl, 5.0)
    assert any(plant.wheel_torque(i) != 0.0 for i in range(4))
    ctl.idle()
    # The transition zeroed the controller's torque commands...
    assert all(plant.wheel_torque(i) == pytest.approx(0.0, abs=0.0) for i in range(4))
    # ...and manual commands now stand: update() must not overwrite them.
    # (Spin-up to 50 rad/s takes I*dOmega/max_torque = 20 s; give it 30.)
    plant.command_speed(0, 50.0)
    _loop(plant, ctl, 30.0)
    assert plant.wheel_speed(0) == pytest.approx(50.0, rel=0.05)


def test_idle_controller_starts_out_of_the_way():
    # A fresh controller is IDLE: update() must leave manual commands alone,
    # and calling idle() again must not zero them either (the transition
    # only fires when leaving an active law).
    plant, ctl = _make()
    assert ctl.law is ControlLaw.IDLE
    plant.command_torque(2, 0.01)
    ctl.update()
    ctl.idle()
    assert plant.wheel_torque(2) == pytest.approx(0.01)


def test_idle_transition_skips_disabled_wheels():
    plant, ctl = _make()
    plant.set_enabled(1, False)
    plant.command_torque(1, 0.02)  # manual command on the disabled wheel
    ctl.rate_null()
    ctl.idle()
    assert plant.commands[1].torque == pytest.approx(0.02)


def test_disabled_wheels_are_left_alone_and_the_rest_compensate():
    plant, ctl = _make()
    plant.set_enabled(3, False)
    plant.command_torque(3, 0.02)  # a stale manual command; must survive
    ctl.hold_attitude(al.quat_from_axis_angle((1.0, 0.0, 0.0), 0.3))
    _loop(plant, ctl, 300.0)
    assert ctl.pointing_error_degrees() < 0.1
    assert plant.commands[3].torque == pytest.approx(0.02)


def test_wheel_disabled_mid_hold_cannot_fire_stale_torque_after_idle():
    # HIGH-severity regression: the controller writes a torque, the wheel
    # is disabled mid-law, the operator goes IDLE, then re-enables the
    # wheel. The controller's stale command must NOT fire — idle() zeroes
    # everything the controller ever wrote, enabled or not.
    plant, ctl = _make(bandwidth=0.2)
    ctl.hold_attitude(al.quat_from_axis_angle((0.0, 0.0, 1.0), math.radians(90.0)))
    _loop(plant, ctl, 5.0)
    plant.set_enabled(1, False)
    ctl.idle()
    plant.set_enabled(1, True)
    assert plant.wheel_torque(1) == pytest.approx(0.0, abs=0.0)
    h_before = plant.state.wheel_momentum[1]
    _loop(plant, ctl, 30.0)
    assert plant.state.wheel_momentum[1] == pytest.approx(h_before, abs=1e-12)


def test_rate_feedforward_tracks_a_rotating_target_without_lag():
    # A nadir-style reference rotating at LEO orbit rate. Without
    # feedforward the damping term fights the rotation and the loop
    # settles at the analytic lag 2*w_orb/wn; with omega_ref supplied the
    # lag all but vanishes. Both halves pin the same closed form.
    w_orb = 2.0 * math.pi / 5700.0
    wn = 0.05

    def track(feedforward):
        plant = Plant(inertia=INERTIA, wheels=PYRAMID)
        ctl = AttitudeController(plant=plant, gains=PDGains.critically_damped(INERTIA, wn))
        omega_ref = (0.0, 0.0, w_orb) if feedforward else (0.0, 0.0, 0.0)
        t, dt = 0.0, 0.01
        for _ in range(round(300.0 / dt)):
            target = al.quat_from_axis_angle((0.0, 0.0, 1.0), w_orb * t)
            ctl.hold_attitude(target, omega_ref)
            ctl.update()
            plant.step(dt)
            t += dt
        return ctl.pointing_error()

    assert track(feedforward=True) < math.radians(0.05)
    assert track(feedforward=False) == pytest.approx(2.0 * w_orb / wn, rel=0.05)


def test_non_principal_inertia_does_not_ring():
    # Principal moments (10, 1, 1) rotated 45 degrees about z: the largest
    # DIAGONAL entry is 5.5 but the stiff axis carries 10. Gains sized on
    # the Gershgorin bound keep even that axis critically damped — the
    # pointing error must decay monotonically, no overshoot.
    rotated = ((5.5, 4.5, 0.0), (4.5, 5.5, 0.0), (0.0, 0.0, 1.0))
    plant = Plant(inertia=rotated, wheels=PYRAMID)
    ctl = AttitudeController(plant=plant, gains=PDGains.critically_damped(rotated, 0.05))
    stiff_axis = al.v_unit((1.0, 1.0, 0.0))
    ctl.hold_attitude(al.quat_from_axis_angle(stiff_axis, math.radians(2.0)))
    prev = ctl.pointing_error()
    for _ in range(60):
        _loop(plant, ctl, 5.0)
        err = ctl.pointing_error()
        assert err <= prev + 1e-9
        prev = err
    assert ctl.pointing_error_degrees() < 0.01


def test_target_exactly_180_degrees_away_converges():
    # The canonicalized error is discontinuous on the 180-degree set; the
    # law is almost-globally stabilizing, and noise-free it must converge
    # cleanly from the worst case without the error ever growing.
    plant, ctl = _make()
    ctl.hold_attitude(al.quat_from_axis_angle((0.0, 0.0, 1.0), math.pi))
    prev = ctl.pointing_error()
    assert prev == pytest.approx(math.pi)
    for _ in range(40):
        _loop(plant, ctl, 10.0)
        err = ctl.pointing_error()
        assert err <= prev + 1e-9
        prev = err
    assert ctl.pointing_error_degrees() < 0.1


def test_hold_attitude_normalizes_its_target():
    plant, ctl = _make()
    ctl.hold_attitude((0.0, 0.0, 0.0, 2.0))
    assert ctl.target == pytest.approx(al.QUAT_IDENTITY)
    with pytest.raises(ValueError, match="zero-length quaternion"):
        ctl.hold_attitude((0.0, 0.0, 0.0, 0.0))
