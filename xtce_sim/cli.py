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
import struct
import sys
from datetime import datetime
from pathlib import Path

import click

from xtce_sim import ccsds, client, codec, render
from xtce_sim.definition import SimDefinition
from xtce_sim.generate import emit_python, format_json, format_text
from xtce_sim.logs import setup_logging
from xtce_sim.server import SimServer
from xtce_sim.synth import LiveTelemetry

_XTCE_ARG = click.argument(
    "xtce",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
_ID_OPT = click.option(
    "--id",
    "instance_id",
    default=None,
    help="Instance id; output goes to runs/<id>/ (default: first file's stem).",
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


def _build_and_dump(
    xtce: tuple[Path, ...],
    instance_id: str | None,
    out_dir: Path | None,
    emit_py: bool,
) -> tuple[SimDefinition, str]:
    """Build the SimDefinition and dump cmd_tlm.{txt,json} (+ generated.py)."""
    simdef = SimDefinition.from_xtce(list(xtce))
    instance_id = instance_id or xtce[0].stem
    out = out_dir or Path("runs") / instance_id
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

    return simdef, instance_id


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
        json_path = Path("runs") / instance_id / "cmd_tlm.json"
        if not json_path.exists():
            raise click.ClickException(
                f"{json_path} not found — run the sim with --id {instance_id} first, "
                "or pass --def <file>."
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
def generate(
    xtce: tuple[Path, ...],
    instance_id: str | None,
    out_dir: Path | None,
    emit_py: bool,
) -> None:
    """Build the resolved cmd/tlm definition from XTCE and dump it to disk."""
    _build_and_dump(xtce, instance_id, out_dir, emit_py)


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
) -> None:
    """Build, dump, then serve a CCSDS simulator on an explicit TCP port."""
    simdef, resolved_id = _build_and_dump(xtce, instance_id, out_dir, emit_py)

    logger = setup_logging(resolved_id, color=color)

    server = SimServer(
        simdef,
        host=host,
        port=port,
        beacon_interval=interval,
        telemetry_source=LiveTelemetry() if live else None,
        logger=logger,
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
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Definition source: an XTCE .xml or a cmd_tlm.json.",
)
@click.option("--apid", default=1, show_default=True, type=int, help="APID for the command packet.")
def send(
    command_args: tuple[str, ...],
    port: int,
    host: str,
    instance_id: str | None,
    def_path: Path | None,
    apid: int,
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

    try:
        client.send_command(host, port, command, args, apid=apid)
    except (ValueError, struct.error) as exc:
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(f"could not reach {host}:{port} — {exc}") from exc

    click.echo(f"sent {command.name} (0x{command.opcode:02X}) args={args or '{}'}")


def _decode_packet(
    packet: bytes, simdef: SimDefinition, wanted: set[str], prefixes: dict[int, str]
):
    """Decode one CCSDS frame -> (apid, name, seq, meta, prefix), or None to skip.

    ``prefixes`` is a per-APID cache of the shared field-name prefix (mutated).
    Skips runt frames (<6 bytes) and, when a filter is active, unwanted packets.
    """
    if len(packet) < 6:  # runt frame from a misbehaving/other-protocol server
        return None
    header = ccsds.CCSDSHeader.unpack(packet[:6])
    packet_def = simdef.packet_by_apid(header.apid)
    name = packet_def.name if packet_def else f"APID_0x{header.apid:X}"
    if wanted and name not in wanted:
        return None
    meta: list = []
    prefix = ""
    if packet_def is not None:
        try:
            values = codec.unpack_telemetry(packet_def, packet[6:])
            meta = [(f.name, values[f.name], f.unit) for f in packet_def.fields]
            prefix = prefixes.setdefault(
                header.apid, render.common_prefix([f.name for f in packet_def.fields])
            )
        except struct.error:
            meta = [("<raw>", packet[6:22].hex(), None)]
    return header.apid, name, header.seq_count, meta, prefix


@main.command()
@click.option("--port", required=True, type=int, help="TCP port of the running sim.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to connect to.")
@click.option("--id", "instance_id", default=None, help="Load def from runs/<id>/cmd_tlm.json.")
@click.option(
    "--def",
    "def_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
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
    help="Stop after N updates — packets in compact/table, frames in dashboard "
    "(0 = run forever).",
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
) -> None:
    """Connect to a running sim and pretty-print decoded live telemetry."""
    simdef = _load_definition(instance_id, def_path)
    wanted = set(packet_filter)
    instance = instance_id or (def_path.stem if def_path else host)
    prefixes: dict[int, str] = {}
    decode = functools.partial(
        _decode_packet, simdef=simdef, wanted=wanted, prefixes=prefixes
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


def _run_stream(host, port, style, decode, show_all_fields, count) -> None:
    """Compact / table styles: render each packet as it arrives."""
    shown = 0
    for packet in client.stream_packets(host, port):
        decoded = decode(packet)
        if decoded is None:
            continue
        apid, name, seq, meta, prefix = decoded
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if style == "table":
            click.echo(render.render_table(ts, apid, name, seq, meta))
        else:
            click.echo(
                render.render_compact(
                    ts, apid, name, seq, meta, prefix, show_all=show_all_fields
                )
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


if __name__ == "__main__":
    main()
