"""
The spacecraft plant: one rigid body plus a cluster of reaction wheels.

This is the physical truth the rest of the ADCS stack observes and
commands. The state is the attitude quaternion (body → reference), the
body angular rates, and each wheel's stored angular momentum; everything
else (Euler angles, pointing error, total momentum) derives from it.

Equations of motion — Euler's rotational equation with momentum exchange:

    I ω̇ = −ω × (I ω + h_wheels) − Σ aᵢ ḣᵢ + τ_ext
    ḣᵢ  = uᵢ − fᵢ Ωᵢ
    q̇   = ½ q ⊗ (ω, 0)

where h_wheels = Σ aᵢ hᵢ is the wheel momentum resolved into the body
frame, uᵢ is the motor torque applied TO wheel i about its axis aᵢ, and
fᵢ Ωᵢ is viscous bearing drag. The body feels the reaction −aᵢ ḣᵢ to
everything the wheel exchanges — motor torque and friction alike — so
with τ_ext = 0 the total angular momentum R(q)(I ω + h_wheels) is
conserved in the reference frame no matter what the motors do; the
momentum-conservation tests pin exactly that. τ_ext is any external
torque in the body frame (environment disturbances, magnetorquers).
hᵢ = I_wᵢ Ωᵢ uses the wheel speed RELATIVE to the body — what a real
tachometer reads — omitting the small I_w aᵀω carried term, the standard
small-wheel approximation; the conservation law above still closes
exactly within the model.

Wheel motors accept either a torque command (what a feedback controller
sends) or a speed target (what ADCS_WHEEL_SET_SPEED sends); a speed
target becomes a proportional torque servo saturated at the motor limit,
which yields the real article's constant-torque spin-up ending in an
exponential approach. Delivered torque is always limited to ±max_torque,
and a wheel at max_speed refuses torque that would push it further —
saturation is emergent, not scripted. A disabled wheel delivers no motor
torque and coasts down through bearing friction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xtce_sim.dynamics import algebra as al


@dataclass(frozen=True)
class WheelParams:
    """One reaction wheel, fixed in the body frame."""

    axis: al.Vec3  # spin axis, body frame; normalized at Plant construction
    inertia: float  # about the spin axis, kg·m²
    max_torque: float  # motor limit, N·m
    max_speed: float  # speed limit, rad/s
    friction: float = 0.0  # viscous bearing drag, N·m per rad/s
    # Speed-servo bandwidth: (N·m per rad/s of speed error) per (kg·m² of
    # wheel inertia), i.e. a 1/tau — the unsaturated closing time constant
    # is 1/servo_gain seconds regardless of wheel size; large errors ride
    # the torque limit instead. RK4 needs h·servo_gain < 2.785 for
    # stability, so the default leaves ~14x margin at 0.1 s substeps.
    servo_gain: float = 2.0


@dataclass
class WheelCommand:
    """What the motor controller of one wheel is currently asked to do."""

    torque: float = 0.0  # commanded motor torque, N·m (torque mode)
    speed_target: float | None = None  # rad/s; overrides torque when set
    enabled: bool = True


@dataclass
class PlantState:
    """The complete physical state; everything telemetered derives from it."""

    quat: al.Quat = al.QUAT_IDENTITY  # body → reference
    omega: al.Vec3 = (0.0, 0.0, 0.0)  # body rates, rad/s
    wheel_momentum: tuple[float, ...] = ()  # hᵢ about each wheel axis, N·m·s


def _check_inertia(inertia: al.Mat3) -> None:
    """Reject a non-physical inertia tensor loudly at construction.

    Symmetry is tested RELATIVE to the tensor's scale (an ISS-class tensor
    at ~1e8 kg·m² legitimately carries ~1e-8 of rounding asymmetry after a
    frame rotation). Positive-definiteness uses Sylvester's criterion on
    the leading principal minors — det > 0 alone is NOT sufficient: a
    symmetric matrix with two negative eigenvalues also has det > 0.
    """
    scale = max(abs(x) for row in inertia for x in row)
    for i in range(3):
        for j in range(i + 1, 3):
            if abs(inertia[i][j] - inertia[j][i]) > 1e-9 * scale:
                raise ValueError("inertia tensor must be symmetric")
    minor1 = inertia[0][0]
    minor2 = inertia[0][0] * inertia[1][1] - inertia[0][1] * inertia[1][0]
    if minor1 <= 0.0 or minor2 <= 0.0 or al.m_det(inertia) <= 0.0:
        raise ValueError("inertia tensor must be positive-definite")


def _validated_wheels(wheels: tuple[WheelParams, ...]) -> tuple[WheelParams, ...]:
    """Wheels with normalized axes; raises on physically senseless params."""
    normalized = []
    for n, w in enumerate(wheels):
        if min(w.inertia, w.max_torque, w.max_speed, w.servo_gain) <= 0.0:
            raise ValueError(
                f"wheel {n}: inertia, max_torque, max_speed, and servo_gain must be positive"
            )
        if w.friction < 0.0:
            raise ValueError(f"wheel {n}: friction cannot be negative")
        try:
            axis = al.v_unit(w.axis)
        except ValueError:
            raise ValueError(f"wheel {n}: spin axis cannot be zero") from None
        normalized.append(
            WheelParams(axis, w.inertia, w.max_torque, w.max_speed, w.friction, w.servo_gain)
        )
    return tuple(normalized)


@dataclass
class Plant:
    """Rigid body + wheels, advanced by fixed-step RK4 substeps.

    `step()` REPLACES `self.state` with a fresh PlantState rather than
    mutating it (and construction copies the one it is given), so read
    `plant.state` afresh after each step. `external_torque` is the sum of
    every external contributor (environment, magnetorquers) — assemble it
    before a step and do not mutate it between the substeps of one tick.
    """

    inertia: al.Mat3
    wheels: tuple[WheelParams, ...]
    state: PlantState = field(default_factory=PlantState)
    external_torque: al.Vec3 = (0.0, 0.0, 0.0)  # body frame, N·m

    def __post_init__(self) -> None:
        _check_inertia(self.inertia)
        self._inertia_inv = al.m_inverse(self.inertia)
        self.wheels = _validated_wheels(self.wheels)
        self.commands = tuple(WheelCommand() for _ in self.wheels)
        momentum = self.state.wheel_momentum or (0.0,) * len(self.wheels)
        if len(momentum) != len(self.wheels):
            raise ValueError("wheel_momentum length must match the wheel count")
        # A private copy: never alias or mutate the caller's PlantState.
        self.state = PlantState(self.state.quat, self.state.omega, tuple(momentum))

    # -- observables --------------------------------------------------------

    def wheel_speed(self, i: int) -> float:
        """Wheel i's speed in rad/s (momentum over inertia)."""
        return self.state.wheel_momentum[i] / self.wheels[i].inertia

    def wheel_momentum_body(self) -> al.Vec3:
        """Total wheel momentum resolved into the body frame, N·m·s."""
        total = (0.0, 0.0, 0.0)
        for w, h in zip(self.wheels, self.state.wheel_momentum):
            total = al.v_add(total, al.v_scale(w.axis, h))
        return total

    def total_momentum_reference(self) -> al.Vec3:
        """System angular momentum in the reference frame — conserved when
        no external torque acts, regardless of wheel activity."""
        body = al.v_add(al.m_vec(self.inertia, self.state.omega), self.wheel_momentum_body())
        return al.quat_rotate(self.state.quat, body)

    # -- commanding ----------------------------------------------------------

    def command_torque(self, i: int, torque: float) -> None:
        """Torque-mode command for wheel i (a feedback controller's output)."""
        self.commands[i].torque = torque
        self.commands[i].speed_target = None

    def command_speed(self, i: int, speed: float) -> None:
        """Speed-target command for wheel i (ADCS_WHEEL_SET_SPEED semantics)."""
        self.commands[i].speed_target = speed

    def set_enabled(self, i: int, enabled: bool) -> None:
        """Enable or disable wheel i's motor; a disabled wheel coasts on
        friction alone and ignores torque and speed commands."""
        self.commands[i].enabled = enabled

    # -- dynamics ------------------------------------------------------------

    def wheel_torque(self, i: int) -> float:
        """Torque wheel i's motor delivers at the current state, N·m — the
        basis for wheel-current telemetry and controller anti-windup (a
        controller commanding past max_torque can see it saturated)."""
        return self._motor_torque(i, self.state.wheel_momentum[i])

    def _motor_torque(self, i: int, h: float) -> float:
        """Torque delivered by wheel i's motor at wheel momentum h."""
        cmd = self.commands[i]
        w = self.wheels[i]
        if not cmd.enabled:
            return 0.0
        speed = h / w.inertia
        if cmd.speed_target is not None:
            u = w.servo_gain * w.inertia * (cmd.speed_target - speed)
        else:
            u = cmd.torque
        u = max(-w.max_torque, min(w.max_torque, u))
        # At the speed limit the drive electronics refuse to push further;
        # torque back toward zero speed still works.
        past_rail = (speed >= w.max_speed and u > 0.0) or (speed <= -w.max_speed and u < 0.0)
        return 0.0 if past_rail else u

    def _derivative(self, y: al.State) -> al.State:
        q = (y[0], y[1], y[2], y[3])
        omega = (y[4], y[5], y[6])
        wheel_h = y[7:]

        h_body = (0.0, 0.0, 0.0)
        reaction = (0.0, 0.0, 0.0)
        h_dots = []
        for i, w in enumerate(self.wheels):
            u = self._motor_torque(i, wheel_h[i])
            drag = -w.friction * (wheel_h[i] / w.inertia)
            h_dots.append(u + drag)
            h_body = al.v_add(h_body, al.v_scale(w.axis, wheel_h[i]))
            # The body feels the reaction to everything the wheel exchanges,
            # friction included: bearing drag pushes momentum back into the
            # body instead of destroying it.
            reaction = al.v_add(reaction, al.v_scale(w.axis, -(u + drag)))

        momentum = al.v_add(al.m_vec(self.inertia, omega), h_body)
        torque = al.v_add(al.v_sub(reaction, al.v_cross(omega, momentum)), self.external_torque)
        omega_dot = al.m_vec(self._inertia_inv, torque)
        q_dot = al.quat_derivative(q, omega)
        return (*q_dot, *omega_dot, *h_dots)

    def step(self, dt: float) -> None:
        """Advance the plant by one RK4 step of size dt and renormalize the
        attitude. Callers pick dt small enough for the fastest dynamics
        (the integration layer substeps each beacon interval)."""
        if dt <= 0.0:
            raise ValueError("dt must be positive")
        s = self.state
        y = (*s.quat, *s.omega, *s.wheel_momentum)
        y = al.rk4_step(self._derivative, y, dt)
        self.state = PlantState(
            quat=al.quat_normalize((y[0], y[1], y[2], y[3])),
            omega=(y[4], y[5], y[6]),
            wheel_momentum=tuple(y[7:]),
        )
