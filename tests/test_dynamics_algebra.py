"""Algebra layer for the dynamics models: vectors, matrices, quaternions, RK4.

The physics units above this layer assume these primitives are exactly
right, so the tests here check algebraic identities and analytic results,
not just plausible outputs: cross-product orthogonality, M·M⁻¹ = I,
rotation round-trips, and RK4's fourth-order convergence rate.
"""

import math

import pytest

from xtce_sim.dynamics import algebra as al

# ---------------------------------------------------------------------------
# Vectors


def test_vector_arithmetic():
    assert al.v_add((1.0, 2.0, 3.0), (4.0, 5.0, 6.0)) == (5.0, 7.0, 9.0)
    assert al.v_sub((4.0, 5.0, 6.0), (1.0, 2.0, 3.0)) == (3.0, 3.0, 3.0)
    assert al.v_scale((1.0, -2.0, 3.0), 2.0) == (2.0, -4.0, 6.0)
    assert al.v_dot((1.0, 2.0, 3.0), (4.0, -5.0, 6.0)) == 12.0
    assert al.v_norm((3.0, 4.0, 0.0)) == 5.0


def test_cross_product_identities():
    a, b = (1.0, 2.0, 3.0), (-4.0, 0.5, 2.0)
    c = al.v_cross(a, b)
    # Orthogonal to both operands, anti-commutative, and right-handed.
    assert abs(al.v_dot(c, a)) < 1e-12
    assert abs(al.v_dot(c, b)) < 1e-12
    assert c == al.v_scale(al.v_cross(b, a), -1.0)
    assert al.v_cross((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)) == (0.0, 0.0, 1.0)


def test_unit_vector():
    u = al.v_unit((3.0, 0.0, 4.0))
    assert al.v_norm(u) == pytest.approx(1.0)
    assert u == (0.6, 0.0, 0.8)


def test_unit_of_zero_vector_raises():
    with pytest.raises(ValueError, match="zero-length vector"):
        al.v_unit((0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# Matrices

_M = ((2.0, 1.0, 0.5), (-1.0, 3.0, 2.0), (0.0, 4.0, 5.0))
_IDENTITY = al.m_diag(1.0, 1.0, 1.0)


def _mat_close(a, b, tol=1e-12):
    return all(abs(a[i][j] - b[i][j]) < tol for i in range(3) for j in range(3))


def test_matrix_vector_and_diag():
    assert al.m_vec(al.m_diag(2.0, 3.0, 4.0), (1.0, 1.0, 1.0)) == (2.0, 3.0, 4.0)
    assert al.m_vec(_IDENTITY, (7.0, -8.0, 9.0)) == (7.0, -8.0, 9.0)


def test_matrix_multiply_and_transpose():
    assert _mat_close(al.m_mul(_M, _IDENTITY), _M)
    assert _mat_close(al.m_mul(_IDENTITY, _M), _M)
    assert al.m_transpose(al.m_transpose(_M)) == _M
    # (AB)^T == B^T A^T
    b = ((1.0, 0.0, 2.0), (0.0, 1.0, 0.0), (3.0, 0.0, 1.0))
    assert _mat_close(
        al.m_transpose(al.m_mul(_M, b)),
        al.m_mul(al.m_transpose(b), al.m_transpose(_M)),
    )


def test_matrix_inverse_roundtrip():
    inv = al.m_inverse(_M)
    assert _mat_close(al.m_mul(_M, inv), _IDENTITY)
    assert _mat_close(al.m_mul(inv, _M), _IDENTITY)


def test_matrix_inverse_of_diag():
    inv = al.m_inverse(al.m_diag(2.0, 4.0, 8.0))
    assert _mat_close(inv, al.m_diag(0.5, 0.25, 0.125))


def test_singular_matrix_raises():
    singular = ((1.0, 2.0, 3.0), (2.0, 4.0, 6.0), (0.0, 1.0, 1.0))
    with pytest.raises(ValueError, match="singular"):
        al.m_inverse(singular)
    with pytest.raises(ValueError, match="singular"):
        al.m_inverse(al.m_diag(0.0, 0.0, 0.0))


def test_inverse_of_tiny_well_conditioned_matrix():
    # The singularity test is relative, not an absolute det floor: the
    # inertia tensor of a 5 cm PocketQube (~1e-4 kg·m² per axis, det ~1e-12)
    # is perfectly conditioned and must invert.
    tiny = al.m_diag(1.04e-4, 1.04e-4, 1.04e-4)
    inv = al.m_inverse(tiny)
    assert _mat_close(al.m_mul(tiny, inv), _IDENTITY, tol=1e-12)


def test_rank_deficient_large_matrix_raises():
    # Rows of magnitude ~1e6 with a relative rank deficiency of ~1e-14:
    # det is huge in absolute terms but the matrix is numerically singular.
    big = (
        (1e6, 2e6, 3e6),
        (2e6, 4e6, 6e6 + 6e-8),
        (3e6, 6e6, 9e6),
    )
    with pytest.raises(ValueError, match="singular"):
        al.m_inverse(big)


def test_determinant():
    assert al.m_det(_IDENTITY) == 1.0
    assert al.m_det(al.m_diag(2.0, 3.0, 4.0)) == 24.0


# ---------------------------------------------------------------------------
# Quaternions


def _quat_close(a, b, tol=1e-12):
    # q and -q are the same rotation; compare up to sign.
    direct = all(abs(x - y) < tol for x, y in zip(a, b))
    flipped = all(abs(x + y) < tol for x, y in zip(a, b))
    return direct or flipped


def test_identity_quaternion():
    assert al.QUAT_IDENTITY == (0.0, 0.0, 0.0, 1.0)
    v = (1.0, -2.0, 3.0)
    assert al.quat_rotate(al.QUAT_IDENTITY, v) == v
    assert al.quat_angle(al.QUAT_IDENTITY) == 0.0


def test_quat_multiply_identity_and_inverse():
    q = al.quat_from_axis_angle((1.0, 2.0, 2.0), 0.7)
    assert _quat_close(al.quat_multiply(q, al.QUAT_IDENTITY), q)
    assert _quat_close(al.quat_multiply(al.QUAT_IDENTITY, q), q)
    assert _quat_close(al.quat_multiply(q, al.quat_conjugate(q)), al.QUAT_IDENTITY)


def test_rotation_90_degrees_about_z_takes_x_to_y():
    q = al.quat_from_axis_angle((0.0, 0.0, 1.0), math.pi / 2.0)
    rotated = al.quat_rotate(q, (1.0, 0.0, 0.0))
    assert rotated[0] == pytest.approx(0.0, abs=1e-12)
    assert rotated[1] == pytest.approx(1.0)
    assert rotated[2] == pytest.approx(0.0, abs=1e-12)


def test_composition_matches_sequential_rotation():
    # a ⊗ b applies b first, then a — the column-vector convention.
    qa = al.quat_from_axis_angle((0.0, 0.0, 1.0), 0.4)
    qb = al.quat_from_axis_angle((1.0, 0.0, 0.0), 1.1)
    v = (0.3, -0.7, 0.9)
    combined = al.quat_rotate(al.quat_multiply(qa, qb), v)
    sequential = al.quat_rotate(qa, al.quat_rotate(qb, v))
    for c, s in zip(combined, sequential):
        assert c == pytest.approx(s)


def test_rotation_preserves_length():
    q = al.quat_from_axis_angle((2.0, -1.0, 0.5), 2.3)
    v = (0.6, 0.8, -1.2)
    assert al.v_norm(al.quat_rotate(q, v)) == pytest.approx(al.v_norm(v))


def test_axis_angle_roundtrip_via_quat_angle():
    for angle in (1e-9, 0.01, 1.0, math.pi - 0.01):
        q = al.quat_from_axis_angle((1.0, 1.0, 1.0), angle)
        assert al.quat_angle(q) == pytest.approx(angle, rel=1e-9)


def test_quat_angle_treats_negated_quaternion_as_same_rotation():
    q = al.quat_from_axis_angle((0.0, 1.0, 0.0), 0.8)
    neg = tuple(-c for c in q)
    assert al.quat_angle(neg) == pytest.approx(al.quat_angle(q))


def test_normalize():
    q = al.quat_normalize((2.0, 0.0, 0.0, 0.0))
    assert q == (1.0, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="zero-length quaternion"):
        al.quat_normalize((0.0, 0.0, 0.0, 0.0))


def test_quat_error_recovers_the_relative_rotation():
    current = al.quat_from_axis_angle((0.0, 0.0, 1.0), 0.5)
    step = al.quat_from_axis_angle((1.0, 0.0, 0.0), 0.25)
    target = al.quat_multiply(current, step)  # rotate `step` in the body frame
    err = al.quat_error(target, current)
    assert _quat_close(err, step, tol=1e-12)
    assert al.quat_angle(err) == pytest.approx(0.25)


def test_quat_error_of_equal_attitudes_is_identity():
    q = al.quat_from_axis_angle((3.0, 1.0, -2.0), 1.9)
    assert al.quat_angle(al.quat_error(q, q)) == pytest.approx(0.0, abs=1e-12)


def test_quat_error_is_canonicalized_to_the_short_rotation():
    # A 200.5° commanded rotation about +Z: the short way around is 159.5°
    # about -Z. The error must come back with w >= 0 and its vector part
    # along -Z, or a feedback law would unwind the long way.
    target = al.quat_from_axis_angle((0.0, 0.0, 1.0), 3.5)
    err = al.quat_error(target, al.QUAT_IDENTITY)
    assert err[3] >= 0.0
    assert err[2] < 0.0
    assert al.quat_angle(err) == pytest.approx(2.0 * math.pi - 3.5)


def test_quat_derivative_integrates_to_axis_rotation():
    # Constant body rate about Z from a NON-aligned start: the analytic
    # solution is q0 ⊗ axis_angle(z, ω·t), i.e. the rotation accumulates on
    # the BODY side. Starting away from identity is what catches a swapped
    # ½ ω ⊗ q kinematics — from identity the two sides agree exactly.
    omega = (0.0, 0.0, 0.2)
    q0 = al.quat_from_axis_angle((1.0, 0.0, 0.0), 1.0)
    q = q0
    steps, h = 1000, 0.01
    for _ in range(steps):
        dq = al.quat_derivative(q, omega)
        q = al.quat_normalize(tuple(qi + h * di for qi, di in zip(q, dq)))
    spin = al.quat_from_axis_angle((0.0, 0.0, 1.0), 0.2 * steps * h)
    expected = al.quat_multiply(q0, spin)
    assert _quat_close(q, expected, tol=1e-5)
    # And the wrong-side (inertial) accumulation is far away.
    wrong = al.quat_multiply(spin, q0)
    assert al.quat_angle(al.quat_error(wrong, q)) > 0.5


def test_euler321_roundtrip():
    cases = [
        (0.0, 0.0, 0.0),
        (0.3, -0.4, 1.2),
        (-1.0, 0.7, -2.5),
        (0.1, 1.4, 0.0),  # near (but off) the pitch pole
    ]
    for roll, pitch, yaw in cases:
        q = al.euler321_to_quat(roll, pitch, yaw)
        r2, p2, y2 = al.quat_to_euler321(q)
        assert r2 == pytest.approx(roll, abs=1e-9)
        assert p2 == pytest.approx(pitch, abs=1e-9)
        assert y2 == pytest.approx(yaw, abs=1e-9)


def test_euler321_pure_axis_rotations():
    q = al.euler321_to_quat(0.5, 0.0, 0.0)
    assert _quat_close(q, al.quat_from_axis_angle((1.0, 0.0, 0.0), 0.5))
    q = al.euler321_to_quat(0.0, 0.5, 0.0)
    assert _quat_close(q, al.quat_from_axis_angle((0.0, 1.0, 0.0), 0.5))
    q = al.euler321_to_quat(0.0, 0.0, 0.5)
    assert _quat_close(q, al.quat_from_axis_angle((0.0, 0.0, 1.0), 0.5))


def test_euler321_pitch_poles_return_equivalent_rotation():
    # Exactly at gimbal lock only roll∓yaw is observable; the extractor pins
    # roll = 0, and the round-trip must reproduce the same physical rotation.
    for pole in (math.pi / 2.0, -math.pi / 2.0):
        q = al.euler321_to_quat(0.2, pole, 0.4)
        roll, pitch, yaw = al.quat_to_euler321(q)
        assert roll == 0.0
        assert pitch == pytest.approx(pole, abs=1e-9)
        q2 = al.euler321_to_quat(roll, pitch, yaw)
        assert al.quat_angle(al.quat_error(q, q2)) == pytest.approx(0.0, abs=1e-9)


def test_euler321_normalizes_non_unit_input():
    # A float32-sized norm defect (exactly what an XTCE-encoded quaternion
    # carries off the wire) must not dodge the pole branch and zero the yaw.
    q = al.euler321_to_quat(0.0, math.pi / 2.0, 0.4)
    shrunk = tuple(c * (1.0 - 1e-6) for c in q)
    roll, pitch, yaw = al.quat_to_euler321(shrunk)
    assert pitch == pytest.approx(math.pi / 2.0, abs=1e-3)
    q2 = al.euler321_to_quat(roll, pitch, yaw)
    assert al.quat_angle(al.quat_error(q, q2)) == pytest.approx(0.0, abs=1e-3)
    # Away from the poles a scaled quaternion extracts the same angles.
    q = al.euler321_to_quat(0.3, -0.4, 1.2)
    angles = al.quat_to_euler321(tuple(c * 3.7 for c in q))
    assert angles == pytest.approx((0.3, -0.4, 1.2))
    with pytest.raises(ValueError, match="zero-length quaternion"):
        al.quat_to_euler321((0.0, 0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# RK4


def test_rk4_exponential_decay():
    # y' = -y from 1.0 over t=1 → e⁻¹, far beyond what Euler would manage
    # at this step size.
    y = (1.0,)
    for _ in range(100):
        y = al.rk4_step(lambda s: (-s[0],), y, 0.01)
    assert y[0] == pytest.approx(math.exp(-1.0), rel=1e-9)


def test_rk4_harmonic_oscillator_conserves_energy():
    # x'' = -x as a 2-state system; energy x² + v² should hold to ~1e-9
    # over ten periods at h=0.05.
    def f(s):
        return (s[1], -s[0])

    y = (1.0, 0.0)
    for _ in range(int(10 * 2 * math.pi / 0.05)):
        y = al.rk4_step(f, y, 0.05)
    energy = y[0] * y[0] + y[1] * y[1]
    assert energy == pytest.approx(1.0, rel=1e-6)


def test_rk4_fourth_order_convergence():
    # Halving h must shrink the global error by ~2⁴; accept anything ≥ 12
    # to leave room for rounding.
    def integrate(h):
        y = (1.0,)
        steps = round(1.0 / h)
        for _ in range(steps):
            y = al.rk4_step(lambda s: (-s[0],), y, h)
        return abs(y[0] - math.exp(-1.0))

    err_coarse = integrate(0.1)
    err_fine = integrate(0.05)
    assert err_coarse / err_fine > 12.0


def test_rk4_multidimensional_state():
    # Two independent decays integrate independently.
    def f(s):
        return (-s[0], -2.0 * s[1])

    y = (1.0, 1.0)
    for _ in range(1000):
        y = al.rk4_step(f, y, 0.001)
    assert y[0] == pytest.approx(math.exp(-1.0), rel=1e-9)
    assert y[1] == pytest.approx(math.exp(-2.0), rel=1e-9)


def test_rk4_rejects_mismatched_derivative_length():
    with pytest.raises(ValueError):
        al.rk4_step(lambda s: (1.0, 2.0), (0.0,), 0.1)
