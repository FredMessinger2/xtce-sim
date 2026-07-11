"""
Vector, matrix, and quaternion algebra plus a fixed-step RK4 integrator.

Everything is pure Python: 3-vectors and quaternions are tuples of floats,
matrices are 3-tuples of row 3-tuples. Immutable values make the physics
code referentially transparent — a state never changes behind the
integrator's back — and keep the whole dynamics layer dependency-free.

Conventions (these hold everywhere in xtce_sim.dynamics):

- Quaternions are scalar-LAST: ``(x, y, z, w)``, matching the XTCE
  quaternion aggregate order Q1..Q4 where the identity attitude is
  (0, 0, 0, 1).
- An attitude quaternion rotates BODY-frame vectors into the REFERENCE
  (inertial) frame: ``quat_rotate(q_body_to_ref, v_body) -> v_ref``.
- Euler angles are intrinsic 3-2-1 (yaw about Z, then pitch about Y, then
  roll about X), in radians, describing the same body-to-reference
  rotation.
- Body rates ``omega`` are expressed in the body frame, rad/s, so the
  kinematic equation is ``q_dot = 1/2 * q ⊗ (omega, 0)``.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]
Mat3 = tuple[Vec3, Vec3, Vec3]

#: A matrix is singular when |det| falls below this fraction of the Hadamard
#: bound (the product of its row norms). Relative, because det scales as the
#: cube of the matrix scale: an absolute floor would reject the perfectly
#: conditioned inertia tensor of a small spacecraft (~1e-4 kg·m² per axis,
#: det ~1e-12) while passing a rank-deficient matrix with large entries.
_SINGULAR_RTOL = 1e-12

#: Below this norm a vector or quaternion cannot be meaningfully normalized.
_ZERO_NORM = 1e-12


# ---------------------------------------------------------------------------
# 3-vectors


def v_add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def v_dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def v_norm(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def v_unit(a: Vec3) -> Vec3:
    """Unit vector along `a`; raises ValueError on a (near-)zero vector."""
    n = v_norm(a)
    if n < _ZERO_NORM:
        raise ValueError("cannot normalize a zero-length vector")
    return (a[0] / n, a[1] / n, a[2] / n)


# ---------------------------------------------------------------------------
# 3x3 matrices (row-major)


def m_diag(x: float, y: float, z: float) -> Mat3:
    return ((x, 0.0, 0.0), (0.0, y, 0.0), (0.0, 0.0, z))


def m_vec(m: Mat3, v: Vec3) -> Vec3:
    return (v_dot(m[0], v), v_dot(m[1], v), v_dot(m[2], v))


def m_transpose(m: Mat3) -> Mat3:
    return (
        (m[0][0], m[1][0], m[2][0]),
        (m[0][1], m[1][1], m[2][1]),
        (m[0][2], m[1][2], m[2][2]),
    )


def m_mul(a: Mat3, b: Mat3) -> Mat3:
    bt = m_transpose(b)
    return (
        (v_dot(a[0], bt[0]), v_dot(a[0], bt[1]), v_dot(a[0], bt[2])),
        (v_dot(a[1], bt[0]), v_dot(a[1], bt[1]), v_dot(a[1], bt[2])),
        (v_dot(a[2], bt[0]), v_dot(a[2], bt[1]), v_dot(a[2], bt[2])),
    )


def m_det(m: Mat3) -> float:
    return v_dot(m[0], v_cross(m[1], m[2]))


def m_inverse(m: Mat3) -> Mat3:
    """Inverse via the adjugate; raises ValueError if `m` is singular."""
    det = m_det(m)
    scale = v_norm(m[0]) * v_norm(m[1]) * v_norm(m[2])
    # <= so the all-zero matrix (det == scale == 0) is caught too.
    if abs(det) <= _SINGULAR_RTOL * scale:
        raise ValueError("matrix is singular")
    # The columns of the inverse are the cross products of m's row pairs,
    # divided by the determinant (the adjugate, written out).
    c0 = v_cross(m[1], m[2])
    c1 = v_cross(m[2], m[0])
    c2 = v_cross(m[0], m[1])
    return (
        (c0[0] / det, c1[0] / det, c2[0] / det),
        (c0[1] / det, c1[1] / det, c2[1] / det),
        (c0[2] / det, c1[2] / det, c2[2] / det),
    )


# ---------------------------------------------------------------------------
# Quaternions, scalar-last (x, y, z, w)

QUAT_IDENTITY: Quat = (0.0, 0.0, 0.0, 1.0)


def quat_multiply(a: Quat, b: Quat) -> Quat:
    """Hamilton product a ⊗ b (apply b's rotation first, then a's)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_conjugate(q: Quat) -> Quat:
    return (-q[0], -q[1], -q[2], q[3])


def quat_norm(q: Quat) -> float:
    return math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])


def quat_normalize(q: Quat) -> Quat:
    """Unit quaternion along `q`; raises ValueError on (near-)zero input.

    A zero-length quaternion is not an attitude, so callers integrating
    attitude must renormalize each step and let this raise if the state
    has been corrupted rather than silently pointing somewhere arbitrary.
    """
    n = quat_norm(q)
    if n < _ZERO_NORM:
        raise ValueError("cannot normalize a zero-length quaternion")
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def quat_rotate(q: Quat, v: Vec3) -> Vec3:
    """Rotate vector `v` by UNIT quaternion `q` (body → reference).

    The two-cross fast form used here is only a rotation for |q| = 1 — and
    unlike the full sandwich product it is not even a uniform scaling
    otherwise (a 0.1% norm defect yields ~4% component error). Callers
    integrating attitude must renormalize before rotating.
    """
    qv = (q[0], q[1], q[2])
    t = v_scale(v_cross(qv, v), 2.0)
    return v_add(v_add(v, v_scale(t, q[3])), v_cross(qv, t))


def quat_from_axis_angle(axis: Vec3, angle: float) -> Quat:
    """Unit quaternion for a rotation of `angle` radians about `axis`."""
    u = v_unit(axis)
    half = angle / 2.0
    s = math.sin(half)
    return (u[0] * s, u[1] * s, u[2] * s, math.cos(half))


def quat_angle(q: Quat) -> float:
    """Rotation angle of `q` in radians, in [0, pi].

    Uses atan2 of the vector and scalar parts, which stays accurate for
    tiny rotations where acos(w) loses precision, and treats q and -q as
    the same physical rotation.
    """
    vec_norm = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2])
    return 2.0 * math.atan2(vec_norm, abs(q[3]))


def quat_error(target: Quat, current: Quat) -> Quat:
    """Rotation carrying `current` onto `target` (both body → reference).

    ``quat_angle(quat_error(t, c))`` is the pointing error between the two
    attitudes; the vector part (in the body frame) is the axis a controller
    should torque about. The result is canonicalized to a non-negative
    scalar part so the vector part always points along the SHORT way
    around — feeding the raw q (with w < 0, encoding the long rotation)
    into a feedback law is the classic quaternion-unwinding bug.
    """
    err = quat_multiply(quat_conjugate(current), target)
    if err[3] < 0.0:
        return (-err[0], -err[1], -err[2], -err[3])
    return err


def quat_derivative(q: Quat, omega: Vec3) -> Quat:
    """Attitude kinematics: q̇ = ½ q ⊗ (ω, 0) for body-frame rates ω."""
    return quat_multiply(
        (q[0] / 2.0, q[1] / 2.0, q[2] / 2.0, q[3] / 2.0),
        (omega[0], omega[1], omega[2], 0.0),
    )


def euler321_to_quat(roll: float, pitch: float, yaw: float) -> Quat:
    """Body-to-reference quaternion from intrinsic 3-2-1 Euler angles (rad)."""
    qz = quat_from_axis_angle((0.0, 0.0, 1.0), yaw)
    qy = quat_from_axis_angle((0.0, 1.0, 0.0), pitch)
    qx = quat_from_axis_angle((1.0, 0.0, 0.0), roll)
    return quat_multiply(quat_multiply(qz, qy), qx)


def quat_to_euler321(q: Quat) -> tuple[float, float, float]:
    """Intrinsic 3-2-1 Euler angles (roll, pitch, yaw) in radians.

    At the gimbal-lock poles (pitch = ±90°) only roll∓yaw is encoded in the
    quaternion, so the generic formulas return garbage there; this picks the
    decomposition with roll = 0. The asin argument is clamped to [-1, 1] so
    rounding noise just off the poles cannot raise.

    The input is normalized on entry (raising ValueError on a zero
    quaternion): every extraction formula assumes |q| = 1, and near the
    poles even a float32-sized norm defect (~1e-6, exactly what an XTCE
    quaternion aggregate carries after wire quantization) would otherwise
    dodge the pole branch and silently zero the yaw.
    """
    x, y, z, w = quat_normalize(q)
    sin_pitch = 2.0 * (w * y - z * x)
    if sin_pitch >= 1.0 - 1e-9:
        # At +90° pitch the quaternion is s*(sin(d/2), cos(d/2), -sin(d/2),
        # cos(d/2)) with d = roll - yaw; pin roll = 0 and recover yaw.
        return (0.0, math.pi / 2.0, -2.0 * math.atan2(x, w))
    if sin_pitch <= -(1.0 - 1e-9):
        return (0.0, -math.pi / 2.0, 2.0 * math.atan2(x, w))
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, sin_pitch)))
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return (roll, pitch, yaw)


# ---------------------------------------------------------------------------
# Fixed-step 4th-order Runge-Kutta

State = tuple[float, ...]
Derivative = Callable[[State], State]


def _axpy(y: State, k: Sequence[float], s: float) -> State:
    """y + s*k, elementwise."""
    return tuple(yi + s * ki for yi, ki in zip(y, k, strict=True))


def rk4_step(f: Derivative, y: State, h: float) -> State:
    """One classical RK4 step of size `h` for the autonomous system y' = f(y).

    The plant equations here carry no explicit time dependence (commands
    change parameters between steps, not within one), so `f` takes only the
    state. `f` must return a derivative tuple of the same length as `y`.
    Time-varying inputs such as environment fields must either ride in the
    state vector (orbital phase makes a circular orbit autonomous) or be
    frozen per substep — at ~0.1 s substeps against a ~90-minute orbit the
    frozen-field error is negligible, but it is a choice, not an accident.
    """
    k1 = f(y)
    k2 = f(_axpy(y, k1, h / 2.0))
    k3 = f(_axpy(y, k2, h / 2.0))
    k4 = f(_axpy(y, k3, h))
    return tuple(
        yi + (h / 6.0) * (a + 2.0 * b + 2.0 * c + d)
        for yi, a, b, c, d in zip(y, k1, k2, k3, k4, strict=True)
    )
