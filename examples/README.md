# Example satellite

A satellite is a **directory**: its XTCE interface file(s) and its
per-subsystem behavior `.toml` files live together, and `run` writes its
artifacts to `<satellite dir>/runs/<id>/`. The definitions are the author's
own; no vendor or proprietary data.

There is deliberately **one** example. A second vehicle exists — a three-wheel
variant with a subset ICD, which is how the suite proves the engine is driven
by the XTCE rather than built around one satellite — but it lives in
[`tests/data/my_vehicle/`](../tests/data/my_vehicle) as a test fixture, not
here. Two competing examples only drift apart.

## `imaging_sat/` — the example

An Earth-observation satellite, `ImagingSat` — 40 commands, 12 telemetry
packets, and per-subsystem behavior files that make it act:

- **`imaging_sat.xml`** — the interface: imaging, thermal, power, ADCS,
  file-transfer, and ATS/RTS sequencing surfaces.
- **`thermal.toml`** — heater commands and ramps, the orbit thermal cycle
  on the structural panels.
- **`imager.toml`** — imager power/capture effects, focal-plane heating,
  event-log entries with immediate emission.
- **`power.toml`** — solar/battery ambient signals.
- **`system.toml`** — mode-change acknowledgments.
- **`adcs.toml`** — attitude-control boot state (identity quaternion,
  STANDBY mode, sensor validity), instant mode acknowledgment, and
  setpoint reflections: slew, wheel-speed, and magnetorquer commands
  mirror their commanded values into telemetry immediately. These are
  placeholders, not dynamics — a slew *jumps* the quaternion — until the
  dynamics arc replaces them.
- **`set_all_fields.sh`** — a scripted sweep that sets every
  command-settable field to a distinctive value, one send per second
  (`PORT=`/`PAUSE=` to override), for watching on the monitor or web
  console.

```bash
xtce-sim run examples/imaging_sat/imaging_sat.xml --port 5000 --interval 1
```

Every `.toml` beside the XTCE is discovered and merged automatically; see
the repo README's Behavior section for the schema.
