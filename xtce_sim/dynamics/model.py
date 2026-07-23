"""
The [_models] construct: a physics model declared in behavior TOML.

A model owns a slice of the telemetry space the way ramps and waves never
could: the ADCS fields are OUTPUTS of a simulated plant + sensor + control
stack, advanced every beacon tick, and the ADCS commands are INPUTS to it.
The TOML declares the physical configuration (inertia, wheel cluster,
controller response) and binds model outputs to XTCE fields
explicitly — every binding validated at load against the definition, in
the behavior engine's strict-and-total style:

    [_models.adcs]
    kind = "adcs"
    substep = 0.1                        # s of physics per RK4 step

    [_models.adcs.body]
    inertia = [12.0, 14.0, 9.0]          # kg·m², principal diagonal

    [[_models.adcs.wheels]]              # one table per wheel
    axis = [0.6, 0.0, 0.8]
    inertia = 0.02                       # kg·m²
    max_torque = 0.05                    # N·m
    max_speed = 600.0                    # rad/s

    [_environment.orbit]         # the shared world, NOT model-owned:
    altitude_km = 500.0          # one solar system per vehicle, every
    inclination_deg = 51.6       # model a tenant (see parse_environment)

    [_models.adcs.controller]
    response_time = 10.0                 # s (a duration, not a bandwidth)

    [_models.adcs.outputs]
    ADCS_MODE = "mode"
    ADCS_ATT_QUAT_Q1 = "quat_q1"
    # ... every bound field, explicitly

Commands route by role with conventional default names (ADCS_SET_MODE,
ADCS_SLEW_TO_QUATERNION, ...), overridable per role in
[_models.adcs.commands]; a named command must exist and carry the
arguments the role needs. Units at the boundary are the XTCE's: degrees
and deg/s for angles and rates, RPM for wheel speeds, µT for the field —
the model converts from its internal SI.

Telemetry honesty: attitude, rates, and pointing error are the
ESTIMATOR's view (what real telemetry reports), not plant truth; wheel
current derives from delivered motor torque and wheel temperature is a
quasi-static map of that current — both documented approximations.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.control import AttitudeController, ControlLaw, PDGains
from xtce_sim.dynamics.environment import CircularOrbit, Environment
from xtce_sim.dynamics.modes import AdcsMode, Magnetorquer, ModeMachine
from xtce_sim.dynamics.plant import Plant, WheelParams
from xtce_sim.dynamics.sensors import (
    AttitudeEstimator,
    EstimatorState,
    Gyro,
    Magnetometer,
    StarTracker,
    SunSensor,
)

logger = logging.getLogger("xtce_sim.dynamics")

RAD_S_TO_RPM = 60.0 / (2.0 * math.pi)

#: Command roles and their conventional command names + required arguments.
_COMMAND_ROLES = {
    "set_mode": ("ADCS_SET_MODE", ("Mode",)),
    "slew_to_quaternion": ("ADCS_SLEW_TO_QUATERNION", ("Q1", "Q2", "Q3", "Q4")),
    "slew_to_angles": ("ADCS_SLEW_TO_ANGLES", ("Roll", "Pitch", "Yaw")),
    "track_target": ("ADCS_TRACK_TARGET", ("Latitude", "Longitude")),
    "wheel_set_speed": ("ADCS_WHEEL_SET_SPEED", ("WheelId", "Speed")),
    "wheel_enable": ("ADCS_WHEEL_ENABLE", ("WheelId",)),
    "wheel_disable": ("ADCS_WHEEL_DISABLE", ("WheelId",)),
    "desaturate": ("ADCS_DESATURATE", ()),
    "mtq_enable": ("ADCS_MTQ_ENABLE", ("State",)),
    "reset_estimator": ("ADCS_RESET_ESTIMATOR", ()),
    "set_gyro_bias": ("ADCS_SET_GYRO_BIAS", ("BiasX", "BiasY", "BiasZ")),
}

# Wheel electrical/thermal telemetry constants (documented approximations).
_IDLE_CURRENT = 0.05  # A drawn by a spinning-but-unloaded wheel
_TORQUE_CONSTANT = 0.025  # N·m of torque per A of motor current
_AMBIENT_C = 20.0  # wheel housing ambient
_TEMP_RISE_C = 25.0  # quasi-static rise at full motor current


@dataclass
class AdcsModelConfig:
    """Validated [_models.<name>] table, ready to instantiate."""

    name: str
    inertia: al.Mat3
    wheels: tuple[WheelParams, ...]
    sun_axis_body: al.Vec3
    response_time: float
    substep: float
    max_dipole: float
    seed: int
    outputs: dict[str, str]  # XTCE field -> model source key
    commands: dict[str, str]  # role -> command name (validated present)

    def describe(self) -> list[str]:
        return [
            f"model {self.name}: rigid-body ADCS ({len(self.wheels)} wheels) "
            f"driving {len(self.outputs)} field(s), "
            f"{len(self.commands)} command(s)"
        ]


def parse_environment(body, error: Callable[[str], None]) -> Environment | None:
    """Validate the vehicle-level [_environment] table and build the world.

    There is exactly ONE solar system per vehicle — orbit, sun, eclipse,
    magnetic field — and every model is a tenant in it, not an owner of
    it. An absent table builds the default world (500 km at 51.6 deg,
    sun along ECI +X), matching the defaults the tables carry.
    """
    where = "[_environment]"
    if not isinstance(body, dict):
        error(f"{where}: must be a table")
        return None
    problems = _ErrorCounter(error)
    err = problems.error
    for key in sorted(set(body) - {"orbit", "sun_direction"}):
        err(f"{where}: unknown key {key!r}")
    orbit = _parse_orbit(body.get("orbit", {}), where, err)
    sun_direction = _unit_vec(
        body.get("sun_direction", [1.0, 0.0, 0.0]), f"{where}.sun_direction", err
    )
    if problems.count or orbit is None:
        return None
    return Environment(orbit=orbit, sun_direction=sun_direction or (1.0, 0.0, 0.0))


def default_environment() -> Environment:
    """The world used when no [_environment] table is declared."""
    return Environment(orbit=CircularOrbit(altitude=500.0e3))


def _reject_moved_world_keys(body: dict, where: str, err: Callable[[str], None]) -> None:
    """The world moved out of the model: one solar system per vehicle,
    declared once at [_environment]. The old keys get a pointed message,
    not a generic unknown-key shrug."""
    for moved, home in (("orbit", "orbit"), ("sun", "sun_direction")):
        if moved in body:
            err(
                f"{where}.{moved}: the world is shared, not model-owned — "
                f"declare it under [_environment] ({home})"
            )


def _source_keys(wheel_count: int) -> set[str]:
    """Every output key the runtime can produce."""
    keys = {
        "mode",
        "est_state",
        "pointing_err_deg",
        "momentum_total",
        "momentum_flag",
        "st_health",
        "st_valid",
        "mtq_state",
        "quat_q1",
        "quat_q2",
        "quat_q3",
        "quat_q4",
        "st_quat_q1",
        "st_quat_q2",
        "st_quat_q3",
        "st_quat_q4",
        "roll_deg",
        "pitch_deg",
        "yaw_deg",
        "rate_x_deg",
        "rate_y_deg",
        "rate_z_deg",
        "sun_x",
        "sun_y",
        "sun_z",
        "sun_present",
        "mag_x_ut",
        "mag_y_ut",
        "mag_z_ut",
    }
    for i in range(1, wheel_count + 1):
        keys |= {f"wheel{i}_speed_rpm", f"wheel{i}_current_a", f"wheel{i}_temp_c"}
    return keys


def parse_model(name: str, body, simdef, error: Callable[[str], None]):
    """Validate one [_models.<name>] table, dispatching on its ``kind``.

    Every kind reports problems via `error` (behavior-engine style: total,
    not fail-fast) and returns None if anything is wrong. The registry of
    kinds is this dispatch — a new model family adds a branch here.
    """
    where = f"[_models.{name}]"
    if not isinstance(body, dict):
        error(f"{where}: must be a table")
        return None
    kind = body.get("kind", "adcs")
    if kind == "power":
        # Local import: power.py reuses this module's validation helpers,
        # so the top level must not import it back.
        from xtce_sim.dynamics.power import parse_power_model

        return parse_power_model(name, body, simdef, error)
    if kind != "adcs":
        error(f"{where}: unknown model kind {kind!r} (one of 'adcs', 'power')")
        return None
    return _parse_adcs_model(name, body, simdef, error)


def _parse_adcs_model(
    name: str, body: dict, simdef, error: Callable[[str], None]
) -> AdcsModelConfig | None:
    where = f"[_models.{name}]"
    problems_before = _ErrorCounter(error)
    err = problems_before.error
    known = {
        "kind",
        "substep",
        "body",
        "wheels",
        "controller",
        "mtq",
        "sensors",
        "outputs",
        "commands",
    }
    _reject_moved_world_keys(body, where, err)
    for key in set(body) - known - {"orbit", "sun"}:
        err(f"{where}: unknown key {key!r}")

    inertia = _parse_inertia(body.get("body", {}), where, err)
    wheels = _parse_wheels(body.get("wheels"), where, err)
    controller = _sub_table(body, "controller", {"response_time", "sun_axis"}, where, err)
    mtq = _sub_table(body, "mtq", {"max_dipole"}, where, err)
    sensors = _sub_table(body, "sensors", {"seed"}, where, err)
    sun_axis = _unit_vec(
        controller.get("sun_axis", [0.0, 0.0, -1.0]),
        f"{where}.controller.sun_axis",
        err,
    )
    response_time = _positive(
        controller.get("response_time", 10.0),
        f"{where}.controller.response_time",
        err,
    )
    substep = _parse_substep(body.get("substep", 0.1), response_time, where, err)
    max_dipole = _positive(
        mtq.get("max_dipole", 5.0),
        f"{where}.mtq.max_dipole",
        err,
    )
    seed = sensors.get("seed", 1)
    if isinstance(seed, bool) or not isinstance(seed, int):
        err(f"{where}.sensors.seed: must be an integer")
        seed = 1

    commands = _parse_commands(body.get("commands", {}), simdef, where, err)
    mode_labels = _reachable_modes(commands, simdef, where, err)
    outputs = _parse_outputs(
        body.get("outputs", {}), simdef, len(wheels or ()), mode_labels, where, err
    )

    if problems_before.count or inertia is None or wheels is None:
        return None
    return AdcsModelConfig(
        name=name,
        inertia=inertia,
        wheels=wheels,
        sun_axis_body=sun_axis or (0.0, 0.0, -1.0),
        response_time=response_time or 10.0,
        substep=substep or 0.1,
        max_dipole=max_dipole or 5.0,
        seed=seed,
        outputs=outputs,
        commands=commands,
    )


class _ErrorCounter:
    def __init__(self, error: Callable[[str], None]) -> None:
        self._error = error
        self.count = 0

    def error(self, msg: str) -> None:
        self.count += 1
        self._error(msg)


def _finite(value) -> bool:
    """A real, usable number: inf and nan are valid TOML floats and must
    never reach the physics."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value, where: str, err) -> float | None:
    if not _finite(value) or value <= 0:
        err(f"{where}: must be a positive number")
        return None
    return float(value)


def _number(value, where: str, err) -> float | None:
    if not _finite(value):
        err(f"{where}: must be a finite number")
        return None
    return float(value)


def _sub_table(body, key: str, known: set[str], where: str, err) -> dict:
    """An optional sub-table: reject non-tables and unknown keys (a typo'd
    knob must not silently fall back to its default)."""
    table = body.get(key, {})
    if not isinstance(table, dict):
        err(f"{where}.{key}: must be a table")
        return {}
    for unknown in sorted(set(table) - known):
        err(f"{where}.{key}: unknown key {unknown!r}")
    return table


def _parse_substep(value, response_time: float | None, where: str, err) -> float | None:
    """The physics step, bounded: below by beacon-tick cost (a microsecond
    step would loop millions of times per tick), above by RK4 accuracy for
    the wheel servos and by sampled-loop stability against the controller
    bandwidth (the controller acts once per substep)."""
    substep = _positive(value, f"{where}.substep", err)
    if substep is None:
        return None
    if not 0.001 <= substep <= 1.0:
        err(f"{where}.substep: must be between 0.001 and 1.0 seconds")
        return None
    if response_time is not None and substep > response_time / 5.0:
        err(
            f"{where}.substep: must be at most response_time/5 "
            f"({response_time / 5.0:.3g} s) so the sampled control loop stays stable"
        )
        return None
    return substep


def _unit_vec(value, where: str, err) -> al.Vec3 | None:
    if not isinstance(value, list) or len(value) != 3 or not all(_finite(c) for c in value):
        err(f"{where}: must be a 3-element number array")
        return None
    try:
        return al.v_unit((float(value[0]), float(value[1]), float(value[2])))
    except ValueError:
        err(f"{where}: cannot be the zero vector")
        return None


def _parse_inertia(table, where: str, err) -> al.Mat3 | None:
    if not isinstance(table, dict):
        err(f"{where}.body: must be a table")
        return None
    for unknown in sorted(set(table) - {"inertia"}):
        err(f"{where}.body: unknown key {unknown!r}")
    diag = table.get("inertia")
    if (
        not isinstance(diag, list)
        or len(diag) != 3
        or not all(isinstance(c, (int, float)) for c in diag)
    ):
        err(f"{where}.body.inertia: must be [Ixx, Iyy, Izz] in kg*m^2")
        return None
    if any(not _finite(c) or c <= 0 for c in diag):
        err(f"{where}.body.inertia: moments must be positive")
        return None
    return al.m_diag(float(diag[0]), float(diag[1]), float(diag[2]))


def _parse_wheels(entries, where: str, err) -> tuple[WheelParams, ...] | None:
    if not isinstance(entries, list) or not entries:
        err(f"{where}: needs at least one [[_models.*.wheels]] entry")
        return None
    wheels = []
    for i, entry in enumerate(entries, start=1):
        w = f"{where}.wheels[{i}]"
        if not isinstance(entry, dict):
            err(f"{w}: must be a table")
            return None
        for unknown in sorted(
            set(entry) - {"axis", "inertia", "max_torque", "max_speed", "friction"}
        ):
            err(f"{w}: unknown key {unknown!r}")
        axis = _unit_vec(entry.get("axis"), f"{w}.axis", err)
        inertia = _positive(entry.get("inertia"), f"{w}.inertia", err)
        max_torque = _positive(entry.get("max_torque"), f"{w}.max_torque", err)
        max_speed = _positive(entry.get("max_speed"), f"{w}.max_speed", err)
        friction = entry.get("friction", 0.0)
        if not _finite(friction) or friction < 0:
            err(f"{w}.friction: must be a non-negative number")
            return None
        if None in (axis, inertia, max_torque, max_speed):
            return None
        wheels.append(
            WheelParams(
                axis=axis,
                inertia=inertia,
                max_torque=max_torque,
                max_speed=max_speed,
                friction=float(friction),
            )
        )
    return tuple(wheels)


def _parse_orbit(table, where: str, err) -> CircularOrbit | None:
    if not isinstance(table, dict):
        err(f"{where}.orbit: must be a table")
        return None
    for unknown in sorted(set(table) - {"altitude_km", "inclination_deg", "raan_deg", "phase_deg"}):
        err(f"{where}.orbit: unknown key {unknown!r}")
    altitude = _positive(table.get("altitude_km", 500.0), f"{where}.orbit.altitude_km", err)
    angles = {
        key: _number(table.get(key, default), f"{where}.orbit.{key}", err)
        for key, default in (("inclination_deg", 51.6), ("raan_deg", 0.0), ("phase_deg", 0.0))
    }
    if altitude is None or any(v is None for v in angles.values()):
        return None
    return CircularOrbit(
        altitude=altitude * 1e3,
        inclination=math.radians(angles["inclination_deg"]),
        raan=math.radians(angles["raan_deg"]),
        phase0=math.radians(angles["phase_deg"]),
    )


#: Label-emitting sources and every label they can produce, for load-time
#: enum compatibility checks. Everything else is numeric. The "mode"
#: source is handled separately: its label set depends on which command
#: roles the vehicle wires (see _reachable_modes).
_SOURCE_LABELS = {
    "est_state": tuple(s.value for s in EstimatorState),
    "momentum_flag": ("OK", "NEAR_SATURATION"),
    "st_health": ("OK", "FAULT"),
    "st_valid": ("OK", "FAULT"),
    "mtq_state": ("ON", "OFF"),
    "sun_present": ("PRESENT", "ABSENT"),
}


def _reachable_modes(commands: dict[str, str], simdef, where: str, err) -> tuple[str, ...]:
    """The mode labels this vehicle can actually reach through its wired
    command roles. A vehicle whose XTCE declares no TRACK_TARGET command
    legitimately omits TARGET_TRACK from its mode enum; demanding the
    full set would reject a correct ICD, so the mode binding is checked
    against reachability instead."""
    all_names = {m.name for m in AdcsMode}
    reachable = {"STANDBY"}  # the boot mode
    if "slew_to_quaternion" in commands or "slew_to_angles" in commands:
        reachable.add("INERTIAL_POINT")
    if "track_target" in commands:
        reachable.add("TARGET_TRACK")
    name = commands.get("set_mode")
    if name is None:
        return tuple(sorted(reachable))
    param = next(p for p in simdef.command_by_name(name).params if p.name == "Mode")
    if not param.enumerations:
        # An unconstrained Mode argument could name anything: assume all.
        return tuple(sorted(all_names))
    for label in sorted(set(param.enumerations) - all_names):
        err(f"{where}.commands.set_mode: {name} Mode label {label!r} is not an ADCS mode")
    return tuple(sorted(reachable | (set(param.enumerations) & all_names)))


def _parse_outputs(
    table, simdef, wheel_count: int, mode_labels: tuple[str, ...], where: str, err
) -> dict[str, str]:
    if not isinstance(table, dict) or not table:
        err(f"{where}.outputs: at least one field binding is required")
        return {}
    fields = {f.name: f for p in simdef.packets for f in p.fields}
    valid_keys = _source_keys(wheel_count)
    outputs = {}
    for fname, source in table.items():
        field = fields.get(fname)
        if field is None:
            err(f"{where}.outputs: unknown field {fname!r}")
            continue
        if source not in valid_keys:
            err(f"{where}.outputs: {fname}: unknown source {source!r}")
            continue
        problem = _binding_problem(field, source, mode_labels)
        if problem is not None:
            err(f"{where}.outputs: {fname}: {problem}")
            continue
        outputs[fname] = source
    return outputs


def _binding_problem(field, source: str, mode_labels: tuple[str, ...]) -> str | None:
    """Why this source's values could not survive storage into this field
    (None when the binding is sound). Everything here is knowable at load;
    the alternative is a store warning on every tick, forever."""
    labels = mode_labels if source == "mode" else _SOURCE_LABELS.get(source)
    if labels is not None:
        if field.python_type in ("string", "bytes"):
            return None
        if not field.enumerations:
            return (
                f"source {source!r} emits labels but the field is "
                f"{field.python_type} with no enumeration"
            )
        missing = [label for label in labels if label not in field.enumerations]
        if missing:
            return (
                f"source {source!r} label(s) {', '.join(missing)} "
                "missing from the field's enumeration"
            )
        return None
    if field.python_type in ("string", "bytes") or field.enumerations:
        return f"source {source!r} is numeric but the field is not"
    if field.calibrator is not None and not field.calibrator.is_invertible:
        return f"source {source!r} needs an invertible calibrator on the field"
    return None


def _parse_commands(table, simdef, where: str, err) -> dict[str, str]:
    if not isinstance(table, dict):
        err(f"{where}.commands: must be a table")
        table = {}
    for role in set(table) - set(_COMMAND_ROLES):
        err(f"{where}.commands: unknown role {role!r}")
    commands = {}
    for role, (default, required_args) in _COMMAND_ROLES.items():
        name = table.get(role, default)
        command = simdef.command_by_name(name)
        if command is None:
            if role in table:  # explicit binding to a missing command: error
                err(f"{where}.commands.{role}: unknown command {name!r}")
            continue  # default not present in this satellite: role unwired
        have = {p.name for p in command.params}
        missing = [a for a in required_args if a not in have]
        if missing:
            err(f"{where}.commands.{role}: {name} lacks argument(s) " + ", ".join(missing))
            continue
        commands[role] = name
    return commands


class AdcsModel:
    """The runtime: plant + sensors + estimator + controller + mode machine,
    advanced in fixed substeps and read out through the output bindings."""

    def __init__(self, config: AdcsModelConfig, environment: Environment) -> None:
        self.config = config
        self.environment = environment
        plant = Plant(inertia=config.inertia, wheels=config.wheels)
        controller = AttitudeController(
            plant=plant,
            gains=PDGains.critically_damped(config.inertia, 1.0 / config.response_time),
        )
        self.machine = ModeMachine(
            plant=plant,
            controller=controller,
            environment=environment,
            star_tracker=StarTracker(seed=config.seed * 7 + 101),
            gyro=Gyro(seed=config.seed * 7 + 102),
            sun_sensor=SunSensor(seed=config.seed * 7 + 103),
            magnetometer=Magnetometer(seed=config.seed * 7 + 104),
            estimator=AttitudeEstimator(),
            mtq=Magnetorquer(max_dipole=config.max_dipole),
            sun_axis_body=config.sun_axis_body,
        )
        self.t = 0.0
        self._accumulated = 0.0
        self._by_role = {name: role for role, name in config.commands.items()}
        # Run one tick so sensors and estimator hold real values before the
        # first beacon (an all-zeros first ADCS frame is not an attitude).
        self.machine.tick(self.t, config.substep)

    # -- commands --------------------------------------------------------------

    def handles(self, command_name: str) -> bool:
        return command_name in self._by_role

    def apply_command(self, command_name: str, args: dict) -> list[str]:
        """Route a decoded command into the model. Loud-but-liberal like
        every behavior application: a bad value warns and applies nothing
        (the vehicle's range validation already rejected wire-level
        violations before this point)."""
        role = self._by_role[command_name]
        try:
            return [getattr(self, f"_cmd_{role}")(args)]
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            logger.warning("[%s] %s: %s; skipped", self.config.name, command_name, exc)
            return []

    def _cmd_set_mode(self, args: dict) -> str:
        mode = AdcsMode[args["Mode"]]
        self.machine.set_mode(mode)
        return f"mode -> {mode.name}"

    def _cmd_slew_to_quaternion(self, args: dict) -> str:
        target = al.quat_normalize((args["Q1"], args["Q2"], args["Q3"], args["Q4"]))
        self.machine.set_inertial_target(target)
        self.machine.set_mode(AdcsMode.INERTIAL_POINT)
        return f"slew to quaternion {tuple(round(c, 4) for c in target)}"

    def _cmd_slew_to_angles(self, args: dict) -> str:
        target = al.euler321_to_quat(
            math.radians(args["Roll"]),
            math.radians(args["Pitch"]),
            math.radians(args["Yaw"]),
        )
        self.machine.set_inertial_target(target)
        self.machine.set_mode(AdcsMode.INERTIAL_POINT)
        return f"slew to roll/pitch/yaw ({args['Roll']}, {args['Pitch']}, {args['Yaw']}) deg"

    def _cmd_track_target(self, args: dict) -> str:
        self.machine.set_ground_target(
            math.radians(args["Latitude"]), math.radians(args["Longitude"])
        )
        return f"tracking ground target ({args['Latitude']}, {args['Longitude']}) deg"

    def _wheel_index(self, args: dict) -> int:
        wheel = int(args["WheelId"]) - 1
        if not 0 <= wheel < len(self.config.wheels):
            raise ValueError(f"WheelId {args['WheelId']} out of range")
        return wheel

    def _cmd_wheel_set_speed(self, args: dict) -> str:
        wheel = self._wheel_index(args)
        self.machine.plant.command_speed(wheel, args["Speed"] / RAD_S_TO_RPM)
        return f"wheel {wheel + 1} speed target {args['Speed']} RPM"

    def _cmd_wheel_enable(self, args: dict) -> str:
        wheel = self._wheel_index(args)
        self.machine.plant.set_enabled(wheel, True)
        return f"wheel {wheel + 1} enabled"

    def _cmd_wheel_disable(self, args: dict) -> str:
        wheel = self._wheel_index(args)
        self.machine.plant.set_enabled(wheel, False)
        return f"wheel {wheel + 1} disabled"

    def _cmd_desaturate(self, _args: dict) -> str:
        self.machine.request_desaturation()
        return "momentum dump engaged"

    def _cmd_mtq_enable(self, args: dict) -> str:
        self.machine.mtq.enabled = args["State"] == "ON"
        return f"magnetorquer chain {args['State']}"

    def _cmd_reset_estimator(self, _args: dict) -> str:
        self.machine.estimator.reset(self.t)
        return "estimator reset (reconverging)"

    def _cmd_set_gyro_bias(self, args: dict) -> str:
        bias = tuple(math.radians(args[k]) for k in ("BiasX", "BiasY", "BiasZ"))
        self.machine.estimator.set_bias_estimate(bias)
        return f"gyro bias estimate ({args['BiasX']}, {args['BiasY']}, {args['BiasZ']}) deg/s"

    # -- time ------------------------------------------------------------------

    def advance(self, dt: float) -> None:
        """Advance physics by dt using whole fixed substeps; the remainder
        accumulates so long-run time never drifts."""
        self._accumulated += dt
        h = self.config.substep
        while self._accumulated >= h:
            self.machine.tick(self.t, h)
            self.machine.plant.step(h)
            self.t += h
            self._accumulated -= h

    # -- outputs ----------------------------------------------------------------

    def outputs(self) -> dict[str, object]:
        """Engineering-unit values for every bound field."""
        values = self._sources()
        return {fname: values[source] for fname, source in self.config.outputs.items()}

    def _sources(self) -> dict[str, object]:
        m = self.machine
        est = m.estimator
        ctl = m.controller
        roll, pitch, yaw = al.quat_to_euler321(est.attitude)
        pointing = (
            math.degrees(ctl.pointing_error()) if ctl.law is ControlLaw.ATTITUDE_HOLD else 0.0
        )
        values: dict[str, object] = {
            "mode": m.mode.name,
            "est_state": est.state.value,
            "pointing_err_deg": pointing,
            "momentum_total": m.momentum_total(),
            "momentum_flag": "NEAR_SATURATION" if m.near_saturation() else "OK",
            "st_health": "OK" if m.st_ok else "FAULT",
            "st_valid": "OK" if m.st_ok else "FAULT",
            "mtq_state": "ON" if m.mtq.enabled else "OFF",
            "quat_q1": est.attitude[0],
            "quat_q2": est.attitude[1],
            "quat_q3": est.attitude[2],
            "quat_q4": est.attitude[3],
            "st_quat_q1": m.st_quat[0],
            "st_quat_q2": m.st_quat[1],
            "st_quat_q3": m.st_quat[2],
            "st_quat_q4": m.st_quat[3],
            "roll_deg": math.degrees(roll),
            "pitch_deg": math.degrees(pitch),
            "yaw_deg": math.degrees(yaw),
            "rate_x_deg": math.degrees(est.rate[0]),
            "rate_y_deg": math.degrees(est.rate[1]),
            "rate_z_deg": math.degrees(est.rate[2]),
            "sun_x": m.sun_vector[0],
            "sun_y": m.sun_vector[1],
            "sun_z": m.sun_vector[2],
            "sun_present": "PRESENT" if m.sun_present else "ABSENT",
            "mag_x_ut": m.mag_body[0] * 1e6,
            "mag_y_ut": m.mag_body[1] * 1e6,
            "mag_z_ut": m.mag_body[2] * 1e6,
        }
        plant = m.plant
        for i in range(len(self.config.wheels)):
            current = self._wheel_current(i)
            max_current = _IDLE_CURRENT + (self.config.wheels[i].max_torque / _TORQUE_CONSTANT)
            values[f"wheel{i + 1}_speed_rpm"] = plant.wheel_speed(i) * RAD_S_TO_RPM
            values[f"wheel{i + 1}_current_a"] = current
            # Quasi-static thermal map: dissipation scales with current^2.
            values[f"wheel{i + 1}_temp_c"] = (
                _AMBIENT_C + _TEMP_RISE_C * (current / max_current) ** 2
            )
        return values

    def _wheel_current(self, i: int) -> float:
        return _IDLE_CURRENT + abs(self.machine.plant.wheel_torque(i)) / _TORQUE_CONSTANT

    def wheel_current_total(self) -> float:
        """The wheels' summed motor current — the same amps ADCS_WHEELS
        telemeters, offered to the power model as one bus draw."""
        return sum(self._wheel_current(i) for i in range(len(self.config.wheels)))
