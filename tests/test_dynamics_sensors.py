"""Sensor and estimator models: deterministic noise, honest failure modes."""

import math

import pytest

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.sensors import (
    AttitudeEstimator,
    EstimatorState,
    Gyro,
    Magnetometer,
    StarTracker,
    SunSensor,
    gaussian,
)

SUN_AHEAD = (1.0, 0.0, 0.0)  # body-frame sun, far from a +z boresight


# ---------------------------------------------------------------------------
# Noise machinery


def test_gaussian_is_deterministic():
    assert gaussian(42, 7) == gaussian(42, 7)
    assert gaussian(42, 7) != gaussian(42, 8)
    assert gaussian(42, 7) != gaussian(43, 7)


def test_gaussian_statistics():
    n = 4000
    draws = [gaussian(9, k) for k in range(n)]
    mean = sum(draws) / n
    var = sum((d - mean) ** 2 for d in draws) / n
    assert abs(mean) < 0.05
    assert math.sqrt(var) == pytest.approx(1.0, rel=0.05)


def test_identically_seeded_sensors_repeat_exactly():
    a, b = Gyro(seed=5), Gyro(seed=5)
    truth = (0.01, -0.02, 0.005)
    seq_a = [a.measure(truth) for _ in range(10)]
    seq_b = [b.measure(truth) for _ in range(10)]
    assert seq_a == seq_b
    # And the stream advances: successive samples differ.
    assert seq_a[0] != seq_a[1]


# ---------------------------------------------------------------------------
# Sensors


def test_star_tracker_noise_is_small_and_nonzero():
    st = StarTracker()
    q_true = al.quat_from_axis_angle((1.0, 2.0, 0.5), 0.9)
    errors = []
    for _ in range(200):
        q, ok = st.measure(q_true, SUN_AHEAD, sun_lit=True)
        assert ok
        errors.append(al.quat_angle(al.quat_error(q_true, q)))
    assert max(errors) < 8.0 * st.sigma
    assert min(errors) > 0.0


def test_star_tracker_blinds_inside_the_sun_exclusion_cone():
    st = StarTracker()  # boresight +z, 30-degree exclusion
    sun_near_boresight = al.v_unit((0.1, 0.0, 1.0))  # ~5.7 degrees off
    _, ok = st.measure(al.QUAT_IDENTITY, sun_near_boresight, sun_lit=True)
    assert not ok
    # Same geometry in eclipse: stars do not care about a dark sun.
    _, ok = st.measure(al.QUAT_IDENTITY, sun_near_boresight, sun_lit=False)
    assert ok
    # Sun outside the cone: fine.
    _, ok = st.measure(al.QUAT_IDENTITY, SUN_AHEAD, sun_lit=True)
    assert ok


def test_noise_free_star_tracker_returns_exact_truth():
    # sigma = 0 is a legitimate configuration (ideal-sensor studies); the
    # zero-length rotation vector must short-circuit to the identity
    # rather than trying to normalize a zero axis.
    st = StarTracker(sigma=0.0)
    q_true = al.quat_from_axis_angle((0.3, -0.5, 1.0), 1.2)
    q, ok = st.measure(q_true, SUN_AHEAD, sun_lit=True)
    assert ok
    assert q == pytest.approx(q_true)


def test_gyro_reports_bias_plus_noise():
    gyro = Gyro(sigma=1e-6, bias=(0.01, -0.005, 0.002))
    meas = gyro.measure((0.0, 0.0, 0.0))
    for m, b in zip(meas, gyro.bias):
        assert m == pytest.approx(b, abs=1e-5)


def test_sun_sensor_eclipse_and_accuracy():
    ss = SunSensor()
    vec, present = ss.measure(SUN_AHEAD, sun_lit=False)
    assert not present
    assert vec == (0.0, 0.0, 0.0)
    vec, present = ss.measure(SUN_AHEAD, sun_lit=True)
    assert present
    assert al.v_norm(vec) == pytest.approx(1.0)
    angle = math.acos(max(-1.0, min(1.0, al.v_dot(vec, SUN_AHEAD))))
    assert angle < math.radians(5.0)


def test_magnetometer_noise_level():
    mag = Magnetometer()
    truth = (1e-5, -2e-5, 3e-5)
    meas = mag.measure(truth)
    for m, b in zip(meas, truth):
        assert m == pytest.approx(b, abs=8.0 * mag.sigma)
    assert meas != truth


# ---------------------------------------------------------------------------
# Estimator


def _perfect_gyro():
    return Gyro(sigma=0.0)


def test_estimator_converges_then_reports_valid():
    est = AttitudeEstimator(convergence_time=30.0)
    q = al.quat_from_axis_angle((0.0, 1.0, 0.0), 0.4)
    assert est.state is EstimatorState.CONVERGING  # before any update
    est.update(0.0, 0.1, q, True, (0.0, 0.0, 0.0))
    assert est.state is EstimatorState.CONVERGING
    assert est.attitude == q
    est.update(31.0, 0.1, q, True, (0.0, 0.0, 0.0))
    assert est.state is EstimatorState.VALID


def test_estimator_dead_reckons_through_a_dropout():
    # Constant true rate, perfect gyro: the propagated attitude must match
    # the true rotation while the tracker is dark, and the state must
    # decay to INVALID once the dropout outlives the limit.
    est = AttitudeEstimator(convergence_time=0.0, dropout_limit=20.0)
    omega = (0.0, 0.0, 0.02)
    q = al.QUAT_IDENTITY
    est.update(0.0, 0.1, q, True, omega)
    t, dt = 0.0, 0.1
    for _ in range(150):  # 15 s dark — still VALID, propagating
        t += dt
        est.update(t, dt, al.QUAT_IDENTITY, False, omega)
    assert est.state is EstimatorState.VALID
    expected = al.quat_from_axis_angle((0.0, 0.0, 1.0), 0.02 * t)
    assert al.quat_angle(al.quat_error(expected, est.attitude)) < 1e-4
    for _ in range(100):  # past the 20 s limit
        t += dt
        est.update(t, dt, al.QUAT_IDENTITY, False, omega)
    assert est.state is EstimatorState.INVALID


def test_wrong_bias_estimate_makes_dead_reckoning_drift():
    # ADCS_SET_GYRO_BIAS with a bogus value: during an outage the solution
    # drifts at exactly the bias error — the honest failure mode.
    est = AttitudeEstimator(convergence_time=0.0, dropout_limit=1e9)
    est.set_bias_estimate((0.001, 0.0, 0.0))
    est.update(0.0, 0.1, al.QUAT_IDENTITY, True, (0.0, 0.0, 0.0))
    t, dt = 0.0, 0.1
    for _ in range(1000):  # 100 s dark, true rate zero
        t += dt
        est.update(t, dt, al.QUAT_IDENTITY, False, (0.0, 0.0, 0.0))
    drift = al.quat_angle(al.quat_error(al.QUAT_IDENTITY, est.attitude))
    assert drift == pytest.approx(0.001 * t, rel=1e-3)


def test_estimator_reset_restarts_convergence():
    est = AttitudeEstimator(convergence_time=30.0)
    q = al.QUAT_IDENTITY
    est.update(0.0, 0.1, q, True, (0.0, 0.0, 0.0))
    est.update(40.0, 0.1, q, True, (0.0, 0.0, 0.0))
    assert est.state is EstimatorState.VALID
    est.reset(40.0)
    est.update(41.0, 0.1, q, True, (0.0, 0.0, 0.0))
    assert est.state is EstimatorState.CONVERGING
    est.update(75.0, 0.1, q, True, (0.0, 0.0, 0.0))
    assert est.state is EstimatorState.VALID


def test_estimator_with_no_tracker_ever_is_invalid_after_convergence_window():
    est = AttitudeEstimator(convergence_time=0.0)
    est.update(0.0, 0.1, al.QUAT_IDENTITY, False, (0.0, 0.0, 0.0))
    assert est.state is EstimatorState.INVALID
