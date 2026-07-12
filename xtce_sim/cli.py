"""
xtce-sim command-line interface.

    xtce-sim generate <file.xml>                  # build + dump resolved cmd/tlm, stop
    xtce-sim generate <cmds.xml> <tlm.xml>        # merge multiple XTCE files
    xtce-sim run <file.xml> --port 5000           # build + dump, then serve on TCP
    xtce-sim run <file.xml> --port 5000 --id sat-a --interval 0.5

`generate` builds the in-memory SimDefinition and dumps it to runs/<id>/ as
cmd_tlm.txt (human) and cmd_tlm.json (machine). `run` does the same, then starts
a CCSDS simulator on an explicit TCP port. `--emit-py` additionally writes
generated.py, an importable snapshot for scripting (the sim never imports it).
"""

from __future__ import annotations

import asyncio
import functools
import glob
import logging
import socket
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from pathlib import Path

import click

from xtce_sim import behavior, ccsds, client, codec, fileservice, render, sequences
from xtce_sim.definition import SimDefinition
from xtce_sim.exercise import build_send_plan, reject_probe, run_exercise
from xtce_sim.generate import GeneratorError, emit_python, format_json, format_text
from xtce_sim.logs import enable_trace, setup_logging
from xtce_sim.server import SimServer
from xtce_sim.synth import LiveTelemetry

_XTCE_ARG = click.argument(
    "xtce",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, path_type=Path),
)
_ID_OPT = click.option(
    "--id",
    "instance_id",
    default=None,
    help="Instance id; output goes to <satellite dir>/runs/<id>/ (default: first file's stem).",
)
_OUT_OPT = click.option(
    "--out",
    "out_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Output directory (overrides runs/<id>/).",
)
_EMIT_PY_OPT = click.option(
    "--emit-py",
    is_flag=True,
    help="Also emit generated.py — an importable snapshot of the definition.",
)
_VERBOSE_OPT = click.option(
    "-v",
    "--verbose",
    count=True,
    help="Trace parser/builder decisions and inferences (-v); add every parsed element (-vv).",
)


_BEHAVIOR_OPT = click.option(
    "--behavior",
    "behavior_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Behavior source: a satellite directory or one .toml file "
    "(default: the first XTCE's directory, when it holds .toml files).",
)
_NO_BEHAVIOR_OPT = click.option(
    "--no-behavior",
    is_flag=True,
    help="Serve the interface only: skip behavior discovery and loading.",
)


def _maybe_enable_trace(verbose: int) -> None:
    if verbose:
        enable_trace(logging.DEBUG if verbose > 1 else logging.INFO)


def _load_behavior_engine(
    path: Path | None, simdef: SimDefinition
) -> behavior.BehaviorEngine | None:
    """Build the runtime engine from a sidecar; validation problems are fatal."""
    if path is None:
        return None
    try:
        spec = behavior.load_behavior(path, simdef)
    except behavior.BehaviorError as exc:
        raise click.ClickException(
            f"{exc}\n(behavior auto-discovered from {path} — pass "
            "--behavior <dir-or-file> to override, or --no-behavior to skip)"
        ) from exc
    return behavior.BehaviorEngine(spec, simdef)


def _build_and_dump(
    xtce: tuple[Path, ...],
    instance_id: str | None,
    out_dir: Path | None,
    emit_py: bool,
) -> tuple[SimDefinition, str, Path]:
    """Build the SimDefinition and dump cmd_tlm.{txt,json} (+ generated.py)."""
    simdef = SimDefinition.from_xtce(list(xtce))
    instance_id = instance_id or xtce[0].stem
    # Artifacts live with the satellite: <satellite dir>/runs/<id>/.
    out = out_dir or xtce[0].resolve().parent / "runs" / instance_id
    out.mkdir(parents=True, exist_ok=True)

    (out / "cmd_tlm.txt").write_text(format_text(simdef))
    (out / "cmd_tlm.json").write_text(format_json(simdef))
    click.echo(f"Space system : {simdef.space_system_name}")
    click.echo(f"Commands     : {len(simdef.commands)}")
    click.echo(f"Telemetry    : {len(simdef.packets)} packet(s)")
    click.echo(f"Wrote {out / 'cmd_tlm.txt'}")
    click.echo(f"Wrote {out / 'cmd_tlm.json'}")
    if emit_py:
        (out / "generated.py").write_text(emit_python(simdef))
        click.echo(f"Wrote {out / 'generated.py'}")

    return simdef, instance_id, out


def _find_run_dump(instance_id: str) -> Path | None:
    """Locate runs/<id>/cmd_tlm.json: here, or in a satellite directory below.

    Dumps live with their satellite (<sat dir>/runs/<id>/), so a client run
    from the repo root searches a couple of levels down. Ambiguity (the same
    id under two satellites) is an error rather than a guess.
    """
    rel = Path("runs") / instance_id / "cmd_tlm.json"
    esc = glob.escape(instance_id)
    candidates = [
        p
        for p in (
            rel,
            *Path(".").glob(f"*/runs/{esc}/cmd_tlm.json"),
            *Path(".").glob(f"*/*/runs/{esc}/cmd_tlm.json"),
        )
        if p.exists()
    ]
    unique = sorted({c.resolve() for c in candidates})
    if len(unique) > 1:
        raise click.ClickException(
            f"--id {instance_id} is ambiguous: " + ", ".join(str(u) for u in unique)
        )
    return unique[0] if unique else None


def _load_definition(instance_id: str | None, def_path: Path | None) -> SimDefinition:
    """Resolve a SimDefinition for a client verb from --def or --id.

    --def points at either an XTCE .xml (parsed) or a dumped cmd_tlm.json
    (loaded). --id is shorthand for runs/<id>/cmd_tlm.json.
    """
    if def_path is not None:
        if def_path.suffix.lower() == ".json":
            return SimDefinition.from_json(def_path)
        return SimDefinition.from_xtce(def_path)
    if instance_id is not None:
        json_path = _find_run_dump(instance_id)
        if json_path is None:
            raise click.ClickException(
                f"no runs/{instance_id}/cmd_tlm.json found here or in any "
                f"satellite directory below — run the sim with --id "
                f"{instance_id} first, or pass --def <file>."
            )
        return SimDefinition.from_json(json_path)
    raise click.ClickException("specify --id <id> or --def <file> for the definition")


@click.group()
@click.version_option(package_name="xtce-sim")
def main() -> None:
    """Run a CCSDS satellite simulator straight from an XTCE file."""


@main.command()
@_XTCE_ARG
@_ID_OPT
@_OUT_OPT
@_EMIT_PY_OPT
@_VERBOSE_OPT
def generate(
    xtce: tuple[Path, ...],
    instance_id: str | None,
    out_dir: Path | None,
    emit_py: bool,
    verbose: int,
) -> None:
    """Build the resolved cmd/tlm definition from XTCE and dump it to disk."""
    _maybe_enable_trace(verbose)
    _build_and_dump(xtce, instance_id, out_dir, emit_py)


@main.command()
@_XTCE_ARG
@click.option(
    "--full",
    is_flag=True,
    help="Trace every parsed element, not just decisions and inferences.",
)
@click.option(
    "--dump",
    is_flag=True,
    help="Also print the full resolved command/telemetry report "
    "(same content as runs/<id>/cmd_tlm.txt; still writes nothing).",
)
@click.option(
    "--behavior",
    "behavior_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Behavior source to validate and narrate: a satellite directory or "
    "one .toml file (default: the first XTCE's directory).",
)
@_NO_BEHAVIOR_OPT
def inspect(
    xtce: tuple[Path, ...],
    full: bool,
    dump: bool,
    behavior_path: Path | None,
    no_behavior: bool,
) -> None:
    """Narrate what the parser sees in an XTCE file and what it infers.

    Parses and builds (writing nothing to disk), tracing the parser's
    decisions as it goes: inferred sizes, applied defaults, leniency
    fallbacks, inheritance resolution, synthetic opcodes, flattenings —
    and, after the parse, any element the file declared but the parser
    never read (unsupported XTCE features). Lines marked ``~`` are
    inferences and gaps rather than explicit declarations. ``--dump``
    appends the full resolved inventory (every command and packet).

    A behavior sidecar (found by convention or via --behavior) is loaded,
    fully validated against the definition — any problem is a hard error —
    and narrated: what every command does to telemetry.
    """
    enable_trace(logging.DEBUG if full else logging.INFO)
    try:
        simdef = SimDefinition.from_xtce(list(xtce))
    except (ET.ParseError, GeneratorError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if dump:
        click.echo()
        click.echo(format_text(simdef))
        click.echo()
    source = None if no_behavior else (behavior_path or behavior.sidecar_path(list(xtce)))
    _inspect_behavior(source, simdef)
    click.echo(
        f"OK: {simdef.space_system_name} — {len(simdef.commands)} command(s), "
        f"{len(simdef.packets)} packet(s)"
    )


def _inspect_behavior(path: Path | None, simdef: SimDefinition) -> None:
    """Validate and narrate a behavior sidecar; validation problems are fatal."""
    if path is None:
        return
    try:
        spec = behavior.load_behavior(path, simdef)
    except behavior.BehaviorError as exc:
        raise click.ClickException(
            f"{exc}\n(behavior auto-discovered from {path} — pass "
            "--behavior <dir-or-file> to override, or --no-behavior to skip)"
        ) from exc
    click.echo(f"\nBehavior ({path}):")
    for line in behavior.describe(spec):
        click.echo(f"  {line}")
    click.echo()


@main.command()
@_XTCE_ARG
@click.option(
    "--port",
    required=True,
    type=click.IntRange(1, 65535),
    help="TCP port to serve on (required).",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Host/interface to bind.")
@click.option(
    "--interval",
    default=1.0,
    show_default=True,
    type=float,
    help="Telemetry beacon interval in seconds.",
)
@click.option(
    "--color",
    type=click.Choice(["auto", "always", "never"]),
    default="auto",
    show_default=True,
    help="Colorize log output (per-instance color keyed off --id).",
)
@click.option(
    "--live",
    is_flag=True,
    help="Beacon changing synthetic values instead of zeros.",
)
@_ID_OPT
@_OUT_OPT
@_EMIT_PY_OPT
@_VERBOSE_OPT
@_BEHAVIOR_OPT
@_NO_BEHAVIOR_OPT
def run(
    xtce: tuple[Path, ...],
    port: int,
    host: str,
    interval: float,
    color: str,
    live: bool,
    instance_id: str | None,
    out_dir: Path | None,
    emit_py: bool,
    verbose: int,
    behavior_path: Path | None,
    no_behavior: bool,
) -> None:
    """Build, dump, then serve a CCSDS simulator on an explicit TCP port."""
    _maybe_enable_trace(verbose)
    simdef, resolved_id, run_dir = _build_and_dump(xtce, instance_id, out_dir, emit_py)

    logger = setup_logging(resolved_id, color=color)
    source = None if no_behavior else (behavior_path or behavior.sidecar_path(list(xtce)))
    engine = _load_behavior_engine(source, simdef)
    # The vehicle's file store lives with its other run artifacts; it
    # persists across restarts of the same instance, as real storage would.
    store = fileservice.FileStore(run_dir / "files")
    service = fileservice.FileService(store, simdef, logger=logger)

    server = SimServer(
        simdef,
        host=host,
        port=port,
        beacon_interval=interval,
        telemetry_source=LiveTelemetry() if live else None,
        behavior_engine=engine,
        file_service=service,
        logger=logger,
    )

    if engine is not None:
        click.echo(
            f"Behavior: {engine.spec.source_label} — {len(engine.spec.commands)} "
            f"command(s) with effects, {len(engine.spec.initial)} initial value(s)"
        )
    click.echo(
        f"File store: {store.root} — {len(store.names())} file(s), "
        f"{store.used()} bytes used"
    )
    click.echo(f"Serving {resolved_id} on {host}:{port} (Ctrl-C to stop)")
    try:
        # serve_forever() binds, beacons, and cleans up (stop()) in its finally.
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        click.echo("\nStopped.")
    except OSError as exc:
        # Bind failure (port in use, bad host, ...) — a clean error, like send/monitor.
        raise click.ClickException(f"could not serve on {host}:{port} — {exc}") from exc


@main.command()
@click.argument("command_args", nargs=-1, required=True)
@click.option("--port", required=True, type=int, help="TCP port of the running sim.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to connect to.")
@click.option("--id", "instance_id", default=None, help="Load def from runs/<id>/cmd_tlm.json.")
@click.option(
    "--def",
    "def_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Definition source: an XTCE .xml or a cmd_tlm.json.",
)
@click.option("--apid", default=1, show_default=True, type=int, help="APID for the command packet.")
@click.option(
    "--force",
    is_flag=True,
    help="Transmit even if arguments violate their declared ValidRanges "
    "(tests the vehicle's own guards; expect a rejection).",
)
def send(
    command_args: tuple[str, ...],
    port: int,
    host: str,
    instance_id: str | None,
    def_path: Path | None,
    apid: int,
    force: bool,
) -> None:
    """Send a command: xtce-sim send --id sat-a --port 5000 SET_POWER SubsystemId=3 PowerState=ON."""
    simdef = _load_definition(instance_id, def_path)

    name, *pairs = command_args
    command = simdef.command_by_name(name)
    if command is None:
        raise click.ClickException(f"unknown command {name!r}")

    args: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.ClickException(f"expected KEY=VALUE, got {pair!r}")
        key, _, value = pair.partition("=")
        args[key] = value

    if command.hazardous:
        # Surface the XTCE-declared criticality (display only — a real
        # arm/fire confirmation gate is future work, and would break scripts
        # if sprung on them silently). To stderr: scripts parsing send's
        # stdout keep seeing exactly the lines they always did.
        color = "red" if command.significance in ("critical", "forbidden") else "yellow"
        why = f": {command.significance_reason}" if command.significance_reason else ""
        click.echo(
            click.style(f"{command.name} is {command.significance.upper()}{why}", fg=color),
            err=True,
        )

    if force:
        click.echo(
            click.style("--force: skipping ground-side range checks", fg="yellow"),
            err=True,
        )
    try:
        client.send_command(host, port, command, args, apid=apid, validate=not force)
    except (ValueError, struct.error) as exc:
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(f"could not reach {host}:{port} — {exc}") from exc

    click.echo(f"sent {command.name} (0x{command.opcode:02X}) args={args or '{}'}")


@main.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--port", required=True, type=int, help="TCP port of the running sim.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to connect to.")
@click.option("--id", "instance_id", default=None, help="Load def from runs/<id>/cmd_tlm.json.")
@click.option(
    "--def",
    "def_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Definition source: an XTCE .xml or a cmd_tlm.json.",
)
@click.option(
    "--timeout",
    default="10s",
    show_default=True,
    help="How long to wait for the vehicle's receipt (e.g. 10s, 1m).",
)
@click.option(
    "--chunk-size",
    default=client.UPLOAD_CHUNK_SIZE,
    show_default=True,
    type=click.IntRange(1, ccsds.FILE_CHUNK_MAX),
    help="Bytes per DATA frame on the wire.",
)
def upload(
    file: Path,
    port: int,
    host: str,
    instance_id: str | None,
    def_path: Path | None,
    timeout: str,
    chunk_size: int,
) -> None:
    """Upload a file to the vehicle's onboard store, over the command link.

    The file goes up in chunks on the reserved file-uplink APID; the vehicle
    reassembles it, verifies size and CRC-32, lands it in runs/<id>/files/,
    and answers with a FILE_RECEIPT — which this command waits for, so the
    exit status is the vehicle's verdict, not just "bytes were sent".
    """
    simdef = _load_definition(instance_id, def_path)
    try:
        wait = sequences.parse_duration(timeout)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    problem = fileservice.name_problem(file.name)
    if problem is not None:
        # The vehicle would refuse this name anyway; say why before sending.
        raise click.ClickException(f"{file.name}: {problem}")
    try:
        data = file.read_bytes()
    except OSError as exc:
        raise click.ClickException(f"could not read {file}: {exc}") from exc

    chunks = -(-len(data) // chunk_size)  # ceiling; 0 for an empty file
    click.echo(
        f"uploading {file.name} — {len(data)} bytes, "
        f"CRC-32 0x{zlib.crc32(data) & 0xFFFFFFFF:08X}, {chunks} chunk(s)"
    )
    try:
        receipt = client.upload_file(
            host,
            port,
            file.name,
            data,
            simdef=simdef,
            chunk_size=chunk_size,
            timeout=wait,
        )
    except client.UploadError as exc:
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(f"could not reach {host}:{port} — {exc}") from exc

    if receipt is None:
        click.echo(
            click.style(
                "sent — this vehicle downlinks no FILE_RECEIPT, so the "
                "transfer is not confirmed",
                fg="yellow",
            )
        )
        return
    click.echo(
        f"receipt: SUCCESS — {file.name} aboard ({receipt['FR_FILE_SIZE']} bytes); "
        f"storage {receipt['FR_STORAGE_USED']} used / "
        f"{receipt['FR_STORAGE_AVAILABLE']} available"
    )


def _decode_packet(
    packet: bytes,
    simdef: SimDefinition,
    wanted: set[str],
    prefixes: dict[int, str],
    raw: bool = False,
):
    """Decode one CCSDS frame -> (apid, name, seq, meta, prefix), or None to skip.

    ``prefixes`` is a per-APID cache of the shared field-name prefix (mutated).
    Skips runt frames (<6 bytes) and, when a filter is active, unwanted packets.
    ``raw`` shows wire counts instead of calibrated engineering values.
    """
    if len(packet) < 6:  # runt frame from a misbehaving/other-protocol server
        return None
    header = ccsds.CCSDSHeader.unpack(packet[:6])
    if header.apid == ccsds.CMD_ECHO_APID:
        # Command echoes are link infrastructure (see ccsds.py), rendered by
        # the web console's command log — not payload telemetry.
        return None
    packet_def = simdef.packet_by_apid(header.apid)
    name = packet_def.name if packet_def else f"APID_0x{header.apid:X}"
    if wanted and name not in wanted:
        return None
    meta: list = []
    prefix = ""
    if packet_def is not None:
        try:
            values = codec.unpack_telemetry(packet_def, packet[6:])
            meta = [
                (
                    f.name,
                    _display_value(f, values[f.name], raw),
                    # counts are unitless: the unit belongs to the calibrated view
                    None if raw and f.calibrator is not None else f.unit,
                )
                for f in packet_def.fields
            ]
            prefix = prefixes.setdefault(
                header.apid, render.common_prefix([f.name for f in packet_def.fields])
            )
        except struct.error:
            meta = [("<raw>", packet[6:22].hex(), None)]
    return header.apid, name, header.seq_count, meta, prefix


def _display_value(field, value, raw: bool = False):
    """The value as an operator wants to read it.

    Enumerated fields show their label; calibrated fields show the
    engineering value converted from the raw wire count (suppressed by
    ``raw``, which shows the counts as transmitted).
    """
    if field.enumerations:
        label = next((k for k, v in field.enumerations.items() if v == value), None)
        if label is not None:
            return label
    if (
        not raw
        and field.calibrator is not None
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return field.calibrator.apply(value)
    return value


@main.command()
@click.option("--port", required=True, type=int, help="TCP port of the running sim.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to connect to.")
@click.option("--id", "instance_id", default=None, help="Load def from runs/<id>/cmd_tlm.json.")
@click.option(
    "--def",
    "def_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Definition source: an XTCE .xml or a cmd_tlm.json.",
)
@click.option(
    "--packet",
    "packet_filter",
    multiple=True,
    help="Only show these packet names (repeatable).",
)
@click.option(
    "--style",
    "-s",
    type=click.Choice(["compact", "table", "dashboard"]),
    default="compact",
    show_default=True,
    help="Output style.",
)
@click.option(
    "--fields",
    "-f",
    "show_all_fields",
    is_flag=True,
    help="Show every field (compact style only; default shows the first few).",
)
@click.option(
    "--count",
    default=0,
    type=click.IntRange(min=0),
    help="Stop after N updates — packets in compact/table, frames in dashboard (0 = run forever).",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Show raw wire counts instead of calibrated engineering units.",
)
def monitor(
    port: int,
    host: str,
    instance_id: str | None,
    def_path: Path | None,
    packet_filter: tuple[str, ...],
    style: str,
    show_all_fields: bool,
    count: int,
    raw: bool,
) -> None:
    """Connect to a running sim and pretty-print decoded live telemetry."""
    simdef = _load_definition(instance_id, def_path)
    wanted = set(packet_filter)
    instance = instance_id or (def_path.stem if def_path else host)
    prefixes: dict[int, str] = {}
    decode = functools.partial(
        _decode_packet, simdef=simdef, wanted=wanted, prefixes=prefixes, raw=raw
    )

    click.echo(f"Monitoring {host}:{port} (style={style}, Ctrl-C to stop)")
    try:
        if style == "dashboard":
            _run_dashboard(host, port, instance, decode, count)
        else:
            _run_stream(host, port, style, decode, show_all_fields, count)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        raise click.ClickException(f"could not reach {host}:{port} — {exc}") from exc


def _stdout_isatty() -> bool:
    """Read at call time (and patchable in tests — CliRunner swaps sys.stdout)."""
    return sys.stdout.isatty()


def _run_stream(host, port, style, decode, show_all_fields, count) -> None:
    """Compact / table styles: render each packet as it arrives.

    On a TTY the table style repaints in place (cursor-home + erase-below,
    written with the frame in one go, so there is no blank-frame flash);
    when piped, tables append so the output stays greppable.
    """
    shown = 0
    table_in_place = style == "table" and _stdout_isatty()
    for packet in client.stream_packets(host, port):
        decoded = decode(packet)
        if decoded is None:
            continue
        apid, name, seq, meta, prefix = decoded
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if style == "table":
            frame = render.render_table(ts, apid, name, seq, meta)
            if table_in_place:
                # color=True keeps the escape intact even where click would
                # strip ANSI (we already know we're on a TTY).
                click.echo("\033[H\033[J" + frame, color=True)
            else:
                click.echo(frame)
        else:
            click.echo(
                render.render_compact(ts, apid, name, seq, meta, prefix, show_all=show_all_fields)
            )
        shown += 1
        if count and shown >= count:
            break


def _run_dashboard(host, port, instance, decode, count) -> None:
    """Dashboard style: keep the latest packet per APID and repaint each cycle.

    On a TTY the frame is repainted in place; when piped, each frame is appended
    so the output stays readable. A full frame is emitted once per beacon cycle,
    detected when the first APID of a cycle comes around again — so each frame
    shows a complete, consistent snapshot rather than a partially-updated one.
    """
    latest: dict = {}
    total = 0
    frames = 0
    first_apid = None
    is_tty = sys.stdout.isatty()

    def paint() -> None:
        if is_tty:
            click.echo("\033[H\033[J", nl=False)  # cursor home + clear screen
        click.echo(render.render_dashboard(host, port, instance, latest, total))
        if not is_tty:
            click.echo()

    for packet in client.stream_packets(host, port):
        decoded = decode(packet)
        if decoded is None:
            continue
        apid, name, seq, meta, prefix = decoded
        if first_apid is None:
            first_apid = apid
        # The cycle's lead APID recurring means the previous cycle is complete:
        # paint that full snapshot before folding in the new value.
        if apid == first_apid and latest:
            paint()
            frames += 1
            if count and frames >= count:
                break
        latest[apid] = (name, seq, meta, prefix)
        total += 1


@main.command()
@click.option("--port", required=True, type=int, help="TCP port of the running sim.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to connect to.")
@click.option("--id", "instance_id", default=None, help="Load def from runs/<id>/cmd_tlm.json.")
@click.option(
    "--def",
    "def_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Definition source: an XTCE .xml or a cmd_tlm.json.",
)
@click.option("--apid", default=1, show_default=True, type=int, help="APID for command packets.")
@click.option(
    "--command",
    "command_filter",
    multiple=True,
    help="Only exercise these commands (repeatable; default: all).",
)
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="After sending, read telemetry back to confirm the sim stayed healthy.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the commands/args that would be sent; connect to nothing.",
)
@click.option(
    "--pause",
    default=0.0,
    show_default=True,
    type=click.FloatRange(min=0),
    help="Seconds to wait after each send (narrates each send; makes effects watchable).",
)
@click.option(
    "--loop",
    is_flag=True,
    help="Repeat the sweep until Ctrl-C (pair with --pause for per-send narration).",
)
@click.option(
    "--reject-probes",
    default=0,
    show_default=True,
    type=click.IntRange(min=0),
    help="Sprinkle N deliberately out-of-range sends among the sweep "
    "(ground check bypassed) so the vehicle's rejection path gets exercised; "
    "placement is seeded-deterministic.",
)
def exercise(
    port,
    host,
    instance_id,
    def_path,
    apid,
    command_filter,
    verify,
    dry_run,
    pause,
    loop,
    reject_probes,
) -> None:
    """Send a valid instance of every command, then check telemetry health.

    Exercises the whole command surface thoroughly: one send per enum value and
    per numeric min/max boundary. --verify checks telemetry health — the sim
    stayed alive and every packet still decoded — not per-command effects
    (commands change telemetry when a behavior sidecar is loaded, but this
    exerciser does not yet check each declared effect).

    For watching effects live (monitor or web console), --pause 1 slows the
    sweep to one send per second and echoes each send as it goes; --loop
    repeats the whole sweep until interrupted. --reject-probes N mixes in
    invalid sends the vehicle must refuse — each shows up as ✗ rejected in
    the web console's command log.
    """
    simdef = _load_definition(instance_id, def_path)

    wanted = set(command_filter)
    if wanted:
        missing = wanted - {c.name for c in simdef.commands}
        if missing:
            raise click.ClickException(f"unknown command(s): {sorted(missing)}")
    targets = [c for c in simdef.commands if not wanted or c.name in wanted]

    if reject_probes and not any(reject_probe(c) for c in targets):
        # Asked-for probes that can't exist deserve a say-so, not silence.
        click.echo(
            click.style(
                "reject-probes: no selected command has a probe-able argument "
                "(no finite ValidRange or bounded enum) — none will be sent",
                fg="yellow",
            ),
            err=True,
        )

    if dry_run:
        _exercise_dry_run(targets, reject_probes)
        return

    # Fail fast with a clean message if the sim isn't reachable.
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError as exc:
        raise click.ClickException(f"could not reach {host}:{port} — {exc}") from exc

    any_failed, interrupted = _run_sweeps(
        simdef,
        targets,
        host,
        port,
        apid=apid,
        verify=verify,
        pause=pause,
        loop=loop,
        reject_probes=reject_probes,
    )
    # Exit code: any failed sweep fails the run (soak included); a Ctrl-C on
    # a one-shot run means it never finished, which is not success either.
    if any_failed:
        raise SystemExit(1)
    if interrupted and not loop:
        raise SystemExit(130)


def _run_sweeps(simdef, targets, host, port, *, apid, verify, pause, loop, reject_probes=0):
    """Run exercise sweeps (once, or looping until Ctrl-C).

    Returns (any_failed, interrupted). A looping run stops on its own when
    every send in a sweep fails — the sim is gone, and spinning at full
    speed against a dead port helps no one.
    """
    on_send = _echo_send if pause > 0 else None
    sweeps_done = 0
    any_failed = False
    interrupted = False
    try:
        while True:
            if loop:
                click.echo(f"— sweep {sweeps_done + 1} —")
            click.echo(f"Exercising {len(targets)} command(s) on {host}:{port} ...")
            report = run_exercise(
                simdef,
                host,
                port,
                apid=apid,
                commands={c.name for c in targets},
                verify=verify,
                pause=pause,
                on_send=on_send,
                reject_probes=reject_probes,
                # Seeded per sweep: deterministic overall, but each loop pass
                # sprinkles its probes at different points in the sweep.
                probe_seed=sweeps_done,
            )
            sweeps_done += 1
            any_failed = any_failed or not report.ok
            _print_exercise_report(report, verify=verify)
            if not loop:
                break
            if report.sends and not any(s.ok for s in report.sends):
                click.echo(click.style("every send failed — stopping the loop.", fg="red"))
                break
            if pause > 0:
                time.sleep(pause)  # breathe between sweeps, same rhythm as sends
    except KeyboardInterrupt:
        interrupted = True
        click.echo(
            f"\ninterrupted during sweep {sweeps_done + 1}; {sweeps_done} sweep(s) completed."
        )
    return any_failed, interrupted


def _echo_send(result) -> None:
    """One line per send, as it happens (interactive/paused runs)."""
    status = "ok" if result.ok else click.style("FAIL", fg="red")
    click.echo(f"  {result.command:<22} {result.label:<26} {status}")


def _exercise_dry_run(targets, reject_probes: int = 0) -> None:
    """Print the send plan ``exercise`` would run, without connecting."""
    plan, problems = build_send_plan(targets, reject_probes=reject_probes)
    for name, error in problems:
        click.echo(click.style(f"  {name:<22} unsatisfiable: {error}", fg="red"))
    for cmd, label, args, validate in plan:
        tag = ""
        if not validate:
            tag = " " + click.style("[REJECT-PROBE]", fg="magenta")
        elif cmd.hazardous:
            color = "red" if cmd.significance in ("critical", "forbidden") else "yellow"
            tag = " " + click.style(f"[{cmd.significance.upper()}]", fg=color)
        click.echo(f"  {cmd.name:<22} {label:<26} {args}{tag}")
    click.echo(f"{len(plan)} sends across {len(targets)} command(s) — dry run, nothing sent")


@main.command()
@click.option("--port", required=True, type=int, help="TCP port of the running sim.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Sim host to connect to.")
@click.option("--id", "instance_id", default=None, help="Load def from runs/<id>/cmd_tlm.json.")
@click.option(
    "--def",
    "def_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Definition source: an XTCE .xml or a cmd_tlm.json.",
)
@click.option(
    "--http-port",
    default=8080,
    show_default=True,
    type=click.IntRange(1, 65535),
    help="Port to serve the browser console on.",
)
@click.option(
    "--http-host",
    default="127.0.0.1",
    show_default=True,
    help="Host/interface to serve the console on.",
)
def ui(
    port: int,
    host: str,
    instance_id: str | None,
    def_path: Path | None,
    http_port: int,
    http_host: str,
) -> None:
    """Serve a live browser console fed by the running sim.

    Plays the ground station: connects to the sim's TCP port like `monitor`
    does, decodes each packet against the definition, and pushes every field
    (engineering units and raw counts) to the browser over WebSocket. The sim
    keeps speaking pure CCSDS; all decoding happens here. Reconnects if the
    sim restarts.
    """
    from xtce_sim import webui

    simdef = _load_definition(instance_id, def_path)
    click.echo(f"Console: http://{http_host}:{http_port}/  (sim {host}:{port}, Ctrl-C to stop)")
    try:
        asyncio.run(webui.run_ui(simdef, host, port, http_host, http_port))
    except KeyboardInterrupt:
        click.echo("\nStopped.")
    except OSError as exc:
        raise click.ClickException(
            f"could not serve the console on {http_host}:{http_port} — {exc}"
        ) from exc


def _print_exercise_report(report, *, verify: bool) -> None:
    """Echo per-command failures and the telemetry-health summary."""
    for s in report.failures:
        click.echo(click.style(f"  FAIL {s.command} [{s.label}]: {s.error}", fg="red"))
    total = len(report.sends)
    tail = f", {len(report.failures)} FAILED" if report.failures else ""
    click.echo(f"Commands: sent {total - len(report.failures)}/{total} OK{tail}")
    probes = sum(1 for s in report.sends if s.ok and s.label.startswith("reject-probe"))
    if probes:
        # "should reject": the exerciser doesn't read echoes — the vehicle's
        # verdict is visible on the downlink (console log / REJECTED echoes).
        click.echo(
            f"Rejection probes: {probes} transmitted — the vehicle should "
            f"reject each (✗ rejected in the console's command log)"
        )

    if not (verify and report.telemetry is not None):
        return
    t = report.telemetry
    if t.error:
        click.echo(click.style(f"Telemetry: could not read ({t.error})", fg="yellow"))
        return
    click.echo(
        f"Telemetry: {t.packets} packet(s), {len(t.apids)} APID(s), "
        f"{t.decode_failures} decode failure(s)"
    )
    if t.sample:
        click.echo(f"  sample: {t.sample}")


@main.group()
def seq() -> None:
    """Ground tools for ATS/RTS sequence files."""


def _read_sequence_text(path: Path) -> str:
    """The file's text, read ONCE — callers parse and transform this same
    string so nothing can change on disk between validation and use."""
    try:
        # newline="" keeps CRLF line endings intact so a shifted file
        # round-trips byte-for-byte outside its timestamp tokens.
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"could not read {path}: {exc}") from exc


def _parse_sequence(path: Path, text: str, simdef: SimDefinition | None):
    kind = sequences.KINDS.get(path.suffix.lower())
    if kind is None:
        raise click.ClickException(f"{path.name}: expected a .ats or .rts file")
    parse = sequences.parse_ats if kind == "ats" else sequences.parse_rts
    try:
        return parse(text, path.name, simdef)
    except sequences.SequenceError as exc:
        raise click.ClickException(str(exc)) from exc


@seq.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--id", "instance_id", default=None, help="Load def from runs/<id>/cmd_tlm.json.")
@click.option(
    "--def",
    "def_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Definition source: an XTCE .xml or a cmd_tlm.json.",
)
def check(file: Path, instance_id: str | None, def_path: Path | None) -> None:
    """Validate a sequence file against a satellite definition.

    Every entry is encoded through the same machinery the uplink uses, so
    what passes here is exactly what LOAD will accept.
    """
    simdef = _load_definition(instance_id, def_path)
    parsed = _parse_sequence(file, _read_sequence_text(file), simdef)
    click.echo(f"OK: {parsed.name} — {len(parsed.entries)} command(s) over {parsed.span:.1f} s")
    for entry in parsed.entries:
        when = sequences.format_utc(entry.time) if parsed.kind == "ats" else f"+{entry.time:g}s"
        args = " ".join(f"{k}={v}" for k, v in entry.args.items())
        click.echo(f"  {when}  {entry.command} {args}".rstrip())


@seq.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--start-in",
    "start_in",
    required=True,
    help="When the FIRST command should fire, from now (e.g. 30s, 5m, 1h).",
)
@click.option(
    "--write",
    is_flag=True,
    help="Rewrite the file in place instead of printing to stdout.",
)
def shift(file: Path, start_in: str, write: bool) -> None:
    """Move an ATS so its first command fires soon, preserving spacing.

    The vehicle judges lateness against real UTC honestly, so a stale plan
    must be re-based on the ground: this rewrites every timestamp by the
    same amount, leaving comments and layout untouched.
    """
    if file.suffix.lower() != ".ats":
        raise click.ClickException(
            "only .ats files carry absolute times; an RTS is already relative"
        )
    try:
        delay = sequences.parse_duration(start_in)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    text = _read_sequence_text(file)  # one read: parse and shift the same bytes
    parsed = _parse_sequence(file, text, None)
    # Whole-second delta: now+DUR is honored to within half a second and a
    # whole-second plan stays whole-second (no microsecond noise in diffs).
    delta = round((time.time() + delay) - parsed.entries[0].time)
    shifted = sequences.shift_ats(text, file.name, delta)
    if write:
        _write_atomic(file, shifted)
        first = sequences.format_utc(parsed.entries[0].time + delta)
        click.echo(f"{file.name}: first command at {first}")
    else:
        click.echo(shifted, nl=False)


def _write_atomic(path: Path, text: str) -> None:
    """Replace the file's contents all-or-nothing: a crash mid-write must
    not leave a half-written command plan behind."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise click.ClickException(f"could not write {path}: {exc}") from exc


if __name__ == "__main__":
    main()
