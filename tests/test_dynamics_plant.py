"""Plant physics: rigid body + reaction wheels against analytic truth.

The load-bearing invariant is momentum: with no external torque the total
angular momentum in the reference frame must survive anything the wheel
motors do — slews, spin-ups, friction, disables. Beyond that, every model
feature is checked against a closed-form result (torque-free precession,
constant-torque spin-up, exponential friction decay), not against the
model's own output.
"""

import math

import pytest

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.plant import Plant, PlantState, WheelParams

# A deliberately asymmetric spacecraft so gyroscopic coupling is nonzero.
INERTIA = al.m_diag(12.0, 14.0, 9.0)

WHEEL_Z = WheelParams(axis=(0.0, 0.0, 1.0), inertia=0.02, max_torque=0.05, max_speed=600.0)

# A four-wheel pyramid, all canted toward +Z like the ImagingSat's.
PYRAMID = tuple(
    WheelParams(axis=ax, inertia=0.02, max_torque=0.05, max_speed=600.0)
    for ax in [
        (0.6, 0.0, 0.8),
        (-0.6, 0.0, 0.8),
        (0.0, 0.6, 0.8),
        (0.0, -0.6, 0.8),
    ]
)


def _run(plant, seconds, dt=0.01):
    for _ in range(round(seconds / dt)):
        plant.step(dt)


# ---------------------------------------------------------------------------
# Construction validation


def test_asymmetric_inertia_rejected():
    bad = ((12.0, 0.5, 0.0), (0.0, 14.0, 0.0), (0.0, 0.0, 9.0))
    with pytest.raises(ValueError, match="symmetric"):
        Plant(inertia=bad, wheels=())


def test_non_positive_definite_inertia_rejected():
    with pytest.raises(ValueError, match="positive-definite"):
        Plant(inertia=al.m_diag(12.0, -1.0, 9.0), wheels=())


def test_bad_wheel_parameters_rejected():
    with pytest.raises(ValueError, match="wheel 0.*positive"):
        Plant(
            inertia=INERTIA,
            wheels=(WheelParams((0, 0, 1), inertia=0.0, max_torque=1, max_speed=1),),
        )
    with pytest.raises(ValueError, match="wheel 0.*axis"):
        Plant(
            inertia=INERTIA,
            wheels=(WheelParams((0, 0, 0), inertia=1, max_torque=1, max_speed=1),),
        )
    with pytest.raises(ValueError, match="wheel 0.*friction"):
        Plant(
            inertia=INERTIA,
            wheels=(WheelParams((0, 0, 1), inertia=1, max_torque=1, max_speed=1, friction=-0.1),),
        )


def test_wheel_axis_is_normalized():
    plant = Plant(
        inertia=INERTIA,
        wheels=(WheelParams((0.0, 0.0, 2.0), inertia=1, max_torque=1, max_speed=1),),
    )
    assert plant.wheels[0].axis == pytest.approx((0.0, 0.0, 1.0))


def test_wheel_momentum_length_mismatch_rejected():
    with pytest.raises(ValueError, match="wheel_momentum length"):
        Plant(
            inertia=INERTIA,
            wheels=(WHEEL_Z,),
            state=PlantState(wheel_momentum=(0.0, 0.0)),
        )


def test_non_positive_dt_rejected():
    plant = Plant(inertia=INERTIA, wheels=())
    with pytest.raises(ValueError, match="dt must be positive"):
        plant.step(0.0)


# ---------------------------------------------------------------------------
# Rigid-body truth


def test_torque_free_momentum_conservation_while_tumbling():
    plant = Plant(
        inertia=INERTIA,
        wheels=(),
        state=PlantState(omega=(0.05, -0.11, 0.08)),
    )
    before = plant.total_momentum_reference()
    _run(plant, 60.0)
    after = plant.total_momentum_reference()
    # The body tumbled (rates traded between axes) ...
    assert plant.state.omega != pytest.approx((0.05, -0.11, 0.08), abs=1e-4)
    # ... yet reference-frame momentum held to near machine precision.
    for b, a in zip(before, after):
        assert a == pytest.approx(b, abs=1e-10)
    # And the attitude quaternion stayed unit-length.
    assert al.quat_norm(plant.state.quat) == pytest.approx(1.0, abs=1e-12)


def test_torque_free_precession_matches_analytic_solution():
    # Axisymmetric body: the transverse rate vector circles at
    # lambda = (I3 - I1)/I1 * omega3 while omega3 and the amplitude hold.
    i1, i3 = 10.0, 16.0
    omega3, amp = 0.3, 0.05
    plant = Plant(
        inertia=al.m_diag(i1, i1, i3),
        wheels=(),
        state=PlantState(omega=(amp, 0.0, omega3)),
    )
    t = 40.0
    _run(plant, t, dt=0.005)
    lam = (i3 - i1) / i1 * omega3
    ox, oy, oz = plant.state.omega
    assert ox == pytest.approx(amp * math.cos(lam * t), abs=1e-8)
    assert oy == pytest.approx(amp * math.sin(lam * t), abs=1e-8)
    assert oz == pytest.approx(omega3, abs=1e-10)


def test_constant_external_torque_spins_the_body_up():
    plant = Plant(inertia=INERTIA, wheels=(), external_torque=(0.0, 0.0, 0.009))
    _run(plant, 10.0)
    assert plant.state.omega[2] == pytest.approx(0.009 * 10.0 / 9.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Wheels


def test_single_wheel_torque_reacts_on_the_body():
    # Constant motor torque u for t seconds, from rest: the wheel gains
    # momentum u*t, the body counter-rotates at -u*t/Izz, and the attitude
    # winds back by the double integral. Total momentum stays zero.
    plant = Plant(inertia=INERTIA, wheels=(WHEEL_Z,))
    u, t = 0.03, 8.0
    plant.command_torque(0, u)
    _run(plant, t)
    izz = INERTIA[2][2]
    assert plant.state.wheel_momentum[0] == pytest.approx(u * t, rel=1e-9)
    assert plant.wheel_speed(0) == pytest.approx(u * t / WHEEL_Z.inertia, rel=1e-9)
    assert plant.state.omega[2] == pytest.approx(-u * t / izz, rel=1e-9)
    _, _, yaw = al.quat_to_euler321(plant.state.quat)
    assert yaw == pytest.approx(-u * t * t / (2.0 * izz), rel=1e-6)
    assert al.v_norm(plant.total_momentum_reference()) == pytest.approx(0.0, abs=1e-10)


def test_momentum_conserved_through_pyramid_activity_while_tumbling():
    plant = Plant(
        inertia=INERTIA,
        wheels=PYRAMID,
        state=PlantState(omega=(0.04, -0.03, 0.06), wheel_momentum=(0.2, -0.1, 0.15, 0.05)),
    )
    before = plant.total_momentum_reference()
    plant.command_torque(0, 0.03)
    plant.command_torque(1, -0.02)
    plant.command_speed(2, 150.0)
    _run(plant, 30.0)
    after = plant.total_momentum_reference()
    for b, a in zip(before, after):
        assert a == pytest.approx(b, abs=1e-9)


def test_speed_servo_rides_the_torque_limit_then_settles():
    plant = Plant(inertia=INERTIA, wheels=(WHEEL_Z,))
    target = 300.0
    plant.command_speed(0, target)
    # Constant-torque phase: far from target the servo saturates, so the
    # speed ramps at exactly max_torque / inertia.
    _run(plant, 20.0)
    assert plant.wheel_speed(0) == pytest.approx(
        WHEEL_Z.max_torque / WHEEL_Z.inertia * 20.0, rel=1e-6
    )
    # Long after the minimum spin-up time (I*dOmega/u_max = 120 s) the
    # exponential tail has closed on the target.
    _run(plant, 130.0)
    assert plant.wheel_speed(0) == pytest.approx(target, rel=1e-3)


def test_wheel_refuses_torque_past_its_speed_limit():
    plant = Plant(inertia=INERTIA, wheels=(WHEEL_Z,))
    plant.command_speed(0, 10_000.0)  # far beyond max_speed
    _run(plant, 300.0)
    # The rail can be overshot by at most one integration step's worth of
    # acceleration (the clamp acts on the speed it sees at each RK4 stage).
    one_step = WHEEL_Z.max_torque / WHEEL_Z.inertia * 0.01
    assert plant.wheel_speed(0) <= WHEEL_Z.max_speed + one_step
    assert plant.wheel_speed(0) == pytest.approx(WHEEL_Z.max_speed, rel=1e-3)
    # Torquing back down from the rail still works.
    plant.command_torque(0, -WHEEL_Z.max_torque)
    _run(plant, 10.0)
    assert plant.wheel_speed(0) < WHEEL_Z.max_speed - 1.0


def test_wheel_speed_limit_holds_in_reverse_too():
    plant = Plant(inertia=INERTIA, wheels=(WHEEL_Z,))
    plant.command_speed(0, -10_000.0)
    _run(plant, 300.0)
    one_step = WHEEL_Z.max_torque / WHEEL_Z.inertia * 0.01
    assert plant.wheel_speed(0) >= -(WHEEL_Z.max_speed + one_step)
    assert plant.wheel_speed(0) == pytest.approx(-WHEEL_Z.max_speed, rel=1e-3)


def test_disabled_wheel_coasts_down_on_friction_and_conserves_momentum():
    wheel = WheelParams(
        axis=(0.0, 0.0, 1.0),
        inertia=0.02,
        max_torque=0.05,
        max_speed=600.0,
        friction=1e-4,
    )
    plant = Plant(
        inertia=INERTIA,
        wheels=(wheel,),
        state=PlantState(wheel_momentum=(0.02 * 200.0,)),
    )
    before = plant.total_momentum_reference()
    plant.set_enabled(0, False)
    plant.command_torque(0, 0.05)  # ignored while disabled
    t = 50.0
    _run(plant, t)
    # Exponential coast-down: Omega(t) = Omega0 * exp(-friction*t/I).
    expected = 200.0 * math.exp(-wheel.friction * t / wheel.inertia)
    assert plant.wheel_speed(0) == pytest.approx(expected, rel=1e-6)
    # Bearing drag hands momentum to the body rather than destroying it.
    after = plant.total_momentum_reference()
    for b, a in zip(before, after):
        assert a == pytest.approx(b, abs=1e-9)
    assert plant.state.omega[2] > 0.0


def test_plant_remembers_commands_across_disable():
    # This pins PLANT semantics (commands persist while the motor is off),
    # not ADCS_WHEEL_ENABLE policy — whether the flight software resumes a
    # pre-disable setpoint or comes up torque-zero is unit 4's decision.
    plant = Plant(inertia=INERTIA, wheels=(WHEEL_Z,))
    plant.command_speed(0, 100.0)
    plant.set_enabled(0, False)
    _run(plant, 5.0)
    assert plant.wheel_speed(0) == pytest.approx(0.0, abs=1e-15)
    assert plant.wheel_torque(0) == pytest.approx(0.0, abs=1e-15)
    plant.set_enabled(0, True)
    _run(plant, 120.0)
    assert plant.wheel_speed(0) == pytest.approx(100.0, rel=1e-3)


def test_torque_mode_commands_are_clamped_to_the_motor_limit():
    # A feedback controller will routinely ask for more than the motor has:
    # the wheel must ramp at exactly max_torque, and wheel_torque() must
    # report the saturated value so anti-windup can see it.
    plant = Plant(inertia=INERTIA, wheels=(WHEEL_Z,))
    plant.command_torque(0, 10.0)  # 200x the motor limit
    assert plant.wheel_torque(0) == pytest.approx(WHEEL_Z.max_torque)
    t = 8.0
    _run(plant, t)
    assert plant.state.wheel_momentum[0] == pytest.approx(WHEEL_Z.max_torque * t, rel=1e-9)
    assert plant.state.omega[2] == pytest.approx(-WHEEL_Z.max_torque * t / INERTIA[2][2], rel=1e-9)


def test_non_positive_definite_with_positive_det_rejected():
    # det = +5 and an all-positive diagonal, but eigenvalues (5, -1, -1):
    # det > 0 alone is NOT positive-definiteness. Sylvester catches it.
    sneaky = ((1.0, 2.0, 2.0), (2.0, 1.0, 2.0), (2.0, 2.0, 1.0))
    with pytest.raises(ValueError, match="positive-definite"):
        Plant(inertia=sneaky, wheels=())


def test_symmetry_tolerance_scales_with_the_tensor():
    # An ISS-class tensor carries ~1e-8 kg·m² of rounding asymmetry after a
    # frame rotation — legitimate at 1e8 scale, and it must be accepted.
    big = (
        (1.0e8, 1000.0, 0.0),
        (1000.0 + 1e-8, 1.6e8, 0.0),
        (0.0, 0.0, 2.4e8),
    )
    Plant(inertia=big, wheels=())


def test_construction_copies_the_given_state():
    shared = PlantState()
    plant = Plant(inertia=INERTIA, wheels=(WHEEL_Z,), state=shared)
    assert plant.state is not shared
    assert shared.wheel_momentum == ()  # the caller's object is untouched
    assert plant.state.wheel_momentum == (0.0,)
