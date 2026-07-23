# xtce-sim

**Run a CCSDS satellite simulator straight from an XTCE file.**

```bash
xtce-sim run examples/imaging_sat/imaging_sat.xml --id sat-a --port 5000
```

`xtce-sim` parses an [XTCE](https://www.omg.org/spec/XTCE/) command/telemetry
definition, builds the commands and telemetry **in memory**, and starts a CCSDS
packet simulator on a TCP port. Point anything at it — OpenC3, Yamcs, a custom
client, or the bundled `xtce-sim monitor`. An optional
[behavior sidecar](#behavior-making-commands-change-telemetry) makes the
simulated vehicle *act*: commands change telemetry, heaters warm toward their
setpoints, panels ride the orbit thermal cycle.

No OpenC3 required, no heavyweight ground system — just a small Python
package with a light dependency footprint (`click`, `crcmod`, and `aiohttp`
for the bundled single-page [web console](#web-console)).

## How it fits together

XTCE is the contract. `xtce-sim run` parses it into an in-memory definition,
writes a machine-readable copy to `<satellite dir>/runs/<id>/cmd_tlm.json`, and serves CCSDS on a
single bidirectional TCP port — telemetry frames stream out, command frames come
in, each a length-prefixed CCSDS packet with a CRC.

```mermaid
flowchart TD
    XTCE["satellite directory — imaging_sat/<br/>XTCE file(s): opcodes, APIDs, fields<br/>behavior .toml files beside them"]
    BEH["behavior TOML (optional)<br/>thermal.toml · imager.toml · …<br/>command effects · ramps · signals"]
    SIM["SimServer<br/>SimDefinition (in memory)<br/>40 commands · 12 telemetry packets"]
    JSON["runs/&lt;id&gt;/cmd_tlm.json<br/>shared command / telemetry dictionary"]
    PORT{{"CCSDS over TCP — one bidirectional port :5000<br/>2-byte length + CCSDS packet + CRC-16"}}
    MON["xtce-sim monitor<br/>decode telemetry"]
    SEND["xtce-sim send<br/>encode command"]
    GS["OpenC3 / Yamcs / custom<br/>ground system"]

    XTCE -->|"xtce-sim run: parse"| SIM
    BEH -->|"validated against the XTCE"| SIM
    SIM -->|dumps| JSON
    SIM -->|serves| PORT
    PORT <-->|"telemetry out / commands in"| MON
    PORT <-->|"telemetry out / commands in"| SEND
    PORT <-->|CCSDS| GS
    JSON -.->|"loaded via --id"| MON
    JSON -.->|"loaded via --id"| SEND
    XTCE -.->|"same definition"| GS

    %% Live/build links (solid) are thick and dark; out-of-band definition
    %% sharing (dashed) is thin and gray — dash length alone is hard to see.
    linkStyle 0,1,2,3,4,5,6 stroke-width:3px
    linkStyle 7,8,9 stroke:#999999,stroke-width:1.5px

    %% Explicit fills + text colors so the diagram stays readable in both
    %% GitHub themes (the default theme picks unreadable colors in dark mode).
    classDef source fill:#8250df,stroke:#6639ba,color:#ffffff
    classDef sim fill:#1f6feb,stroke:#1158c7,color:#ffffff
    classDef artifact fill:#57606a,stroke:#424a53,color:#ffffff
    classDef wire fill:#9a6700,stroke:#7d4e00,color:#ffffff
    classDef client fill:#2da44e,stroke:#1a7f37,color:#ffffff
    class XTCE source
    class BEH artifact
    class SIM sim
    class JSON artifact
    class PORT wire
    class MON,SEND,GS client
```

*(Thick solid arrows: build + the live CCSDS link. Thin gray dashed arrows: the
command/telemetry definition, shared out-of-band — no in-band discovery.)*

The wire carries only binary CCSDS — there is **no in-band discovery**. A client
learns the command/telemetry set *out of band*: the bundled `monitor` and `send`
load the `cmd_tlm.json` the server dumped (via `--id`), and a third-party ground
system (OpenC3, Yamcs, your own) is configured with the same XTCE. Either way,
both ends derive identical opcodes, APIDs, and field layouts from one definition.
(One packet rides outside the XTCE: the command echo on reserved APID 0x7FD —
link protocol, not payload telemetry; a third-party ground system can simply
ignore that APID. See the web console section.)

## Commands

```bash
xtce-sim inspect  <file.xml...>                    # narrate what the parser sees and infers
xtce-sim generate <file.xml...>                    # build defs, write cmd/tlm to disk, stop
xtce-sim run      <file.xml...> --id ID --port N   # build, dump, and serve
xtce-sim monitor  --id ID --port N                 # watch decoded live telemetry
xtce-sim ui       --id ID --port N                 # live browser console (WebSocket push)
xtce-sim send     --id ID --port N CMD K=V ...     # send a command
xtce-sim upload   <file> --id ID --port N          # upload a file to the vehicle's store
xtce-sim exercise --id ID --port N                 # send every command, check telemetry health
xtce-sim seq check <file.ats|.rts> --def <xml>     # validate a command sequence file
xtce-sim seq shift <file.ats> --start-in 30s       # re-base an ATS to start soon
```

### Example

```bash
# Terminal 1 — serve the bundled example satellite
xtce-sim run examples/imaging_sat/imaging_sat.xml --id sat-a --port 5000 --live

# Terminal 2 — watch telemetry stream in, decoded by field name
xtce-sim monitor --id sat-a --port 5000

# Terminal 3 — send a command (enum arguments accept their labels)
xtce-sim send --id sat-a --port 5000 SET_POWER SubsystemId=COMMS PowerState=ON
```

The bundled example is
[`examples/imaging_sat/`](examples/imaging_sat) — an Earth-observation
satellite with imaging, thermal, a full ADCS (attitude determination and
control: quaternion attitude, four-wheel pyramid, star tracker/sun/mag
sensors — raw counts with calibrators throughout), file transfer, and
ATS/RTS sequencing. The per-subsystem behavior files beside its XTCE are
discovered automatically, so its commands actually change its telemetry and
its ADCS flies real physics from the first beacon — see
[Behavior](#behavior-making-commands-change-telemetry) and
[Physics models](#physics-models-the-adcs-flies) below.

Command and telemetry can live in **one** XTCE file (as above) or in
**several** — pass them all and they are merged, since some vendors ship
command and telemetry databases separately:

```bash
xtce-sim run sat_commands.xml sat_telemetry.xml --id sat-a --port 5000
```

### Inspecting a definition

Before serving a new XTCE, ask the parser to narrate what it sees — and, more
importantly, what it *infers*:

```bash
xtce-sim inspect examples/imaging_sat/imaging_sat.xml
```

```text
parsing examples/imaging_sat/imaging_sat.xml (SpaceSystem 'ImagingSat')
resolved inheritance: 42 command(s) with a base command (42 fixing inherited args via assignments), ...
significance: 11 command(s) declare non-normal criticality (2 vital, 9 critical)
~ aggregate 'ADCS_ATT_QUAT' flattened to 4 field(s) (ADCS_ATT_QUAT_Q1...)
...
built ImagingSat: 41 dispatchable command(s), 13 telemetry packet(s)

Behavior (examples/imaging_sat):
  environment: orbit 500 km @ 51.6 deg, sun [1.0, 0.0, 0.0] (shared by all models)
  initial values: 11 field(s)
    ...
  boot signals: 6
    THM_PANEL_PLUS_X oscillates (sine) around 10.0 amplitude 25.0, period 5400.0s ±noise(0.5)
    ...
  model adcs: rigid-body ADCS (4 wheels) driving 41 field(s), 11 command(s)
  model power: EPS (2x60 W wings, 10 Ah battery, 5 switched load(s)) driving 4 field(s)
  HEATER_AUTO:
    THM_HEATER{HeaterId}_STATE = 'AUTO'  [emit: immediate]
    THM_HEATER{HeaterId}_TEMP regulates around @THM_HEATER{HeaterId}_SETPOINT band 2.0 (heats to 60.0 tau=30.0s, cools to 20.0 tau=45.0s)
  ...
OK: ImagingSat — 41 command(s), 13 packet(s)
```

Lines marked `~` are **inferences and gaps** — places the parser filled a gap
rather than reading an explicit declaration (an enum sized from its max value,
a boolean defaulted to 1 bit, a command assigned a synthetic opcode), and
**content the parser ignored**: after the parse it reports any element the
file declared but nothing ever read (`ignored N <VerifierSet> ... — present
in the XTCE but not read by this parser`), so unsupported XTCE features are
visible instead of silently dropped. (The shipped examples currently contain
no unread elements — a test pins that.) Warnings appear inline with a `!` marker. `inspect --full`
traces every parsed element, and `inspect --dump` appends the complete
resolved inventory — every command and telemetry packet, the same report
`generate` writes to `<satellite dir>/runs/<id>/cmd_tlm.txt`. The same trace is available
live during a build or serve with `generate -v` / `run -v` (`-vv` for the
full firehose). `inspect` writes nothing to disk.

### Command significance

XTCE lets a definition declare how dangerous each command is —
`<DefaultSignificance consequenceLevel="critical" reasonForWarning="..."/>`,
with the levels (`normal`, `vital`, `critical`, `forbidden`, `user1`)
following ISO 14950 telecommand criticality. The imaging satellite declares
eleven hazardous commands (wheel shutdowns, desaturation, sequence starts,
file deletion), and the significance follows the command everywhere an
operator meets it:

```
$ xtce-sim send --def examples/imaging_sat/imaging_sat.xml --port 5000 ADCS_DESATURATE
ADCS_DESATURATE is CRITICAL: Attitude transients while momentum unloads; imaging unavailable
sent ADCS_DESATURATE (0x48) args={}
```

`exercise --dry-run` badges hazardous commands, the dumped `cmd_tlm.txt`
marks them (`[CRITICAL]` with the declared reason), derived commands inherit
their base command's significance up the XTCE inheritance chain, and the web
console's command log wears a red or amber badge on each hazardous entry
(hover for the reason). This is display only, deliberately: a real arm/fire
confirmation gate is future work, and would be designed for scripts, not
sprung on them.

### Argument range enforcement

Declared `ValidRange`s (and enum membership) are enforced on **both ends of
the link**, the way real systems do it. The ground refuses to build an
invalid command — nothing is transmitted:

```
$ xtce-sim send --def examples/imaging_sat/imaging_sat.xml --port 5000 ADCS_WHEEL_SET_SPEED WheelId=7 Speed=0
ADCS_WHEEL_SET_SPEED is VITAL: Bypasses the controller; test mode only
Error: ADCS_WHEEL_SET_SPEED: WheelId=7 is outside ValidRange [1.0, 4.0]
```

And the vehicle validates for itself — it does not trust the ground. A
command that arrives out of range anyway (a foreign client, a corrupted
uplink, a truncated payload whose zero-padding falls outside a declared
range) is **rejected**: no effects apply, the sim logs why, and the command
echo carries the `rejected` status, so the web console's command log shows
a red `✗ rejected` entry the moment it happens. To exercise that path
deliberately, `send --force` skips the ground-side check and transmits
anyway — the honest way to test flight software's own guards:

```
$ xtce-sim send --def examples/imaging_sat/imaging_sat.xml --port 5000 --force ADCS_WHEEL_SET_SPEED WheelId=7 Speed=0
ADCS_WHEEL_SET_SPEED is VITAL: Bypasses the controller; test mode only
--force: skipping ground-side range checks
sent ADCS_WHEEL_SET_SPEED (0x47) args={'WheelId': '7', 'Speed': '0'}
```

Bounds are inclusive, and float32 arguments are compared as the wire sees
them (value and bounds quantized identically on both ends, so a legal
boundary value can never be accepted by the ground and rejected by the
vehicle). Two scope notes: XTCE's *exclusive* min/max attributes parse but
are not yet enforced; and command arguments carry no calibrators in this
pipeline, so declared ranges apply directly to the value you type (which
*is* the wire value) — XTCE's raw-vs-calibrated range distinction becomes
relevant only if argument calibration lands later.

### Exercising the command surface

Smoke-test every command a definition declares — one send per enum label and
per numeric min/max boundary — then confirm telemetry is still flowing and
decodable:

```bash
xtce-sim exercise --id sat-a --port 5000
```

```text
leaving 8 sequence command(s) out of the sweep (they act on the sequencer — exercise them via upload + LOAD/START, or name them with --command)
Exercising 32 command(s) on 127.0.0.1:5000 ...
Commands: sent 74/74 OK
Telemetry: 154 packet(s), 12 APID(s), 0 decode failure(s)
  sample: THERMAL_STATUS: THM_TIMESTAMP=0, THM_PANEL_PLUS_X=2568, THM_PANEL_MINUS_X=-469
```

`--command NAME` limits the sweep (repeatable), `--dry-run` prints what would
be sent without connecting, and the exit code is non-zero on any failure — 
usable in CI.

The eight ATS/RTS commands sit the sweep out, as the first line says: the
sweep is background traffic, and it must not abort a plan you are running
or land a sequencer slot in ERROR with a bare LOAD. Naming one with
`--command` sends it like anything else; the real way to exercise them is
the flow in [Onboard sequences](#onboard-sequences-ats-and-rts).

At full speed the sweep finishes in well under a second — fine for CI,
useless to watch. For a human at a monitor or the web console, slow it down
and let it run:

```bash
xtce-sim exercise --id sat-a --port 5000 --pause 1 --loop
```

`--pause 1` waits a second after each send and narrates every send as it
happens (`ADCS_SET_MODE  Mode=NADIR  ok`), so you can match commands to the
telemetry reacting; `--loop` repeats the whole sweep until Ctrl-C, reporting
the sweep count on exit.

`--reject-probes N` mixes N deliberately out-of-range sends into the sweep —
a valid command with exactly one argument pushed past its declared range or
enum, transmitted with the ground check bypassed, so the vehicle's own
rejection path gets exercised alongside the happy path. Placement is
seeded-deterministic (each `--loop` pass sprinkles differently, but the same
run reproduces exactly), `--dry-run` marks them `[REJECT-PROBE]`, and in the
web console each one lands as a red `✗ rejected` line in the command log.

The imaging satellite also ships a scripted sweep,
[`examples/imaging_sat/set_all_fields.sh`](examples/imaging_sat/set_all_fields.sh),
which sets every command-settable telemetry field to a distinctive value one
send at a time — heater setpoints, exposure, quaternion, wheel speeds — a
guided tour of the behavior wiring rather than a boundary probe (a test
cross-validates every line of it against the XTCE).

### Monitor styles

`monitor` has three display styles (`--style`). Output is colored in a real
terminal; the values below are illustrative (serve with `--live` or a behavior
sidecar for moving data — with neither, the beacon is zeros).

**`compact`** (default) — one line per packet; scrolls, greps, pipes. Shows the
first few fields; add `--fields` for all.

```
07:25:25.338  0x10 HOUSEKEEPING     seq 0      TIMESTAMP=1735689604 s  SYSTEM_MODE=STANDBY  CMD_RECV_COUNT=8  CMD_REJECT_COUNT=8  +10 more
07:25:25.339  0x11 IMAGER_STATUS    seq 0      TIMESTAMP=1735689604 s  STATE=IDLE  EXPOSURE_MS=75  GAIN=75  +6 more
07:25:25.339  0x12 POWER_STATUS     seq 0      TIMESTAMP=1735689604 s  SOLAR_VOLTAGE=16.014 V  SOLAR_CURRENT=0.968 A  BATTERY_VOLTAGE=23.864 V  +7 more
```

**`table`** — a boxed, per-packet table of every field with value and unit. On
a terminal it repaints in place without flicker; piped, tables append so the
output stays greppable. Best paired with `--packet NAME` to focus one packet
(unfiltered, each arriving packet repaints over the last).

```
┌ HOUSEKEEPING · APID 0x10 · seq 1 · 07:25:26.340
│ HK_TIMESTAMP            1735689605  s
│ HK_SYSTEM_MODE          STANDBY
│ HK_CMD_RECV_COUNT       10
│ HK_CMD_REJECT_COUNT     10
│ HK_LAST_CMD_OPCODE      80
│ HK_UPTIME               10
│ HK_ERROR_COUNT          10
│ HK_BUS_VOLTAGE          7.536  V
│ HK_BUS_CURRENT          0.99  A
│ HK_BATTERY_SOC          80
│ HK_BOARD_TEMP           22.99  degC
│ HK_ISSUED_TIMESTAMP     1.73569e+09  s
│ HK_RECEIVED_TIMESTAMP   1.73569e+09  s
│ HK_GENERATED_TIMESTAMP  1.73569e+09  s
└─────────────────────────────────────────────────
```

*(every field in the packet — all 14 of HOUSEKEEPING's, here in full)*

**`dashboard`** — a full-screen view, one row per APID, refreshing in place.

```
xtce-sim monitor · sat-a · 127.0.0.1:5010     packets 11
──────────────────────────────────────────────────────────────────
0x10 HOUSEKEEPING   seq 4      TIMESTAMP=1735689647 s  SYSTEM_MODE=DOWNLINK  CMD_RECV_COUNT=94  CMD_REJECT_COUNT=94  LAST_CMD_OPCODE=90  +9
0x11 IMAGER_STATUS  seq 4      TIMESTAMP=1735689647 s  STATE=ERROR  EXPOSURE_MS=90  GAIN=90  BAND1_AVG=89.9983  +5
0x12 POWER_STATUS   seq 4      TIMESTAMP=1735689647 s  SOLAR_VOLTAGE=16.642 V  SOLAR_CURRENT=0.657 A  BATTERY_VOLTAGE=24.178 V  BATTERY_CURRENT=0.657 A  +6
0x13 THERMAL_STATUS seq 4      TIMESTAMP=1735689647 s  PANEL_PLUS_X=10.94 degC  PANEL_MINUS_X=8.9 degC  PANEL_PLUS_Y=35.53 degC  PANEL_MINUS_Y=-13.96 degC  +7
0x14 EVENT_LOG      seq 4      TIMESTAMP=1735689647 s  SEVERITY=0  SUBSYSTEM=90  EVENT_ID=90  MESSAGE=
0x16 ATS_STATUS     seq 4      TIMESTAMP=1784172395 s  SEQ_ID=0  SEQ_NAME=  STATE=IDLE  CMD_TOTAL=0  +6
0x17 RTS_STATUS     seq 4      TIMESTAMP=1784172395 s  SEQ_ID=0  SEQ_NAME=  STATE=IDLE  CMD_TOTAL=0  +6
0x18 ADCS_STATUS    seq 4      TIMESTAMP=1735689647 s  MODE=STANDBY  EST_STATE=VALID  POINTING_ERR=0 deg  MOMENTUM_TOTAL=0 Nms  +3
0x19 ADCS_ATTITUDE  seq 4      ATT_TIMESTAMP=1735689647 s  ATT_QUAT_Q1=-9.15555e-05  ATT_QUAT_Q2=-6.1037e-05  ATT_QUAT_Q3=-0.000122074  ATT_QUAT_Q4=1  +6
0x1A ADCS_WHEELS    seq 4      WHL_TIMESTAMP=1735689647 s  WHEEL1_SPEED=0 RPM  WHEEL2_SPEED=0 RPM  WHEEL3_SPEED=0 RPM  WHEEL4_SPEED=0 RPM  +8
0x1B ADCS_SENSORS   seq 4      SNS_TIMESTAMP=1735689647 s  ST_QUAT_Q1=-9.15555e-05  ST_QUAT_Q2=-6.1037e-05  ST_QUAT_Q3=-0.000122074  ST_QUAT_Q4=1  +8
```

*(the instance label is the `--id`. `0x15 FILE_RECEIPT` is absent by design:
file receipts are event telemetry — they downlink when a transfer or a
FILE_\* command happens, and never on the beacon. The ATS/RTS rows are the
sequencer's real state — an idle vehicle reports IDLE with a live
timestamp, whatever `--live` synthesizes for the other packets.)*

Filter to specific packets with `--packet NAME` (repeatable).

### Web console

`xtce-sim ui` serves a live browser console — every packet, every field, in
one window, updated the instant data arrives:

```bash
xtce-sim ui --def examples/imaging_sat/imaging_sat.xml --port 5000
# Console: http://127.0.0.1:8080/  (sim 127.0.0.1:5000, Ctrl-C to stop)
```

The layering is deliberate. The sim server plays the *spacecraft*: it speaks
only framed CCSDS over TCP and never learns about JSON or browsers. `ui` is a
separate small process playing the *ground station* — it connects to the
sim's TCP port exactly like `monitor` does, decodes each packet against the
definition, and pushes JSON to the browser over WebSocket. On connect the
browser receives the full definition (packets, fields, units, enum labels)
and builds its panels from it — the page hardcodes nothing.

In the console: one panel per packet with every field, values in engineering
units with an **EU/RAW** toggle (same rules as `monitor --raw`), a changed
value breathes briefly, panels dim when their packets stop arriving, and a
link dot tracks the sim connection — kill the sim and it goes red, restart
and the bridge reconnects on its own. Because telemetry is *pushed*,
immediate emissions (see below) appear the moment they happen, between
beacon beats. `--http-port` moves the console off 8080.

Event-driven packets are the exception to the dimming rule: a packet that
downlinks when something happens rather than on the beacon (`FILE_RECEIPT`,
today) wears an **EVENT** badge and never dims — quiet is its normal state,
and the latched last event stays readable instead of fading five seconds
after every upload.

**The command log.** The console is split in two — a command history and
the telemetry grid — separated by a draggable splitter bar (the header's
*split* button flips it between horizontal and vertical; your arrangement
and position are remembered). Every command the sim processes while the
console is open appears in the log as it executes: timestamp, name, every
argument (enums as their labels), and a status mark — green ✓ for executed,
red ✗ tagged with the failure status (`rejected`, `unknown_opcode`,
`failed`) for one that wasn't. Hazardous commands (see [Command
significance](#command-significance)) wear a red (`critical`/`forbidden`) or
amber (`vital`) badge; hovering it shows the declared reason. The log holds
the last 500 entries and sticks to the bottom unless you've scrolled up to
read history.

The command log works the way real ground systems learn about commanding:
the vehicle reports it. On every command it processes — from any client,
`send`, the exerciser, anyone — the sim broadcasts a **command echo**: a
telemetry packet on a reserved APID (0x7FD, documented in `ccsds.py`)
carrying the original command bytes verbatim plus an execution status. The
bridge decodes the embedded command against the definition to recover the
name and arguments. Real systems verify commanding with the same family of
mechanisms (command counters, ECSS PUS Service 1 acknowledgment, literal
command echo); this is the echo flavor. The echo APID is part of this
simulator's link protocol — like the length-prefix framing — not part of
any satellite's XTCE, and the terminal `monitor` skips it (the web console
is its renderer).

**Commanding from the browser.** The command pane carries an entry line —
under the log when the pane is a tall column, left of it when the pane is a
wide strip, following the *split* toggle. Type shell-style —
`HEATER_ON HeaterId=1` — and Enter sends. The command name autocompletes
from the definition (arrow keys and Tab, case-insensitive, resolving to the
ICD's exact spelling), and once a name is complete the hint line shows its
full argument signature straight from the XTCE: enum labels, declared
ranges, units. ArrowUp recalls earlier sends.

A command the ICD marks hazardous does not go on one Enter: the entry arms
a confirm step showing the significance level and its declared reason —
Enter again transmits, Esc cancels, and changing the line in any way
disarms it.

The bridge is the ground authority, and it does not trust the page: every
send is validated and encoded exactly as `xtce-sim send` does, so an
unknown name, a misspelled argument, or a value outside its declared range
is **refused on the ground** — the reason appears under the entry line and
in the log as `refused by ground`, and nothing touches the wire. A command
that passes goes up the same TCP link telemetry rides down, and its outcome
arrives the way every command's does: as the vehicle's echo, on every
console watching. The bridge also refuses WebSocket connections from
foreign web origins and requests addressed to unexpected hosts, so a random
page open in your browser cannot command the vehicle through your loopback
bridge.

### Live telemetry

Telemetry values come from up to three layers. With no options the sim beacons
zeros for every field no behavior file claims (the bundled satellite carries
behavior files that fly its ADCS fields from boot, options or not). Add
`--live` to `run` and it beacons changing synthetic values instead —
counters climb, temperatures and voltages drift, wheel speeds wobble — so
`monitor` shows moving data:

```bash
xtce-sim run examples/imaging_sat/imaging_sat.xml --id sat-a --port 5000 --live
```

```
07:28:40.080  0x10 HOUSEKEEPING     seq 0      TIMESTAMP=1735689604 s  SYSTEM_MODE=STANDBY  CMD_RECV_COUNT=8  CMD_REJECT_COUNT=8  +10 more
07:28:41.081  0x10 HOUSEKEEPING     seq 1      TIMESTAMP=1735689605 s  SYSTEM_MODE=STANDBY  CMD_RECV_COUNT=10  CMD_REJECT_COUNT=10  +10 more
```

`--live` heuristics choose plausible engineering values ("about 8 volts") — a
light stand-in, not physics. A field whose XTCE declares a calibrator
transmits the raw count that decodes back to that value, so the wire stays
honest. The third layer is the
[behavior sidecar](#behavior-making-commands-change-telemetry): any field it
governs overrides both other layers, so seeded values, command effects, and
ambient signals always win over zeros and `--live` synthetics.

When the XTCE declares calibrators (polynomial or spline), the wire always
carries raw counts and `monitor` converts them, showing engineering units by
default; pass `--raw` to see the counts as transmitted.

Every `run` and `generate` writes the resolved command/telemetry to the
satellite's own directory, `<satellite dir>/runs/<id>/`
(`cmd_tlm.txt` for humans, `cmd_tlm.json` for machines; add `--emit-py` for an
importable Python snapshot). The `monitor` and `send` clients load that
`cmd_tlm.json` via `--id`, so they need no XTCE of their own (use `--def <file>`
to point at a specific `.json` or `.xml`).

### Fleets

Run several instances at once — replicas of one satellite or entirely different
ones — each its own process with its own `--id` and `--port`:

```bash
xtce-sim run examples/imaging_sat/imaging_sat.xml --id sat-a --port 5001 &
xtce-sim run examples/imaging_sat/imaging_sat.xml --id sat-b --port 5002 &
xtce-sim run other_sat.xml  --id probe --port 5003 &
```

Each instance keys a stable color off its `--id`, so when their logs share a
terminal the `[id]` tags stay easy to tell apart (a given id is always the same
color). Control it with `--color auto|always|never`.

Try it with the bundled example satellite — three replicas in one terminal:

```bash
V=examples/imaging_sat/imaging_sat.xml
xtce-sim run $V --id sat-a --port 5001 --color always &
xtce-sim run $V --id sat-b --port 5002 --color always &
xtce-sim run $V --id sat-c --port 5003 --color always &
```

You'll see three colored `listening on …` lines. Send a different command to
each and watch it appear in that instance's color:

```bash
xtce-sim send --id sat-a --port 5001 SET_POWER SubsystemId=COMMS PowerState=ON
xtce-sim send --id sat-b --port 5002 TAKE_IMAGE ImageCount=3
xtce-sim send --id sat-c --port 5003 RESET SubsystemId=THERMAL ResetMode=HARD
```

```
16:46:10 [sat-a] listening on 127.0.0.1:5001 — 40 command(s), 13 packet(s)
16:46:10 [sat-b] listening on 127.0.0.1:5002 — 40 command(s), 13 packet(s)
16:46:13 [sat-a] command 0x10 SET_POWER args={'SubsystemId': 'COMMS', 'PowerState': 'ON'}
16:46:13 [sat-b] command 0x33 TAKE_IMAGE args={'ImageCount': 3}
```

Watch one instance's telemetry live, then stop the fleet:

```bash
xtce-sim monitor --id sat-b --port 5002 --style dashboard
kill $(jobs -p)          # or: pkill -f "xtce-sim run"
```

## Onboard sequences: ATS and RTS

The vehicle carries an onboard sequencer with one **ATS** slot and one
**RTS** slot, modeled on flight stored-command systems (cFS's Stored
Command app is the reference). An ATS — *absolute time sequence* — fires
each command at a UTC instant; an RTS — *relative time sequence* — fires
each command a delay after START. Plans are plain text, one command per
line, with `#` comments:

```
# window7.ats — times are absolute UTC
2026-07-15T12:00:00Z IMAGER_ON
2026-07-15T12:00:30Z TAKE_IMAGE ImageCount=3
2026-07-15T12:02:00Z IMAGER_OFF
```

```
# pass1.rts — delays in seconds after START (ms/s/m/h suffixes accepted)
+0   IMAGER_ON
+10  SET_EXPOSURE ExposureMs=40 GainLevel=2
+15  TAKE_IMAGE ImageCount=1
+30  IMAGER_OFF
```

### Validate on the ground

`seq check` runs every entry through the same encoding machinery the
uplink uses, so what passes here is exactly what LOAD will accept:

```bash
xtce-sim seq check pass1.rts --id sat-a
```

```
OK: pass1.rts — 4 command(s) over 30.0 s
  +0s  IMAGER_ON
  +10s  SET_EXPOSURE ExposureMs=40 GainLevel=2
  +15s  TAKE_IMAGE ImageCount=1
  +30s  IMAGER_OFF
```

A bad plan is refused with every problem listed by line — this one had a
typo'd argument name:

```
Error: pass1.rts: 1 problem(s):
  - line 3: SET_EXPOSURE: unknown argument(s) ['Gain']; valid: ['ExposureMs', 'GainLevel']
```

### Fly it: upload, LOAD, START

The plan reaches the vehicle the way any file does — over the command
link into the onboard store — and LOAD reads it from there, not from your
disk:

```bash
xtce-sim upload pass1.rts --id sat-a --port 5000
```

```bash
xtce-sim send --id sat-a --port 5000 LOAD_RTS Filename=pass1.rts
```

```bash
xtce-sim send --id sat-a --port 5000 START_RTS SeqId=1
```

(START and ABORT are declared `critical` in the ICD, so `send` announces
the significance before transmitting. `SeqId` is 1 — the ICD declares a
ValidRange of exactly 1..1 because there is one slot of each kind, and
the vehicle rejects anything else.)

From START, the vehicle is on its own. Every fired entry is encoded as a
real command packet and pushed through the same dispatch path a ground
command takes — argument validation, behavior effects, immediate
emissions, and the command echo all happen exactly as if you had typed
it, which is why the fires appear in the sim window and in the web
console's command log:

```
20:21:02 [sat-a] RTS started: pass1.rts
20:21:02 [sat-a] command 0x30 IMAGER_ON args={}
20:21:02 [sat-a]   effects: IMG_STATE=1, EVT_MESSAGE=b'IMAGER POWERED ON', EVT_EVENT_ID=10, IMG_FOCAL_PLANE_TEMP ramping to 35.0 (tau=20.0s)
20:21:02 [sat-a]   immediate: APID 0x11 emitted
20:21:02 [sat-a]   immediate: APID 0x14 emitted
20:21:02 [sat-a] RTS fired IMAGER_ON (1/4) -> SUCCESS
20:21:12 [sat-a] command 0x32 SET_EXPOSURE args={'ExposureMs': 40, 'GainLevel': 2}
20:21:12 [sat-a]   effects: IMG_EXPOSURE_MS=40, IMG_GAIN=2
20:21:12 [sat-a] RTS fired SET_EXPOSURE (2/4) -> SUCCESS
20:21:17 [sat-a] command 0x33 TAKE_IMAGE args={'ImageCount': 1}
20:21:17 [sat-a] RTS fired TAKE_IMAGE (3/4) -> SUCCESS
20:21:32 [sat-a] command 0x31 IMAGER_OFF args={}
20:21:32 [sat-a] RTS fired IMAGER_OFF (4/4) -> SUCCESS
20:21:32 [sat-a] RTS complete: pass1.rts (4 executed, 0 skipped)
```

Progress downlinks in the `ATS_STATUS`/`RTS_STATUS` packets. They ride
the normal beacon, and additionally push the moment anything happens — a
load, a start, a fire, a completion, a failure — so the console reacts
immediately instead of a beacon later:

```
┌ RTS_STATUS · APID 0x17 · seq 5 · 20:21:55.598
│ RTS_TIMESTAMP        1784172115  s
│ RTS_SEQ_ID           1
│ RTS_SEQ_NAME         pass1.rts
│ RTS_STATE            COMPLETE
│ RTS_CMD_TOTAL        4
│ RTS_CMD_EXECUTED     4
│ RTS_CMD_SKIPPED      0
│ RTS_CMD_REMAINING    0
│ RTS_ELAPSED_SEC      30
│ RTS_LAST_CMD_NAME    IMAGER_OFF
│ RTS_LAST_CMD_RESULT  SUCCESS
└───────────────────────────────────────────────
```

### The verbs

The command set matches cFS Stored Command's semantics:

- **LOAD** installs a plan from the store into its slot. The parsed plan
  is held in memory, so deleting or replacing the file afterwards cannot
  touch a loaded sequence.
- **START** runs the loaded plan from the top — whether freshly LOADED or
  already COMPLETE (an RTS can simply be re-run). There is no pause.
- **STOP** halts execution; the plan stays on board, reset exactly to the
  state a fresh LOAD leaves, and START runs it again from the top.
- **ABORT** halts and clears the slot entirely; re-running needs a new
  LOAD.
- A fired command that fails (a rejected argument, a handler error) is
  recorded FAILED and the sequence **continues** — one bad command does
  not strand the rest of a timeline, matching flight sequencers.

### ATS time is wall-clock UTC — and the vehicle is honest about it

The ATS time base is real UTC, so a plan written for this morning is
stale by afternoon. Starting an ATS whose leading entries are past
**skips** them (counted in `ATS_CMD_SKIPPED`, so the downlinked numbers
always add up); starting one whose entries are *all* past is refused —
burst-firing a stale timeline at a vehicle is exactly the accident this
rule prevents:

```
20:22:35 [sat-a] refused START_ATS: all 3 remaining command(s) are in the past — re-base the plan with 'seq shift' and load it again
```

The fix is the ground tool it names — re-base the plan and send it up
again:

```bash
xtce-sim seq shift window7.ats --start-in 30s --write
```

```
window7.ats: first command at 2026-07-16T03:23:22Z
```

Only the timestamps move (comments, spacing, and the command half of
every line are preserved byte-for-byte); the relative spacing between
entries is kept, so a choreographed pass stays choreographed.

### Failure is a state, not a shrug

A LOAD that cannot produce a plan — the file is missing, is not UTF-8
text, has the wrong extension for the slot, or fails validation — echoes
FAILED and lands the slot in **ERROR**, with the refusing plan's name
held in telemetry so the operator can see *what* failed, not just that
something did:

```
┌ ATS_STATUS · APID 0x16 · seq 22 · 20:26:55.393
│ ATS_TIMESTAMP        1784172415  s
│ ATS_SEQ_ID           0
│ ATS_SEQ_NAME         ghost.ats
│ ATS_STATE            ERROR
│ ATS_CMD_TOTAL        0
│ ATS_CMD_EXECUTED     0
│ ATS_CMD_SKIPPED      0
│ ATS_CMD_REMAINING    0
│ ATS_NEXT_CMD_TIME    0
│ ATS_LAST_CMD_NAME    
│ ATS_LAST_CMD_RESULT  PENDING
└────────────────────────────────────────────────
```

A good LOAD (or an ABORT) clears the error. A LOAD against a RUNNING slot
is refused outright — a bad load attempt must not tear down the plan
currently executing.

## Behavior: making commands change telemetry

The XTCE defines the *interface* — packets, fields, commands, encodings.
**Behavior TOML files** define what the vehicle *does*: what each command
changes, and how values evolve on their own. A satellite is a directory: the
XTCE and its per-subsystem behavior files (`thermal.toml`, `imager.toml`, ...)
live together, every `.toml` beside the XTCE is auto-discovered and merged —
strictly, so the same field in the same table in two files is a load error
naming both files (`--behavior <dir-or-file>` overrides discovery). Behavior
is kept out of the XTCE on purpose — no standard XTCE construct expresses
behavior, and the same interface files work unmodified in OpenC3/Yamcs.

```toml
[_initial]                       # seeded once at boot
THM_HEATER1_SETPOINT = 40.0

[HEATER_AUTO]                    # effects applied when HEATER_AUTO executes
"THM_HEATER{HeaterId}_STATE" = { set = "AUTO", emit = "immediate" }
"THM_HEATER{HeaterId}_TEMP" = { regulate = "@THM_HEATER{HeaterId}_SETPOINT",
                                band = 2.0, heats_to = 60.0, tau_heat = 30.0,
                                cools_to = 20.0, tau_cool = 45.0 }

[SET_HEATER_SETPOINT]
"THM_HEATER{HeaterId}_SETPOINT" = "@arg:Setpoint"

[_signals]                       # ambient behaviors running from boot
THM_PANEL_PLUS_X = { oscillate = 10.0, amplitude = 25.0, period = 5400, noise = 0.5 }
PWR_BATTERY_VOLTAGE = { hold = 24.0, noise = 0.3 }
```

That is a working thermal subsystem: `HEATER_AUTO HeaterId=1` flips the state
enum (acknowledged instantly — see below) and starts a real thermostat loop:
the heating element drives the temperature up at one rate, cuts out at the
top of the hysteresis band around the setpoint, lets it relax toward ambient
at another rate, cuts back in at the bottom — the sawtooth a real thermostat
draws. The setpoint is re-read live forever, so commanding a new one recenters
the loop at any time. Meanwhile the panel rides a 90-minute orbit sine and
the battery bus jitters around 24 V, no commands required.

**The verbs.** A bare scalar sets a field when the command executes;
`"@arg:Name"` copies a command argument (enum labels arrive as labels, stored
as raw values); `{ increment = n }` adds. Four verbs are *continuous* —
registered per field and advanced by the beacon clock: `ramp_to`/`tau`
(first-order approach, dt-independent), `oscillate` with `amplitude`, `period`
(seconds — always periods, never Hz), optional `shape` (`sine`, `triangle`,
`sawtooth`) and `phase`, `hold` (keeps re-asserting a value or tracking an
`@FIELD`), and `regulate` (bang-bang regulation: an internal element heats
toward `heats_to` or relaxes toward `cools_to`, flipping at the edges of the
hysteresis `band` around a live center — it never retires, so a changed
setpoint is always honored). The continuous verbs compose with `noise = stddev` — Gaussian
jitter with one seeded RNG per field, so runs reproduce exactly. One behavior
per field: a new command's behavior replaces the old (HEATER_OFF's cooling
displaces HEATER_ON's warming), and a direct set cancels it — last command
wins. `{ArgName}` templates in field names scale one rule across a family of
fields: an integer argument fills in its value (`THM_HEATER{HeaterId}_STATE`
reaches heater 1 or 2), and an enumerated argument fills in its **label**
(`PWR_{SubsystemId}_STATE` sends COMMS to `PWR_COMMS_STATE`) — a raw enum
value with no declared label, or a string that is not one of the declared
labels, refuses to resolve, and the effect is skipped with a warning. An `@FIELD` reference may not name its own field (feeding a
field its own output is drift, not jitter).

**Values are engineering units.** A calibrated field transmits raw counts on
the wire, but behavior values mean what they say — `ramp_to = 40.0` on a
temperature is forty degrees, and the engine converts to counts at the wire
boundary (and back, for live `@FIELD` references). Command a setpoint of
25.304 and the readback shows 25.30: the round trip through integer counts
quantizes, exactly like real telemetry.

**Validation is strict and total.** Every field name, argument reference, enum
label, verb, and attribute is checked against the XTCE at load, and *all*
problems are reported in one error — a broken sidecar blocks startup rather
than misbehaving quietly. At runtime the engine is deliberately liberal: a
skipped effect logs a warning and the beacon keeps flowing.

### Immediate emission

By default a command's effects ride the next beacon. Mark an instant effect
(`set`/copy/`increment`) with `emit = "immediate"` and the packet containing
that field is emitted the moment the command executes — an *extra*
transmission, sequence counters continuous, beacon schedule untouched. This is
the standard command-acknowledgment / event-report pattern (PUS services 1
and 5): the ground learns the vehicle obeyed *now*. Several immediate fields
in one packet emit it once; continuous verbs reject the flag at load.

The imaging satellite uses it to turn EVENT_LOG into a real event channel:

```toml
[TAKE_IMAGE]
IMG_STATE = { set = "CAPTURING", emit = "immediate" }
EVT_MESSAGE = { set = "IMAGE CAPTURE STARTED", emit = "immediate" }
EVT_EVENT_ID = { set = 11, emit = "immediate" }
```

See it: slow the imager packet right down (its declared period is 1 s), so
the instant emission stands out against a ten-second gap —

```bash
xtce-sim run examples/imaging_sat/imaging_sat.xml --port 5000 --interval 10
xtce-sim monitor --def examples/imaging_sat/imaging_sat.xml --port 5000
xtce-sim send --def examples/imaging_sat/imaging_sat.xml --port 5000 SET_TLM_RATE Packet=IMAGER_STATUS PeriodMs=10000
xtce-sim send --def examples/imaging_sat/imaging_sat.xml --port 5000 TAKE_IMAGE ImageCount=3
```

IMAGER_STATUS (`STATE=CAPTURING`) and EVENT_LOG (`EVENT_ID=11`) appear alone,
out of rhythm, the instant the TAKE_IMAGE lands — the retimed imager packet
otherwise beacons on its new ten-second beat (and EVENT_LOG, which declares
no rate, paces on the `--interval 10` fallback). `xtce-sim inspect` narrates
a loaded sidecar (initial values, boot signals, per-command effects,
`[emit: immediate]` marks), so you can review the behavior without running
anything.

### Telemetry rates: every packet has its own period

Real vehicles do not beacon everything at one rate, and neither does the
sim. Each telemetry container in the XTCE declares its nominal rate with
the standard's own element — this is POWER_STATUS in
`examples/imaging_sat/imaging_sat.xml`:

```xml
<xtce:SequenceContainer name="POWER_STATUS" shortDescription="Electrical power system status">
  <!-- beacons every 2 s -->
  <xtce:DefaultRateInStream basis="perSecond" minimumValue="0.5" />
```

The parser reads the declaration (one more XTCE element that used to be
ignored), the server paces each packet on its own period, and `inspect
--dump` shows the schedule as durations:

```
APID 0x10  HOUSEKEEPING  [every 1 s]
APID 0x11  IMAGER_STATUS  [every 1 s]
APID 0x12  POWER_STATUS  [every 2 s]
APID 0x13  THERMAL_STATUS  [every 2 s]
```

Packets that declare no rate (my_vehicle declares none anywhere) pace on
the `--interval` fallback, exactly the old behavior — imaging_sat's
EVENT_LOG is such a packet, while FILE_RECEIPT is event-only and never
beacons at all. In flight, `SET_TLM_RATE`
retimes one packet — PUS-style per-packet periods, addressed by name, the
period a duration in milliseconds:

```
13:18:13 [sat-a] command 0x11 SET_TLM_RATE args={'Packet': 'IMAGER_STATUS', 'PeriodMs': 10000}
13:18:13 [sat-a]   telemetry period: IMAGER_STATUS every 10 s
```

The retimed packet emits immediately (announcing its new cadence) and then
holds the commanded period until another SET_TLM_RATE — or a restart —
restores the declared one.

### Beacon control: ENABLE_BEACON

A vehicle whose XTCE declares an `ENABLE_BEACON` command (with an
ENABLE/DISABLE `BeaconState` argument) gets real beacon control — the same
opt-in-by-declaration convention as the `FILE_*` and ATS/RTS command
families. `DISABLE` stops the periodic beacon: physics keep ticking, and
command-caused transmissions (command echoes, `emit = "immediate"` effects,
file receipts, sequence status) still flow, but the beacon stays quiet
until an `ENABLE` arrives. One carve-out to know: beacon-off does not
pause the vehicle's stored program — a *running* ATS/RTS keeps executing,
and its fired commands re-enter the normal dispatch path, so their echoes,
immediate effects, and sequence-status pushes still transmit on the
sequence's own clock.

The imaging satellite pairs the gate with a telemetry mirror in
`comms.toml`, so the commanded state is visible on the COMMS_STATUS card.
Because the sidecar's immediate emission runs before the gate flips,
`DISABLE`'s own COMMS_STATUS push — showing `DISABLE` — is the link's last
autonomous packet:

```
06:34:10 [sat-a] command 0x52 ENABLE_BEACON args={'BeaconState': 'DISABLE'}
06:34:10 [sat-a]   effects: COMM_BEACON_STATE=0
06:34:10 [sat-a]   immediate: APID 0x1C emitted
06:34:10 [sat-a]   beacon disabled (periodic telemetry quiet)
```

With the beacon disabled, `monitor` and the web console go quiet and the
console's panels dim stale — that is not a bug; it is what you commanded
the vehicle to do.

The quiet vehicle can still be polled. `GET_STATUS` (same
opt-in-by-declaration convention) commands one immediate pass over every
periodic packet, whatever the beacon gate says, so `monitor` shows the
vehicle's full current state the moment the command lands and then goes
quiet again. From the same session, mid-silence, and then the `ENABLE`
that ends it:

```
06:34:11 [sat-a] command 0x02 GET_STATUS args={}
06:34:11 [sat-a]   status: telemetry snapshot emitted (12 packet(s))
06:34:13 [sat-a] command 0x52 ENABLE_BEACON args={'BeaconState': 'ENABLE'}
06:34:13 [sat-a]   effects: COMM_BEACON_STATE=1
06:34:13 [sat-a]   immediate: APID 0x1C emitted
06:34:13 [sat-a]   beacon enabled
```

### Physics models: the ADCS flies

Some subsystems are too coupled for per-field verbs: an attitude slew is not
a value changing, it is a rigid body rotating because reaction wheels torqued
it. The `[_models]` construct hands a whole slice of the telemetry space to a
physics model. The imaging satellite's `adcs.toml` declares one:

```toml
[_models.adcs]
kind = "adcs"
substep = 0.1                # s of physics per RK4 step

[_models.adcs.body]
inertia = [12.0, 14.0, 9.0]  # kg*m^2

[[_models.adcs.wheels]]      # x4: the reaction wheel pyramid
axis = [0.6, 0.0, 0.8]
inertia = 0.02
max_torque = 0.05
max_speed = 600.0

[_models.adcs.outputs]       # model outputs -> XTCE fields, explicitly
ADCS_MODE = "mode"
ADCS_ATT_QUAT_Q1 = "quat_q1"
# ... 41 bindings in the shipped file
```

The world the model flies in is *not* the model's property. One vehicle has
exactly one solar system — orbit, sun, eclipse, magnetic field — declared
once at the vehicle level (the example puts it in `system.toml`) and shared
by every model:

```toml
[_environment.orbit]
altitude_km = 500.0
inclination_deg = 51.6
```

Declaring an orbit inside a model is a load error pointing here; two models
can therefore never disagree about where the sun is.

Behind those bindings runs owned, dependency-free physics: Euler's rigid-body
equation with wheel momentum exchange integrated by RK4, a quaternion-feedback
PD controller, a circular orbit with sun, eclipse, and a rotating tilted-dipole
magnetic field, and modeled sensors (star tracker with a sun-exclusion cone,
gyro with bias, sun sensor, magnetometer) feeding an estimator. **The control
loop closes on the estimates, and the telemetry reports them** — command a
bogus gyro bias and the vehicle genuinely settles off-target by the closed-form
amount an ADCS engineer would predict.

The ADCS commands are inputs to the model, not table entries:
`ADCS_SLEW_TO_QUATERNION` starts a real slew that converges over tens of
seconds through saturated wheel torques (watch `ADCS_POINTING_ERR` fall in the
web console); `ADCS_SET_MODE Mode=NADIR` tracks the LVLH frame around the
orbit; `Mode=DETUMBLE` runs a filtered B-dot law through the magnetorquers,
exactly as slowly as the real technique; `ADCS_DESATURATE` dumps wheel
momentum through the magnetorquers while the hold loop keeps pointing —
`ADCS_MOMENTUM_TOTAL` drains on live telemetry. Wheel currents follow
delivered motor torque; speeds read back in RPM, rates in deg/s, the field in
µT — the XTCE's units, converted from the model's SI internals.

The second model is the electrical power system (`power.toml`,
`kind = "power"`), and it exists to make the vehicle's subsystems
*interconnected*: solar generation follows the same sun and eclipse the ADCS
flies in, scaled by how squarely the wings can face the sun given the body's
**real attitude** (perfect single-axis SADA tracking — slew the imager at a
target and generation drops by the cosine of what the wings can no longer
reach). The battery is a state, not a signal: charge integrates every tick,
terminal voltage follows charge and sags under load, and the charge
controller tapers near full and shunts the surplus. Each switched load draws
what the vehicle is actually *doing* while its `PWR_*_STATE` reads ON: the
ADCS load includes the live wheel currents the dynamics model computes (a
slew pulls real amps), the imager's draw follows `IMG_STATE` (idle
keep-alive vs the full capture draw), COMMS adds beacon transmit while
`COMM_BEACON_STATE` reads ENABLE, and each heater element draws when forced
ON — or, under `HEATER_AUTO`, exactly while its thermostat's element is lit,
so the regulate loop's duty sawtooth appears in `PWR_BATTERY_CURRENT`
(signed: positive charging, negative discharging). Fly an orbit and the
battery breathes: discharge through eclipse, recharge in the sun.

Nothing about the model is specific to this satellite. A second vehicle —
kept as a test fixture rather than an example
([`tests/data/my_vehicle/`](tests/data/my_vehicle)) — flies the same model as
a **three-wheel** variant whose ICD is a deliberate subset: three orthogonal
wheels instead of the four-wheel pyramid, six of the eleven command roles,
fewer telemetry fields, and a mode enumeration without TARGET_TRACK (whose
STANDBY is a different raw value). Validation checks the mode binding against
the modes *that vehicle can actually reach* through its wired commands, so a
leaner ICD is a correct configuration, not an error. That fixture is how the
suite proves the engine is driven by the XTCE rather than built around any one
satellite.

Ownership is validated at load: a field bound under `[outputs]` belongs to
the model, and any `[_initial]` seed or command-table effect that targets it
is a load error naming the model — one source of truth per field. All
simplifications (circular unperturbed orbit, fixed inertial sun, cylindrical
shadow, centered dipole, quasi-static wheel temperatures) are documented in
the module docstrings rather than hidden.

## Development

```bash
uv run pytest                             # run the test suite
uv run pytest --cov=xtce_sim              # with coverage (gate: fail_under=90%)
uv run ruff check xtce_sim                # lint
```

The fleet and logging behavior has direct coverage:

```bash
uv run pytest tests/test_logs.py tests/test_server.py -v
```

- `test_instance_color_is_deterministic` — an `--id` always maps to the same color
- `test_colors_spread_across_whole_palette` — ids exercise every palette color
- `test_two_instances_serve_independently` — two servers on separate ports, each
  serving its own client

Continuous integration ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs
lint + tests + the coverage gate on Python 3.11–3.13, and a
[SonarQube Cloud](docs/sonarcloud.md) scan.

Confirm the color mapping directly (`sat-a` is the same color both times):

```bash
uv run python -c "from xtce_sim import logs; \
[print(f'{i:6} -> {logs.instance_color(i)}') for i in ['sat-a','sat-b','sat-c','sat-a']]"
```

## Status

Early development.

## License

MIT — see [LICENSE](LICENSE).
