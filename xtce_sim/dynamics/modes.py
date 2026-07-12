"""
The ADCS mode machine: six flight modes wired to laws, references,
sensors, and magnetorquers.

This is the flight-software layer above the controller. Each tick it
reads the SENSORS (built from plant truth and the environment), feeds
the ESTIMATOR, points the CONTROLLER at the active mode's reference
attitude and rate, and runs the MAGNETORQUER laws — so the whole chain
the ground sees (mode → sensors → estimate → torque → motion →
telemetry) is causally real. The loop closes on estimates, never truth.

Modes, per the XTCE AdcsModeType:

- STANDBY        — controller idle; manual wheel commands stand.
- DETUMBLE       — wheels idle, magnetorquers run the B-dot law
                   m = −k·dB/dt on the MEASURED field: as the field
                   turns along the orbit, tumble energy bleeds out. The
                   flight-realistic magnetic detumble, and exactly as
                   slow as the real one (orbit-timescale).
- SUNSAFE        — hold the configured body axis on the sun line.
- NADIR          — track the LVLH frame (+Z nadir, +X along-track),
                   with orbit-rate feedforward.
- TARGET_TRACK   — track a commanded ground target through Earth
                   rotation, feedforward included.
- INERTIAL_POINT — hold the commanded inertial quaternion (the slew
                   commands' destination).

Desaturation (ADCS_DESATURATE) may be requested in any mode but ENGAGES
only while the controller is holding attitude: the cross-product law
m = k·(h × B)/|B|² torques the body against its wheel momentum, and it
is the hold loop absorbing that reaction into the wheels that makes the
stored momentum drain — the classic dump, emergent from the physics
rather than scripted. Without a hold the same torque would only spin
the vehicle, so in STANDBY/DETUMBLE the request stays pending (the
`desaturating` flag is telemetry-visible) until a pointing mode is
active. It disengages below `desat_stop`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.control import AttitudeController, ControlLaw
from xtce_sim.dynamics.environment import Environment, reference_rate
from xtce_sim.dynamics.plant import Plant
from xtce_sim.dynamics.sensors import (
    AttitudeEstimator,
    Gyro,
    Magnetometer,
    StarTracker,
    SunSensor,
)


class AdcsMode(Enum):
    """Matches the XTCE AdcsModeType labels and raw values."""

    DETUMBLE = 0
    SUNSAFE = 1
    NADIR = 2
    TARGET_TRACK = 3
    INERTIAL_POINT = 4
    STANDBY = 5


@dataclass
class Magnetorquer:
    """Three orthogonal torque rods: dipole command in, m × B torque out.

    The commanded dipole is clamped PER AXIS (each rod saturates on its
    own), and a disabled chain (ADCS_MTQ_ENABLE OFF) produces nothing.
    """

    max_dipole: float = 5.0  # A·m² per axis
    enabled: bool = True

    def actuate(self, dipole: al.Vec3, b_body: al.Vec3) -> tuple[al.Vec3, al.Vec3]:
        """(torque N·m, clamped dipole actually driven)."""
        if not self.enabled:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        m = tuple(max(-self.max_dipole, min(self.max_dipole, c)) for c in dipole)
        return al.v_cross(m, b_body), m


@dataclass
class ModeMachine:
    """Ticks the whole ADCS: sensors → estimator → mode → actuators.

    Call `tick(t, dt)` once per control step, then advance the plant by
    dt. Commands arrive between ticks via the setter methods, matching
    the vehicle's dispatch-before-control ordering (the unit 2 review's
    requirement that a re-enabled wheel never fires a stale torque).
    """

    plant: Plant
    controller: AttitudeController
    environment: Environment
    star_tracker: StarTracker = field(default_factory=StarTracker)
    gyro: Gyro = field(default_factory=Gyro)
    sun_sensor: SunSensor = field(default_factory=SunSensor)
    magnetometer: Magnetometer = field(default_factory=Magnetometer)
    estimator: AttitudeEstimator = field(default_factory=AttitudeEstimator)
    mtq: Magnetorquer = field(default_factory=Magnetorquer)
    sun_axis_body: al.Vec3 = (0.0, 0.0, -1.0)  # solar panels opposite imager
    bdot_gain: float = 1e8  # A·m² per T/s; saturates during a real tumble
    bdot_filter_tau: float = 5.0  # s low-pass on dB/dt — raw differenced
    # magnetometer noise (~2e-6 T/s at 0.1 s ticks) swamps the orbital
    # signal (~1e-6 T/s); every flight B-dot filters for the same reason.
    desat_gain: float = 5e-3  # 1/s momentum-dump rate (dipole-limited)
    desat_stop: float = 0.02  # N·m·s: dump disengages below this

    def __post_init__(self) -> None:
        self.mode = AdcsMode.STANDBY
        self.inertial_target: al.Quat = al.QUAT_IDENTITY
        self.target_lat: float | None = None
        self.target_lon: float | None = None
        self.desaturating = False
        # The controller must fly on the estimate, not on plant truth.
        self.controller.state_provider = lambda: (
            self.estimator.attitude,
            self.estimator.rate,
        )
        self._prev_b: al.Vec3 | None = None
        self._bdot: al.Vec3 | None = None
        # Telemetry snapshot (unit 4 reads these after each tick).
        self.sun_present = False
        self.sun_vector: al.Vec3 = (0.0, 0.0, 0.0)
        self.mag_body: al.Vec3 = (0.0, 0.0, 0.0)
        self.st_ok = True
        self.st_quat: al.Quat = al.QUAT_IDENTITY  # last raw ST measurement
        self.mtq_dipole: al.Vec3 = (0.0, 0.0, 0.0)

    # -- commands --------------------------------------------------------------

    def set_mode(self, mode: AdcsMode) -> None:
        """ADCS_SET_MODE. TARGET_TRACK requires a target first."""
        if mode is AdcsMode.TARGET_TRACK and self.target_lat is None:
            raise ValueError("TARGET_TRACK commanded with no ground target set")
        if mode in (AdcsMode.STANDBY, AdcsMode.DETUMBLE):
            self.controller.idle()
        self.mode = mode
        self._prev_b = None  # B-dot restarts cleanly on any transition
        self._bdot = None

    def set_inertial_target(self, target: al.Quat) -> None:
        """The slew commands' destination; flies in INERTIAL_POINT mode."""
        self.inertial_target = al.quat_normalize(target)

    def set_ground_target(self, lat: float, lon: float) -> None:
        """ADCS_TRACK_TARGET (radians); also enters TARGET_TRACK, matching
        the command's documented behavior."""
        self.target_lat = lat
        self.target_lon = lon
        self.set_mode(AdcsMode.TARGET_TRACK)

    def request_desaturation(self) -> None:
        """ADCS_DESATURATE: dump wheel momentum until below `desat_stop`."""
        self.desaturating = True

    # -- the tick ----------------------------------------------------------------

    def tick(self, t: float, dt: float) -> None:
        truth = self.plant.state
        q_inv = al.quat_conjugate(truth.quat)
        sun_lit = self.environment.sun_visible(t)
        sun_body = al.quat_rotate(q_inv, self.environment.sun_direction)
        b_body = al.quat_rotate(q_inv, self.environment.magnetic_field(t))

        st_quat, self.st_ok = self.star_tracker.measure(truth.quat, sun_body, sun_lit)
        self.st_quat = st_quat
        gyro_rate = self.gyro.measure(truth.omega)
        self.sun_vector, self.sun_present = self.sun_sensor.measure(sun_body, sun_lit)
        self.mag_body = self.magnetometer.measure(b_body)
        self.estimator.update(t, dt, st_quat, self.st_ok, gyro_rate)

        self._point(t)
        self.controller.update()

        dipole = self._mtq_command(dt)
        torque, self.mtq_dipole = self.mtq.actuate(dipole, b_body)
        # This assignment is the aggregation point for ALL external torques:
        # when disturbances (gravity gradient, drag) arrive, sum them here.
        self.plant.external_torque = torque
        self._prev_b = self.mag_body

    def _point(self, t: float) -> None:
        """Aim the controller for the active mode."""
        env = self.environment
        if self.mode is AdcsMode.NADIR:
            self.controller.hold_attitude(
                env.nadir_attitude(t), reference_rate(env.nadir_attitude, t)
            )
        elif self.mode is AdcsMode.TARGET_TRACK:

            def att(when: float) -> al.Quat:
                return env.target_attitude(when, self.target_lat, self.target_lon)

            self.controller.hold_attitude(att(t), reference_rate(att, t))
        elif self.mode is AdcsMode.SUNSAFE:
            self.controller.hold_attitude(env.sun_attitude(self.sun_axis_body))
        elif self.mode is AdcsMode.INERTIAL_POINT:
            self.controller.hold_attitude(self.inertial_target)
        # STANDBY and DETUMBLE: controller stays idle (set at transition).

    def _mtq_command(self, dt: float) -> al.Vec3:
        """Dipole request from the avionics' MEASURED field (the torque is
        later computed against the TRUE field — the sensor gap is real)."""
        dipole = (0.0, 0.0, 0.0)
        b = self.mag_body
        if self.mode is AdcsMode.DETUMBLE and self._prev_b is not None:
            raw = al.v_scale(al.v_sub(b, self._prev_b), 1.0 / dt)
            if self._bdot is None:
                self._bdot = raw
            else:
                blend = dt / (dt + self.bdot_filter_tau)
                self._bdot = al.v_add(self._bdot, al.v_scale(al.v_sub(raw, self._bdot), blend))
            dipole = al.v_scale(self._bdot, -self.bdot_gain)
        if self.desaturating:
            h = self.plant.wheel_momentum_body()
            if al.v_norm(h) <= self.desat_stop:
                self.desaturating = False
            elif self.controller.law is ControlLaw.ATTITUDE_HOLD:
                # The dump only works while the controller holds attitude —
                # the wheels must absorb the MTQ reaction for their stored
                # momentum to drain. Without a hold the same torque just
                # spins the vehicle, so the request stays PENDING (flag
                # readable in telemetry) until a pointing mode is active.
                b_sq = al.v_dot(b, b)
                if b_sq > 0.0:
                    # m = k (h × B)/|B|² gives torque ≈ −k·h_perp: the body
                    # is pushed against its wheel bias, the hold loop makes
                    # the wheels absorb it, and stored momentum drains.
                    dipole = al.v_add(
                        dipole,
                        al.v_scale(al.v_cross(h, b), self.desat_gain / b_sq),
                    )
        return dipole

    # -- observables --------------------------------------------------------------

    def momentum_total(self) -> float:
        """|wheel momentum| in N·m·s — ADCS_MOMENTUM_TOTAL's source."""
        return al.v_norm(self.plant.wheel_momentum_body())

    def near_saturation(self, fraction: float = 0.8) -> bool:
        """ADCS_MOMENTUM_FLAG: any wheel beyond `fraction` of its speed
        limit counts — saturation is per wheel, not per axis."""
        return any(
            abs(self.plant.wheel_speed(i)) >= fraction * w.max_speed
            for i, w in enumerate(self.plant.wheels)
        )


def latlon_degrees(lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Radians from the degree arguments ADCS_TRACK_TARGET carries."""
    return math.radians(lat_deg), math.radians(lon_deg)
