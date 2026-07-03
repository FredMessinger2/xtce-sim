# Example satellites

A small synthetic vehicle, `MyVehicle` — 55 commands, 14 telemetry packets. The
definitions are the author's own; no vendor or proprietary data.

- **`my_vehicle.xml`** — the primary example: **one** XTCE file with both commands
  and telemetry. This is the headline form:

  ```bash
  xtce-sim run examples/my_vehicle.xml --id sat-a --port 5000 --live
  ```

- **`my_vehicle_commands.xml`** + **`my_vehicle_telemetry.xml`** — the *same*
  satellite split into separate command and telemetry files, demonstrating
  multi-file loading (some vendors ship command and telemetry as separate XTCE).
  Pass both and they merge:

  ```bash
  xtce-sim run examples/my_vehicle_commands.xml examples/my_vehicle_telemetry.xml \
    --id sat-a --port 5000
  ```

Both forms build an identical simulator — a test
(`test_combined_example_matches_split_pair`) guards that they stay in sync.
