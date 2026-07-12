"""
Attitude control: quaternion-feedback PD with min-norm wheel allocation.

The controller closes the loop the way real ADCS flight software does:
from the estimated attitude and rates it computes a desired BODY torque,
allocates that torque across the reaction-wheel cluster, and writes
torque commands to the plant's wheel motors. It never touches the plant
state directly — everything it achieves, it achieves through actuators,
so wheel torque limits, speed rails, and momentum buildup all apply.

Control laws (what the controller can do; WHICH law an ADCS mode uses is
the mode machine's decision, built in unit 3 where the reference frames
for NADIR/SUNSAFE/TARGET_TRACK exist):

- IDLE — the controller does not drive the wheels at all; an operator's
  manual speed/torque commands (ADCS_WHEEL_SET_SPEED test mode) stand.
- RATE_NULL — pure rate damping, τ = −K_d ω: wheel-based detumbling that
  pulls the tumble momentum into the wheels. (Flight detumble usually
  dumps momentum magnetically; that arrives with the environment models.)
- ATTITUDE_HOLD — quaternion feedback, τ = K_p q_e,vec − K_d (ω − ω_ref),
  where q_e = quat_error(target, current) is canonicalized to the short
  way around: the classic ALMOST-globally stabilizing PD law without
  unwinding (the canonicalization is discontinuous on the 180° set, the
  known limitation hybrid controllers exist to fix; noise-free it
  converges cleanly even from exactly 180°, which a test pins). ω_ref is
  the reference's own body rate — zero for a fixed hold, the orbit rate
  for tracking modes; without it a constant-rate target is tracked with
  a permanent lag of ≈ 2·ω_ref/ωn. Small-angle, sub-saturation behavior
  is the critically dampable second order
  I φ̈ + K_d φ̇ + (K_p/2) φ = 0 — the closed form the tests pin.

Honest limits: during saturation each wheel clamps independently, so the
realized body torque points somewhat off the commanded direction (flight
controllers often rescale the whole allocation vector to preserve it);
convergence is unaffected here. Recorded as future work.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.plant import Plant, WheelParams


class ControlLaw(Enum):
    IDLE = "idle"
    RATE_NULL = "rate_null"
    ATTITUDE_HOLD = "attitude_hold"


@dataclass(frozen=True)
class PDGains:
    """Quaternion-feedback gains.

    kp is N·m per unit of quaternion-error VECTOR component (≈ half the
    error angle in radians, small angles); kd is N·m per rad/s of body
    rate. For axis inertia I the small-angle closed loop is
    φ̈ + (kd/I) φ̇ + (kp/2I) φ = 0.
    """

    kp: float
    kd: float

    def __post_init__(self) -> None:
        if self.kp <= 0.0 or self.kd <= 0.0:
            raise ValueError("PD gains must be positive")

    @classmethod
    def critically_damped(cls, inertia: al.Mat3, bandwidth: float) -> "PDGains":
        """Gains for a critically damped loop at `bandwidth` rad/s natural
        frequency, sized on the Gershgorin upper bound of the inertia's
        eigenvalues (max absolute row sum ≥ largest principal moment) so
        EVERY axis is at least critically damped, non-principal tensors
        included — lighter axes respond faster, none ring.

        The law is continuous-time: the controller tick h must satisfy
        bandwidth·h ≪ 1 (the sampled loop goes unstable at bandwidth·h
        = 1). At 0.1 s ticks that allows bandwidths up to ~10 rad/s —
        far beyond anything a reaction wheel plant wants.
        """
        if bandwidth <= 0.0:
            raise ValueError("bandwidth must be positive")
        i_max = max(sum(abs(x) for x in row) for row in inertia)
        return cls(kp=2.0 * i_max * bandwidth * bandwidth, kd=2.0 * i_max * bandwidth)


class WheelAllocator:
    """Minimum-norm mapping from a desired body torque to per-wheel motor
    torques over the currently enabled wheels.

    Solves Σ aᵢ uᵢ = −τ_body (the body feels −aᵢuᵢ) via u = Aᵀ(AAᵀ)⁻¹(−τ).
    When the enabled wheels no longer span three axes, AAᵀ is singular and
    exact allocation is physically impossible; a damped inverse then
    yields the best-effort torque within the wheels' span instead of
    crashing — the honest behavior of a degraded cluster.
    """

    def __init__(self, wheels: Sequence[WheelParams]) -> None:
        self._axes = tuple(w.axis for w in wheels)

    def allocate(self, torque: al.Vec3, enabled: Sequence[bool]) -> tuple[float, ...]:
        axes = [a for a, on in zip(self._axes, enabled, strict=True) if on]
        if not axes:
            return (0.0,) * len(self._axes)
        m = self._gram(axes)
        try:
            m_inv = al.m_inverse(m)
        except ValueError:
            m_inv = al.m_inverse(self._damped(m, len(axes)))
        x = al.m_vec(m_inv, al.v_scale(torque, -1.0))
        return tuple(
            al.v_dot(a, x) if on else 0.0 for a, on in zip(self._axes, enabled, strict=True)
        )

    @staticmethod
    def _gram(axes: list[al.Vec3]) -> al.Mat3:
        """AAᵀ = Σ aᵢ aᵢᵀ over the enabled axes."""
        rows = [[0.0] * 3 for _ in range(3)]
        for a in axes:
            for i in range(3):
                for j in range(3):
                    rows[i][j] += a[i] * a[j]
        return tuple(tuple(r) for r in rows)

    @staticmethod
    def _damped(m: al.Mat3, n_axes: int) -> al.Mat3:
        # Tikhonov damping at 1% of the mean eigenvalue (trace/3 = n/3 for
        # unit axes): always invertible. The bias is ≈ lam/mu in EVERY
        # direction (mu = that direction's Gram eigenvalue) — negligible
        # where the surviving span is well conditioned, and a strong
        # attenuation of barely-in-span requests, which is the sane
        # response: the exact solution there would demand torques far
        # beyond any motor.
        lam = 0.01 * n_axes / 3.0
        return tuple(tuple(m[i][j] + (lam if i == j else 0.0) for j in range(3)) for i in range(3))


@dataclass
class AttitudeController:
    """Closes the attitude loop through the plant's wheel motors.

    Call `update()` once per control tick (before advancing the plant;
    command processing must also run before update so a re-enabled wheel
    never fires a stale torque); it recomputes the PD torque from the
    current state and rewrites the torque command of every ENABLED wheel.
    The controller remembers which wheels IT has written and, when a law
    ends (idle()), zeroes exactly those — enabled or not — so its own
    output can never linger on a wheel that was disabled mid-law, while
    genuinely manual commands always stand.
    """

    plant: Plant
    gains: PDGains
    law: ControlLaw = ControlLaw.IDLE
    target: al.Quat = al.QUAT_IDENTITY
    omega_ref: al.Vec3 = (0.0, 0.0, 0.0)  # reference body rate, rad/s

    def __post_init__(self) -> None:
        self._allocator = WheelAllocator(self.plant.wheels)
        self._driven: set[int] = set()

    # -- law selection --------------------------------------------------------

    def hold_attitude(self, target: al.Quat, omega_ref: al.Vec3 = (0.0, 0.0, 0.0)) -> None:
        """Drive the body to `target` (body → reference) and keep it there.

        For a moving reference (nadir, target track) pass the reference's
        own body rate as `omega_ref` so the damping term does not fight
        the tracking rotation; without feedforward a constant-rate target
        is followed with a permanent lag of ≈ 2·|omega_ref|/bandwidth.
        """
        self.target = al.quat_normalize(target)
        self.omega_ref = omega_ref
        self.law = ControlLaw.ATTITUDE_HOLD

    def rate_null(self) -> None:
        """Damp body rates to zero, absorbing the momentum into the wheels."""
        self.law = ControlLaw.RATE_NULL

    def idle(self) -> None:
        """Stop driving the wheels; hand them back to manual commands.

        Zeroes every torque command this controller wrote — including on
        wheels disabled mid-law, whose stale command would otherwise fire
        the moment they were re-enabled."""
        for i in sorted(self._driven):
            self.plant.command_torque(i, 0.0)
        self._driven.clear()
        self.law = ControlLaw.IDLE

    # -- the loop --------------------------------------------------------------

    def update(self) -> None:
        if self.law is ControlLaw.IDLE:
            return
        state = self.plant.state
        if self.law is ControlLaw.ATTITUDE_HOLD:
            err = al.quat_error(self.target, state.quat)
            rate_error = al.v_sub(state.omega, self.omega_ref)
            torque = al.v_sub(
                al.v_scale((err[0], err[1], err[2]), self.gains.kp),
                al.v_scale(rate_error, self.gains.kd),
            )
        else:  # RATE_NULL: null the INERTIAL rates, no reference to track.
            torque = al.v_scale(state.omega, -self.gains.kd)
        enabled = [cmd.enabled for cmd in self.plant.commands]
        for i, u in enumerate(self._allocator.allocate(torque, enabled)):
            if enabled[i]:
                self.plant.command_torque(i, u)
                self._driven.add(i)

    # -- observables -----------------------------------------------------------

    def pointing_error(self) -> float:
        """Angle between the current and target attitudes, radians — the
        source for ADCS_POINTING_ERR telemetry. Only meaningful under
        ATTITUDE_HOLD; in IDLE/RATE_NULL the target is stale, and the
        telemetry layer should gate on `law` (unit 4)."""
        return al.quat_angle(al.quat_error(self.target, self.plant.state.quat))

    def pointing_error_degrees(self) -> float:
        return math.degrees(self.pointing_error())
