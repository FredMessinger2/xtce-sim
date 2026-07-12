"""
Sensor and estimator models: what the avionics believe, not what is true.

The control loop closes on ESTIMATES — the star tracker quaternion, the
bias-compensated gyro rates — while the plant integrates truth. The gap
between the two is where sensor realism lives: a wrong gyro-bias estimate
makes the vehicle genuinely drift, a sun-blinded star tracker degrades
the attitude solution, and all of it shows up in telemetry exactly the
way it would from a real vehicle.

Noise is deterministic: a counter-indexed integer hash fed through
Box-Muller, not a PRNG (the project's Sonar profile treats random.Random
as a vulnerability, and sensor noise only needs plausible spread and
exact reproducibility — same seed, same telemetry, every run).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from xtce_sim.dynamics import algebra as al


def _uniform(seed: int, k: int) -> float:
    """Deterministic uniform in (0, 1) for draw k of stream `seed` —
    a Knuth multiplicative hash with an xorshift finisher to break up
    the low-bit regularity of consecutive counters."""
    x = (seed * 2654435761 + k * 40503 + 12345) & 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x45D9F3B) & 0xFFFFFFFF
    x ^= x >> 16
    return (x + 0.5) / 2.0**32


def gaussian(seed: int, k: int) -> float:
    """Deterministic standard-normal draw k of stream `seed` (Box-Muller)."""
    u1 = _uniform(seed, 2 * k)
    u2 = _uniform(seed, 2 * k + 1)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


@dataclass
class _NoiseStream:
    """A private, counter-indexed gaussian stream for one sensor."""

    seed: int
    _k: int = field(default=0, repr=False)

    def next3(self) -> al.Vec3:
        k = self._k
        self._k += 3
        return (
            gaussian(self.seed, k),
            gaussian(self.seed, k + 1),
            gaussian(self.seed, k + 2),
        )


def _small_rotation(vec: al.Vec3) -> al.Quat:
    """Quaternion for a small rotation-vector perturbation (radians)."""
    angle = al.v_norm(vec)
    if angle < 1e-15:
        return al.QUAT_IDENTITY
    return al.quat_from_axis_angle(vec, angle)


@dataclass
class StarTracker:
    """Quaternion measurement with per-axis angular noise and a sun
    exclusion cone: sunlight within `exclusion` of the boresight blinds
    it (FAULT), exactly the constraint that makes slews near the sun
    line interesting. It works fine in eclipse — stars don't set."""

    sigma: float = 1.5e-4  # rad per axis, 1σ (~30 arcsec)
    boresight: al.Vec3 = (0.0, 0.0, 1.0)  # body frame
    exclusion: float = math.radians(30.0)
    seed: int = 101

    def __post_init__(self) -> None:
        self.boresight = al.v_unit(self.boresight)
        self._noise = _NoiseStream(self.seed)

    def measure(self, q_true: al.Quat, sun_body: al.Vec3, sun_lit: bool) -> tuple[al.Quat, bool]:
        """(measured quaternion, solution ok). The quaternion is noise on
        truth; when blinded the flag is False and the output must be
        ignored, as the estimator does. `sun_body` must be nonzero when
        `sun_lit` (it is a direction; the caller derives it from a unit
        sun vector)."""
        n = self._noise.next3()
        q = al.quat_multiply(q_true, _small_rotation(al.v_scale(n, self.sigma)))
        if sun_lit:
            cos_angle = al.v_dot(al.v_unit(sun_body), self.boresight)
            if cos_angle > math.cos(self.exclusion):
                return q, False
        return q, True


@dataclass
class Gyro:
    """Rate measurement with white noise and a TRUE bias the estimator
    may or may not know about."""

    sigma: float = 1e-5  # rad/s per axis, 1σ
    bias: al.Vec3 = (0.0, 0.0, 0.0)  # true bias, rad/s
    seed: int = 102

    def __post_init__(self) -> None:
        self._noise = _NoiseStream(self.seed)

    def measure(self, omega_true: al.Vec3) -> al.Vec3:
        n = self._noise.next3()
        return al.v_add(al.v_add(omega_true, self.bias), al.v_scale(n, self.sigma))


@dataclass
class SunSensor:
    """Body-frame sun vector with angular noise. Modeled as an
    all-sky suite of coarse heads: presence is purely an illumination
    question — ABSENT in eclipse, PRESENT otherwise."""

    sigma: float = math.radians(0.5)  # rad, 1σ angular error
    seed: int = 103

    def __post_init__(self) -> None:
        self._noise = _NoiseStream(self.seed)

    def measure(self, sun_body_true: al.Vec3, sun_lit: bool) -> tuple[al.Vec3, bool]:
        """`sun_body_true` must be nonzero when `sun_lit`."""
        if not sun_lit:
            return (0.0, 0.0, 0.0), False
        n = self._noise.next3()
        perturbed = al.quat_rotate(
            _small_rotation(al.v_scale(n, self.sigma)), al.v_unit(sun_body_true)
        )
        return perturbed, True


@dataclass
class Magnetometer:
    """Body-frame field measurement with additive noise, Tesla."""

    sigma: float = 1e-7  # T per axis, 1σ (~0.1 µT, typical fluxgate)
    seed: int = 104

    def __post_init__(self) -> None:
        self._noise = _NoiseStream(self.seed)

    def measure(self, b_body_true: al.Vec3) -> al.Vec3:
        n = self._noise.next3()
        return al.v_add(b_body_true, al.v_scale(n, self.sigma))


class EstimatorState(Enum):
    """Matches the XTCE EstimatorStateType labels."""

    CONVERGING = "CONVERGING"
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass
class AttitudeEstimator:
    """A deliberately simple attitude estimator with honest failure modes.

    With a valid star tracker solution the attitude IS that measurement
    (a full Kalman filter would smooth the noise; recorded as future
    work). Without one it dead-reckons on bias-compensated gyro rates —
    so a wrong `bias_estimate` (ADCS_SET_GYRO_BIAS) makes the solution
    genuinely drift during star-tracker outages, and drives the hold
    loop off-attitude even outside them. State: INVALID until the first
    star fix ever arrives (there is nothing to have converged FROM),
    CONVERGING for `convergence_time` after start or reset
    (ADCS_RESET_ESTIMATOR) once a fix exists, VALID while the star
    tracker feeds it, and INVALID again once a tracker outage outlives
    `dropout_limit`. Call `update` with non-decreasing t — a rewound
    clock re-opens the convergence window.
    """

    bias_estimate: al.Vec3 = (0.0, 0.0, 0.0)
    convergence_time: float = 30.0  # s of CONVERGING after start/reset
    dropout_limit: float = 20.0  # s of dead-reckoning before INVALID
    attitude: al.Quat = al.QUAT_IDENTITY
    rate: al.Vec3 = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        self._converge_until: float | None = None
        self._last_st: float | None = None
        self._t = 0.0

    def update(
        self,
        t: float,
        dt: float,
        st_quat: al.Quat,
        st_ok: bool,
        gyro_rate: al.Vec3,
    ) -> None:
        self._t = t
        if self._converge_until is None:
            self._converge_until = t + self.convergence_time
        self.rate = al.v_sub(gyro_rate, self.bias_estimate)
        if st_ok:
            self.attitude = st_quat
            self._last_st = t
        else:
            dq = al.quat_derivative(self.attitude, self.rate)
            self.attitude = al.quat_normalize(tuple(a + dt * d for a, d in zip(self.attitude, dq)))

    @property
    def state(self) -> EstimatorState:
        if self._converge_until is None:
            return EstimatorState.CONVERGING
        if self._last_st is None:
            return EstimatorState.INVALID
        # Time comparisons ride on the last update's inputs.
        if self._t < self._converge_until:
            return EstimatorState.CONVERGING
        if self._t - self._last_st > self.dropout_limit:
            return EstimatorState.INVALID
        return EstimatorState.VALID

    def reset(self, t: float) -> None:
        """ADCS_RESET_ESTIMATOR: keep the solution, restart convergence."""
        self._converge_until = t + self.convergence_time

    def set_bias_estimate(self, bias: al.Vec3) -> None:
        """ADCS_SET_GYRO_BIAS: operator override of the bias estimate."""
        self.bias_estimate = bias
