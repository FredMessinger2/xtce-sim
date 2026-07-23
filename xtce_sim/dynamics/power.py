"""
The [_models] power model: generation, a real battery, and switched loads.

The electrical power system as a tenant of the shared [_environment]
world: solar generation follows the SAME sun and eclipse the ADCS flies
in, scaled by how squarely the wings can face the sun given the body
attitude; the battery is a state — charge that integrates every tick —
whose terminal voltage follows charge and sags under load; and each
switched load draws its configured current while its PWR_*_STATE reads
ON. Battery current is signed: POSITIVE while charging, NEGATIVE while
discharging.

    [_models.power]
    kind = "power"

    [_models.power.array]
    wing_power_w = 60.0          # Pmp per wing, BOL, sun-normal
    wings = 2
    vmp = 28.0                   # array max-power voltage
    mppt_efficiency = 0.95
    sada_axis = [0.0, 1.0, 0.0]  # the wings' rotation axis, body frame

    [_models.power.battery]
    capacity_ah = 10.0
    cells = 6                    # Li-ion cells in series
    internal_resistance = 0.15   # ohm, whole string
    charge_current_max = 2.0     # A, controller limit (tapers near full)
    initial_soc = 0.75

    [_models.power.loads]        # every load gates on PWR_<name>_STATE == ON
    CDH = 0.3                    # the simple form: flat amps while ON
    ADCS = { base = 0.3, wheels = true }
    IMAGER = { by = "IMG_STATE", amps = { IDLE = 0.2, CAPTURING = 0.8 } }
    HEATER = { per_element = 0.4, elements = { THM_HEATER1_STATE = "THM_HEATER1_TEMP" } }

    [_models.power.outputs]      # model outputs -> XTCE fields
    PWR_SOLAR_VOLTAGE = "solar_voltage"
    PWR_SOLAR_CURRENT = "solar_current"
    PWR_BATTERY_VOLTAGE = "battery_voltage"
    PWR_BATTERY_CURRENT = "battery_current"

Documented approximations (each invisible from a ground console, each
recorded here rather than hidden):

- SADA tracking is PERFECT about its single axis: the only off-pointing
  that costs power is the sun component along the axis itself (which no
  wing rotation can recover). Illumination = sqrt(1 - (s_body . axis)^2),
  zero in eclipse. Without an attitude source (a vehicle with no ADCS
  model), the wings are assumed sun-pointed.
- Array voltage reads Vmp whenever illuminated and ~0 in eclipse (real
  Vmp holds until extreme off-pointing; the transition is sharpened).
- The open-circuit voltage curve is linear in state of charge
  (3.4 V empty to 4.2 V full per cell — real Li-ion has a flatter
  middle); terminal voltage adds I*R signed by charge/discharge.
- The charge controller tapers linearly over the top 10% of charge and
  shunts excess generation (a full battery in full sun simply wastes
  the surplus, as real shunt regulators do).
- A load's draw composes up to four physical parts, all gated by its LCL
  (PWR_<name>_STATE == ON; anything else draws nothing): a flat ``base``;
  an activity-keyed part (``by`` names an enum field, ``amps`` gives the
  draw per label, unlisted labels draw 0); the ADCS model's live wheel
  currents (``wheels = true`` — the same amps ADCS_WHEELS telemeters);
  and duty-cycled elements (``per_element`` amps each, ``elements`` maps
  a mode field to the field it regulates — the element draws while its
  mode reads ON, or in AUTO exactly while the regulate loop's element is
  on, so the thermostat's duty sawtooth becomes a power sawtooth).
- Switching an LCL OFF only stops the draw — it does not halt the
  subsystem behind it (the ADCS keeps flying with its power "off"; LCL
  feedback into the models is a documented limit).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from xtce_sim.dynamics import algebra as al
from xtce_sim.dynamics.environment import Environment

logger = logging.getLogger("xtce_sim.dynamics")

_V_CELL_EMPTY = 3.4  # V, open-circuit at 0% charge
_V_CELL_FULL = 4.2  # V, open-circuit at 100% charge
_TAPER_BAND = 0.10  # top fraction of charge over which charging tapers

#: Every output key the runtime can produce (all numeric).
_SOURCE_KEYS = frozenset(
    {"solar_voltage", "solar_current", "battery_voltage", "battery_current"}
)


@dataclass(frozen=True)
class ElementParams:
    """One duty-cycled element: forced by its mode field, thermostat-driven
    in AUTO (the regulate loop on ``duty_field`` says whether it is heating
    right now)."""

    mode_field: str  # e.g. THM_HEATER1_STATE
    on_raw: int  # the mode field's ON raw value (element forced on)
    auto_raw: Optional[int]  # the AUTO raw value; None when the ICD has none
    duty_field: str  # the regulated field whose loop carries the duty


@dataclass(frozen=True)
class LoadParams:
    """One switched load: the LCL gate plus its composed draw parts."""

    state_field: str  # e.g. PWR_CDH_STATE — the LCL gate
    on_raw: int  # the gate field's raw value for the ON label
    base_a: float  # flat amps while the gate reads ON
    by_field: Optional[str]  # activity enum field, e.g. IMG_STATE
    by_amps: tuple[tuple[int, float], ...]  # (label raw value, amps) pairs
    wheels: bool  # add the ADCS model's live wheel currents
    per_element_a: float  # amps per lit element
    elements: tuple[ElementParams, ...]


@dataclass
class PowerModelConfig:
    """Validated [_models.<name>] table (kind = "power"), ready to run."""

    name: str
    wings: int
    wing_power_w: float
    vmp: float
    mppt_efficiency: float
    sada_axis: al.Vec3
    capacity_ah: float
    cells: int
    internal_resistance: float
    charge_current_max: float
    initial_soc: float
    loads: tuple[LoadParams, ...]
    outputs: dict[str, str]  # XTCE field -> model source key
    commands: dict[str, str]  # none in this bank; kept for the model contract

    def describe(self) -> list[str]:
        return [
            f"model {self.name}: EPS ({self.wings}x{self.wing_power_w:.0f} W wings, "
            f"{self.capacity_ah:.0f} Ah battery, {len(self.loads)} switched load(s)) "
            f"driving {len(self.outputs)} field(s)"
        ]


class PowerModel:
    """The runtime: illumination -> generation -> loads -> battery state.

    Reads the shared Environment for sun and eclipse, an attitude source
    (the ADCS model's plant truth) for wing pointing, and a field reader
    (the engine's overlay) for the LCL switch states and activity fields.
    Two more probes feed the activity-driven draws: ``element_on`` asks
    the engine whether the regulate loop on a field currently has its
    element lit, and ``wheel_current`` is the ADCS model's summed wheel
    amps. Owns no commands in this bank — SET_POWER drives the switches
    through ordinary behavior, and this model feels the consequences.
    """

    def __init__(
        self,
        config: PowerModelConfig,
        environment: Environment,
        read_raw: Callable[[str], object],
        attitude: Optional[Callable[[], al.Quat]] = None,
        element_on: Optional[Callable[[str], bool]] = None,
        wheel_current: Optional[Callable[[], float]] = None,
    ) -> None:
        self.config = config
        self.environment = environment
        self._read_raw = read_raw
        self._attitude = attitude
        self._element_on = element_on or (lambda _fname: False)
        self._wheel_current = wheel_current
        self.t = 0.0
        self.soc = config.initial_soc
        self._solar_current = 0.0  # array-side amps
        self._battery_current = 0.0  # signed: + charging, - discharging
        self._illuminated = False
        self._step(0.0)  # first beacon carries a live bus, not zeros

    # -- physics ---------------------------------------------------------------

    def advance(self, dt: float) -> None:
        self.t += dt
        self._step(dt)

    def _step(self, dt: float) -> None:
        cfg = self.config
        illum = self._illumination()
        self._illuminated = illum > 0.0
        solar_power = cfg.wings * cfg.wing_power_w * illum
        self._solar_current = solar_power / cfg.vmp if cfg.vmp else 0.0
        v_bus = self._open_circuit_voltage()
        bus_available = solar_power * cfg.mppt_efficiency / v_bus
        net = bus_available - self._load_current()
        if net >= 0.0:
            self._battery_current = min(net, self._charge_limit())  # excess is shunted
        else:
            self._battery_current = net  # battery covers the deficit
        if dt > 0.0:
            delta_ah = self._battery_current * dt / 3600.0
            self.soc = min(1.0, max(0.0, self.soc + delta_ah / cfg.capacity_ah))

    def _illumination(self) -> float:
        """Cosine of the best off-sun angle the SADAs can reach; 0 in shadow."""
        if not self.environment.sun_visible(self.t):
            return 0.0
        if self._attitude is None:
            return 1.0  # no attitude source: wings assumed sun-pointed
        s_body = al.quat_rotate(
            al.quat_conjugate(self._attitude()), self.environment.sun_direction
        )
        along_axis = al.v_dot(s_body, self.config.sada_axis)
        return max(0.0, 1.0 - along_axis * along_axis) ** 0.5

    def _load_current(self) -> float:
        return sum(
            self._one_load_current(load)
            for load in self.config.loads
            if self._read_raw(load.state_field) == load.on_raw
        )

    def _one_load_current(self, load: LoadParams) -> float:
        """The composed draw of one switched-on load, part by part."""
        total = load.base_a
        if load.by_field is not None:
            raw = self._read_raw(load.by_field)
            total += next((amps for value, amps in load.by_amps if raw == value), 0.0)
        if load.wheels and self._wheel_current is not None:
            total += self._wheel_current()
        for elem in load.elements:
            mode = self._read_raw(elem.mode_field)
            lit = mode == elem.on_raw or (
                elem.auto_raw is not None
                and mode == elem.auto_raw
                and self._element_on(elem.duty_field)
            )
            if lit:
                total += load.per_element_a
        return total

    def _charge_limit(self) -> float:
        """Controller limit, tapering linearly over the top of the charge."""
        headroom = (1.0 - self.soc) / _TAPER_BAND
        return self.config.charge_current_max * min(1.0, max(0.0, headroom))

    def _open_circuit_voltage(self) -> float:
        return self.config.cells * (
            _V_CELL_EMPTY + (_V_CELL_FULL - _V_CELL_EMPTY) * self.soc
        )

    # No command methods: config.commands is empty, so the engine's
    # command router can never reach this model in this bank.

    # -- outputs ----------------------------------------------------------------

    def outputs(self) -> dict[str, object]:
        """Engineering-unit values for every bound field."""
        values = {
            "solar_voltage": self.config.vmp if self._illuminated else 0.0,
            "solar_current": self._solar_current,
            "battery_voltage": self._open_circuit_voltage()
            + self._battery_current * self.config.internal_resistance,
            "battery_current": self._battery_current,
        }
        return {fname: values[source] for fname, source in self.config.outputs.items()}


def parse_power_model(
    name: str, body: dict, simdef, error: Callable[[str], None]
) -> PowerModelConfig | None:
    """Validate one [_models.<name>] table with kind = "power".

    Same contract as the ADCS parse: total (every problem reported via
    `error`), returns None if anything is wrong.
    """
    # Deferred import: model.py's dispatch imports this module, so the
    # helper import must not run at model.py's own import time.
    from xtce_sim.dynamics.model import _ErrorCounter, _positive, _unit_vec

    where = f"[_models.{name}]"
    problems = _ErrorCounter(error)
    err = problems.error
    for key in sorted(set(body) - {"kind", "array", "battery", "loads", "outputs"}):
        err(f"{where}: unknown key {key!r}")
    array = _sub_table(body, "array", where, err)
    battery = _sub_table(body, "battery", where, err)
    for sub, keys in (
        ("array", {"wing_power_w", "wings", "vmp", "mppt_efficiency", "sada_axis"}),
        ("battery", {"capacity_ah", "cells", "internal_resistance", "charge_current_max", "initial_soc"}),
    ):
        table = array if sub == "array" else battery
        for key in sorted(set(table) - keys):
            err(f"{where}.{sub}: unknown key {key!r}")

    wings = array.get("wings", 2)
    if isinstance(wings, bool) or not isinstance(wings, int) or wings < 1:
        err(f"{where}.array.wings: must be a positive integer")
        wings = 2
    cells = battery.get("cells", 6)
    if isinstance(cells, bool) or not isinstance(cells, int) or cells < 1:
        err(f"{where}.battery.cells: must be a positive integer")
        cells = 6
    wing_power = _positive(array.get("wing_power_w", 60.0), f"{where}.array.wing_power_w", err)
    vmp = _positive(array.get("vmp", 28.0), f"{where}.array.vmp", err)
    mppt_eff = _positive(array.get("mppt_efficiency", 0.95), f"{where}.array.mppt_efficiency", err)
    if mppt_eff is not None and mppt_eff > 1.0:
        err(f"{where}.array.mppt_efficiency: cannot exceed 1.0")
        mppt_eff = None
    sada_axis = _unit_vec(array.get("sada_axis", [0.0, 1.0, 0.0]), f"{where}.array.sada_axis", err)
    capacity = _positive(battery.get("capacity_ah", 10.0), f"{where}.battery.capacity_ah", err)
    resistance = _positive(
        battery.get("internal_resistance", 0.15), f"{where}.battery.internal_resistance", err
    )
    charge_max = _positive(
        battery.get("charge_current_max", 2.0), f"{where}.battery.charge_current_max", err
    )
    initial_soc = battery.get("initial_soc", 0.75)
    if (
        isinstance(initial_soc, bool)
        or not isinstance(initial_soc, (int, float))
        or not 0.0 <= initial_soc <= 1.0
    ):
        err(f"{where}.battery.initial_soc: must be a number between 0 and 1")
        initial_soc = 0.75

    loads = _parse_loads(body.get("loads", {}), simdef, where, err)
    outputs = _parse_power_outputs(body.get("outputs", {}), simdef, where, err)

    if problems.count:
        return None
    return PowerModelConfig(
        name=name,
        wings=wings,
        wing_power_w=wing_power,
        vmp=vmp,
        mppt_efficiency=mppt_eff,
        sada_axis=sada_axis,
        capacity_ah=capacity,
        cells=cells,
        internal_resistance=resistance,
        charge_current_max=charge_max,
        initial_soc=float(initial_soc),
        loads=loads,
        outputs=outputs,
        commands={},
    )


def _sub_table(body: dict, key: str, where: str, err) -> dict:
    table = body.get(key, {})
    if not isinstance(table, dict):
        err(f"{where}.{key}: must be a table")
        return {}
    return table


def _parse_loads(table, simdef, where: str, err) -> tuple[LoadParams, ...]:
    """Each load key K gates on PWR_K_STATE, which must exist with an ON
    label — the load list is checked against the ICD like everything else.

    A load's value is either flat amps (the whole draw) or a table
    composing base / by+amps / wheels / per_element+elements parts.
    """
    if not isinstance(table, dict):
        err(f"{where}.loads: must be a table")
        return ()
    fields = {f.name: f for p in simdef.packets for f in p.fields}
    loads = []
    for key, spec in table.items():
        state_field = f"PWR_{key}_STATE"
        field = fields.get(state_field)
        if field is None:
            err(f"{where}.loads: {key}: no field {state_field!r} in the definition")
            continue
        if not field.enumerations or "ON" not in field.enumerations:
            err(f"{where}.loads: {key}: {state_field} has no ON label to gate on")
            continue
        load = _parse_one_load(
            spec, fields, state_field, int(field.enumerations["ON"]), f"{where}.loads.{key}", err
        )
        if load is not None:
            loads.append(load)
    return tuple(loads)


def _parse_one_load(spec, fields, state_field, on_raw, where: str, err) -> Optional[LoadParams]:
    from xtce_sim.dynamics.model import _ErrorCounter, _positive

    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        amps = _positive(spec, where, err)
        if amps is None:
            return None
        return LoadParams(state_field, on_raw, amps, None, (), False, 0.0, ())
    if not isinstance(spec, dict):
        err(f"{where}: must be amps or a table of draw parts")
        return None
    problems = _ErrorCounter(err)
    perr = problems.error
    known = {"base", "by", "amps", "wheels", "per_element", "elements"}
    for key in sorted(set(spec) - known):
        perr(f"{where}: unknown key {key!r}")
    if not (spec.keys() & known):
        perr(f"{where}: declares no draw part")
    base = _positive(spec["base"], f"{where}.base", perr) if "base" in spec else 0.0
    by_field, by_amps = _parse_by_part(spec, fields, where, perr)
    wheels = spec.get("wheels", False)
    if not isinstance(wheels, bool):
        perr(f"{where}.wheels: must be true or false")
    per_element, elements = _parse_element_part(spec, fields, where, perr)
    if problems.count:
        return None
    return LoadParams(state_field, on_raw, base, by_field, by_amps, wheels, per_element, elements)


def _parse_by_part(spec, fields, where: str, err):
    """The activity-keyed part: ``by`` names an enum field, ``amps`` maps
    its labels to draws. Unlisted labels draw 0 at runtime."""
    from xtce_sim.dynamics.model import _positive

    if ("by" in spec) != ("amps" in spec):
        err(f"{where}: by and amps must appear together")
        return None, ()
    if "by" not in spec:
        return None, ()
    by = spec["by"]
    field = fields.get(by) if isinstance(by, str) else None
    if field is None:
        err(f"{where}.by: no field {by!r} in the definition")
        return None, ()
    if not field.enumerations:
        err(f"{where}.by: {by} is not an enumerated field")
        return None, ()
    amps_table = spec["amps"]
    if not isinstance(amps_table, dict) or not amps_table:
        err(f"{where}.amps: must be a non-empty table of label = amps")
        return None, ()
    pairs = []
    for label, amps in amps_table.items():
        if label not in field.enumerations:
            err(f"{where}.amps: {by} has no label {label!r}")
            continue
        value = _positive(amps, f"{where}.amps.{label}", err)
        if value is not None:
            pairs.append((int(field.enumerations[label]), value))
    return by, tuple(pairs)


def _parse_element_part(spec, fields, where: str, err):
    """The duty-cycled part: ``elements`` maps each element's mode field
    (needing an ON label; AUTO defers to the regulate loop on the mapped
    field) to the field it regulates."""
    from xtce_sim.dynamics.model import _positive

    if ("per_element" in spec) != ("elements" in spec):
        err(f"{where}: per_element and elements must appear together")
        return 0.0, ()
    if "elements" not in spec:
        return 0.0, ()
    per_element = _positive(spec["per_element"], f"{where}.per_element", err) or 0.0
    table = spec["elements"]
    if not isinstance(table, dict) or not table:
        err(f"{where}.elements: must be a non-empty table of mode field = regulated field")
        return per_element, ()
    elements = []
    for mode_name, duty_name in table.items():
        elem = _parse_one_element(mode_name, duty_name, fields, where, err)
        if elem is not None:
            elements.append(elem)
    return per_element, tuple(elements)


def _parse_one_element(mode_name, duty_name, fields, where: str, err) -> Optional[ElementParams]:
    mode = fields.get(mode_name)
    if mode is None:
        err(f"{where}.elements: no field {mode_name!r} in the definition")
        return None
    if not mode.enumerations or "ON" not in mode.enumerations:
        err(f"{where}.elements: {mode_name} has no ON label")
        return None
    duty = fields.get(duty_name) if isinstance(duty_name, str) else None
    if duty is None:
        err(f"{where}.elements: {mode_name}: no field {duty_name!r} in the definition")
        return None
    if duty.python_type in ("string", "bytes"):
        err(f"{where}.elements: {mode_name}: {duty_name} is not a numeric field")
        return None
    auto = mode.enumerations.get("AUTO")
    return ElementParams(
        mode_name,
        int(mode.enumerations["ON"]),
        int(auto) if auto is not None else None,
        duty_name,
    )


def _parse_power_outputs(table, simdef, where: str, err) -> dict[str, str]:
    if not isinstance(table, dict) or not table:
        err(f"{where}.outputs: at least one field binding is required")
        return {}
    fields = {f.name: f for p in simdef.packets for f in p.fields}
    outputs = {}
    for fname, source in table.items():
        field = fields.get(fname)
        if field is None:
            err(f"{where}.outputs: unknown field {fname!r}")
            continue
        if source not in _SOURCE_KEYS:
            err(f"{where}.outputs: {fname}: unknown source {source!r}")
            continue
        if field.python_type in ("string", "bytes"):
            err(f"{where}.outputs: {fname}: numeric source into a text field")
            continue
        outputs[fname] = source
    return outputs
