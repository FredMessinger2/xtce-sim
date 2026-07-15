# tests/data — test fixtures

XTCE files used **only by the test suite**. These are not examples and are not
shipped with the package. Do not delete them — they back real test coverage.

## `full_features.xml`

A deliberate "kitchen sink" XTCE that exercises the full breadth of the parser
in a single, small file. It is **not** a realistic satellite; it packs in one
of nearly every XTCE construct so a single parse touches every code path:

- every parameter/argument type kind (integer, float, enumerated, boolean,
  string, binary, absolute-time, aggregate, array)
- encodings, calibrators, valid ranges, units
- static alarms and context alarms
- container inheritance (`BaseContainer` + `RestrictionCriteria`, incl. a
  multi-comparison `ComparisonList`)
- command inheritance (`BaseMetaCommand` + `ArgumentAssignmentList`) and
  per-argument / command-level `AncillaryData`
- path-qualified references and nested `SpaceSystem`s (merged into one def)

### Used by

`tests/test_parser_full.py` — loads it once as the module `defn` fixture for
broad parser assertions, and parses it twice (`[FULL, FULL]`) to test
SpaceSystem merging. Several suites lean on it: boolean field sizing, container
restriction criteria, and command-inheritance / ancillary-data tests.

### Why it stays unrealistic

The realistic, runnable example lives in `examples/imaging_sat/` — that one is
for demos and the `run`/`monitor`/`exercise` commands. This fixture's only job
is coverage: keeping it dense and diverse means adding a parser feature here is
enough to test it, without touching the demo files. If you add a parser
capability, extend this file to cover it.

## `my_vehicle/`

A complete second satellite, `MyVehicle` — 61 commands, 18 telemetry packets.
It lived in `examples/` until 2026-07-15 and was moved here because two
example vehicles competed for attention and drifted (a stale `launch.json`
pointed at paths that no longer existed). It is a **fixture**, not an example:
nothing in the README, the VSCode configs, or the docs points at it, and it is
not shipped.

It stays because it is the only thing proving this simulator is XTCE-*driven*
rather than built around `imaging_sat`. Three properties do that work, and no
single-vehicle repo can:

- **A different ADCS shape** — three orthogonal wheels against imaging_sat's
  four-wheel pyramid, wired through `adcs.toml`.
- **A subset ICD** — six of the eleven ADCS command roles, and a mode
  enumeration with no TARGET_TRACK. Proves a leaner ICD is a valid
  configuration rather than a validation error.
- **A different enum encoding** — its `ADCS_MODE` STANDBY is raw 4 where
  imaging_sat's is 5. A wire-value shortcut anywhere in the stack would be
  wrong on exactly one of the two vehicles, and this is what catches it.

It also carries two file-level jobs:

- **`my_vehicle_commands.xml` + `my_vehicle_telemetry.xml`** are the same
  satellite split in two, covering multi-file merge (`run a.xml b.xml`).
  `my_vehicle.xml` is the combined equivalent, and
  `test_combined_example_matches_split_pair` guards that they stay identical.
- It declares `FILE_LIST`/`FILE_DELETE` but **no `FILE_RECEIPT` packet**,
  which is what pins the file service's log-only-receipt path and the CLI's
  honest "not confirmed" message for a vehicle that cannot report transfers.

### Used by

Twelve test modules, including `test_behavior.py` (the three-wheel model
fixtures `mv_simdef`/`mv_engine`), `test_generate.py` (merge equivalence),
`test_fileservice.py` and `test_cli.py` (the no-receipt contract).
