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

The realistic, runnable examples live in `examples/` (`my_vehicle.xml`,
`imaging_sat.xml`) — those are for demos and the `run`/`monitor`/`exercise`
commands. This fixture's only job is coverage: keeping it dense and diverse
means adding a parser feature here is enough to test it, without touching the
demo files. If you add a parser capability, extend this file to cover it.
