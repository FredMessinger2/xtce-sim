# Example satellites

A satellite is a **directory**: its XTCE interface file(s) and its
per-subsystem behavior `.toml` files live together, and `run` writes its
artifacts to `<satellite dir>/runs/<id>/`. The definitions are the author's
own; no vendor or proprietary data.

## `my_vehicle/` — the primary example

A small synthetic vehicle, `MyVehicle` — 55 commands, 14 telemetry packets,
with polynomial and spline calibrators on its sensor fields (raw counts on
the wire, engineering units on the monitor). No behavior files yet.

- **`my_vehicle.xml`** — one XTCE file with both commands and telemetry:

  ```bash
  xtce-sim run examples/my_vehicle/my_vehicle.xml --id sat-a --port 5000 --live
  ```

- **`my_vehicle_commands.xml`** + **`my_vehicle_telemetry.xml`** — the *same*
  satellite split into separate command and telemetry files, demonstrating
  multi-file loading (some vendors ship command and telemetry separately):

  ```bash
  xtce-sim run examples/my_vehicle/my_vehicle_commands.xml \
    examples/my_vehicle/my_vehicle_telemetry.xml --id sat-a --port 5000
  ```

Both forms build an identical simulator — a test
(`test_combined_example_matches_split_pair`) guards that they stay in sync.

## `imaging_sat/` — the full-featured example

An Earth-observation satellite, `ImagingSat` — 30 commands, 8 telemetry
packets, and per-subsystem behavior files that make it act:

- **`imaging_sat.xml`** — the interface: imaging, thermal, power,
  file-transfer, and ATS/RTS sequencing surfaces.
- **`thermal.toml`** — heater commands and ramps, the orbit thermal cycle
  on the structural panels.
- **`imager.toml`** — imager power/capture effects, focal-plane heating,
  event-log entries with immediate emission.
- **`power.toml`** — solar/battery ambient signals.
- **`system.toml`** — mode-change acknowledgments.

```bash
xtce-sim run examples/imaging_sat/imaging_sat.xml --port 5000 --interval 1
```

Every `.toml` beside the XTCE is discovered and merged automatically; see
the repo README's Behavior section for the schema.
