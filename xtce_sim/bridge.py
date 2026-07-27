"""The viewer bridge: NAV telemetry in, state-vector SSE out.

Plays the ground station for an external orbit display (molniya-viewer):
it connects to one or more running sims exactly like ``monitor`` does,
decodes each vehicle's NAV_STATUS packet against its definition, and
republishes every state vector on ONE Server-Sent-Events stream at
``GET /telemetry/stream``. The sims keep speaking pure CCSDS and never
learn a viewer exists; the display side opens a single long-lived HTTP
request and sorts satellites by ``sat_id``. The wire format is the
molniya repo's ``docs/telemetry-sse-contract.md`` — the single source of
truth both projects implement against.

Conventions (opt-in by declaration, like every link convention): a
vehicle is bridgeable when its definition declares NAV_POS_X/Y/Z and
NAV_VEL_X/Y/Z in one telemetry packet, in kilometers and kilometers per
second, ECI axes. If NAV_GPS_VALID exists, only VALID solutions are
forwarded — an invalid GNSS fix must not be published as truth.

Honesty notes, mirrored from the nav model's docstring: the epoch on
each event is wall-clock UTC stamped at decode (the sim has no onboard
calendar), and the frame is reported as J2000, which at this fidelity
(unperturbed circular orbit, fixed sun) is the honest nearest label.

One event per received packet; ``event: bye`` per satellite on clean
shutdown. A ``Last-Event-ID`` from a reconnecting client is acknowledged
in the log and the stream simply continues — every state event is a
complete snapshot, so resume needs no replay.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from xtce_sim import ccsds, codec
from xtce_sim.definition import SimDefinition

log = logging.getLogger("xtce_sim.bridge")

_POS_FIELDS = ("NAV_POS_X", "NAV_POS_Y", "NAV_POS_Z")
_VEL_FIELDS = ("NAV_VEL_X", "NAV_VEL_Y", "NAV_VEL_Z")
_VALID_FIELD = "NAV_GPS_VALID"
_RECONNECT_DELAY_S = 2.0
_CLIENT_QUEUE_MAX = 16


class BridgeConfigError(Exception):
    """The bridge configuration is unusable; the message lists every problem."""


def sse_frame(event: str, event_id: int, data: dict) -> str:
    """One Server-Sent-Events frame, exactly as the contract specifies:
    an ``event:`` name, a monotonic ``id:``, single-line JSON ``data:``,
    and the blank-line terminator."""
    return f"event: {event}\nid: {event_id}\ndata: {json.dumps(data)}\n\n"


@dataclass
class SatelliteFeed:
    """One sim to watch: where it is, what to call it, how to draw it."""

    sat_id: str
    name: str
    host: str
    port: int
    simdef: SimDefinition
    display: dict = field(default_factory=dict)
    apid: int = -1  # the NAV packet's APID, resolved by validate()
    valid_raw: int | None = None  # NAV_GPS_VALID's VALID raw value, if declared

    def validate(self) -> list[str]:
        """Resolve the NAV packet; every problem returned, none raised."""
        problems = []
        needed = set(_POS_FIELDS + _VEL_FIELDS)
        for packet in self.simdef.packets:
            names = {f.name for f in packet.fields}
            if needed <= names:
                self.apid = packet.apid
                valid = next((f for f in packet.fields if f.name == _VALID_FIELD), None)
                if valid is not None and valid.enumerations:
                    raw = valid.enumerations.get("VALID")
                    self.valid_raw = int(raw) if raw is not None else None
                return []
        problems.append(
            f"{self.name}: no telemetry packet declares all of "
            f"{', '.join(sorted(needed))} — the vehicle is not bridgeable"
        )
        return problems


class ViewerBridge:
    """Fan state vectors from every satellite feed out to every SSE client."""

    def __init__(self, feeds: list[SatelliteFeed]) -> None:
        self.feeds = feeds
        self._clients: set[asyncio.Queue] = set()

    # -- fan-out ---------------------------------------------------------------

    def _broadcast(self, event: str, data: dict) -> None:
        for queue in self._clients:
            if queue.full():  # state events supersede: drop the oldest
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait((event, data))

    def _state_event(self, feed: SatelliteFeed, values: dict) -> dict:
        epoch = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        state = {
            "sat_id": feed.sat_id,
            "name": feed.name,
            "epoch": epoch.replace("+00:00", "Z"),
            "frame": "J2000",
            "position_km": {
                "x": values[_POS_FIELDS[0]],
                "y": values[_POS_FIELDS[1]],
                "z": values[_POS_FIELDS[2]],
            },
            "velocity_kms": {
                "vx": values[_VEL_FIELDS[0]],
                "vy": values[_VEL_FIELDS[1]],
                "vz": values[_VEL_FIELDS[2]],
            },
        }
        if feed.display:
            state["display"] = feed.display
        return state

    # -- the downlink side (one task per sim) ----------------------------------

    async def feed_loop(self, feed: SatelliteFeed) -> None:
        """Stay connected to one sim forever, forwarding its NAV packets."""
        while True:
            try:
                reader, writer = await asyncio.open_connection(feed.host, feed.port)
            except OSError:
                await asyncio.sleep(_RECONNECT_DELAY_S)
                continue
            log.info("%s: downlink up (%s:%d)", feed.name, feed.host, feed.port)
            try:
                await self._read_stream(feed, reader)
            except ccsds.FrameError as exc:
                log.warning("%s: framing error: %s — reconnecting", feed.name, exc)
            finally:
                writer.close()
            log.info("%s: downlink down; retrying", feed.name)
            await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _read_stream(self, feed: SatelliteFeed, reader: asyncio.StreamReader) -> None:
        buffer = b""
        while True:
            data = await reader.read(4096)
            if not data:
                return
            packets, buffer = ccsds.deframe(buffer + data)
            for packet in packets:
                state = self.decode(feed, packet)
                if state is not None:
                    self._broadcast("state", state)

    def decode(self, feed: SatelliteFeed, packet: bytes) -> dict | None:
        """One CCSDS packet -> one contract `state` dict, or None to skip."""
        if len(packet) < 6:
            return None
        header = ccsds.CCSDSHeader.unpack(packet[:6])
        if header.apid != feed.apid:
            return None
        packet_def = feed.simdef.packet_by_apid(header.apid)
        try:
            raw = codec.unpack_telemetry(packet_def, packet[6:])
        except Exception:  # torn/short payload must not kill the bridge
            log.warning("%s: undecodable NAV packet; skipped", feed.name)
            return None
        if feed.valid_raw is not None and raw.get(_VALID_FIELD) != feed.valid_raw:
            return None  # an invalid GNSS fix is not truth; don't publish it
        fields = {f.name: f for f in packet_def.fields}
        values = {}
        for name in _POS_FIELDS + _VEL_FIELDS:
            value = raw[name]
            cal = fields[name].calibrator
            values[name] = float(cal.apply(value)) if cal is not None else float(value)
        return self._state_event(feed, values)

    # -- the SSE side ----------------------------------------------------------

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        resume = request.headers.get("Last-Event-ID")
        if resume is not None:
            log.info("client resumed after event %s (state is snapshot; continuing)", resume)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
        self._clients.add(queue)
        event_id = 0
        try:
            while True:
                item = await queue.get()
                if item is None:  # shutdown sentinel, after the byes
                    break
                event, data = item
                event_id += 1
                await response.write(sse_frame(event, event_id, data).encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(queue)
        return response

    async def shutdown(self, _app: web.Application) -> None:
        """Best-effort byes so the display removes our satellites promptly."""
        for feed in self.feeds:
            self._broadcast("bye", {"sat_id": feed.sat_id})
        for queue in self._clients:
            if queue.full():  # same drop-oldest rule; the sentinel must land
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(None)
        await asyncio.sleep(0.1)  # one scheduling turn for the writers


async def run_bridge(
    feeds: list[SatelliteFeed],
    sse_host: str,
    sse_port: int,
    on_ready=None,
) -> None:
    """Serve the SSE stream and keep every satellite feed running.

    ``on_ready`` (optional) is called with the actually-bound port once the
    stream is listening — how a test on an ephemeral port finds the bridge.
    """
    bridge = ViewerBridge(feeds)
    app = web.Application()
    app.router.add_get("/telemetry/stream", bridge.handle_stream)
    app.on_shutdown.append(bridge.shutdown)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, sse_host, sse_port)
    await site.start()
    if on_ready is not None:
        on_ready(runner.addresses[0][1])
    tasks = [asyncio.create_task(bridge.feed_loop(feed)) for feed in bridge.feeds]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_bridge_config(path: Path) -> list[SatelliteFeed]:
    """Parse a bridge TOML: one [[satellites]] entry per running sim.

    Strict and total, like every config in this project: every problem in
    the file is reported in one BridgeConfigError.
    """
    import tomllib

    try:
        body = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BridgeConfigError(f"{path}: {exc}") from exc
    entries = body.get("satellites")
    problems: list[str] = []
    for key in sorted(set(body) - {"satellites"}):
        problems.append(f"unknown table {key!r}")
    if not isinstance(entries, list) or not entries:
        raise BridgeConfigError(
            f"{path}: needs at least one [[satellites]] entry"
            + ("".join(f"\n  - {p}" for p in problems))
        )
    feeds = []
    seen: dict[str, int] = {}
    for n, entry in enumerate(entries, 1):
        feed = _parse_satellite(entry, n, problems.append)
        if feed is None:
            continue
        if feed.sat_id in seen:
            problems.append(
                f"satellites #{n}: sat_id {feed.sat_id!r} already used by entry "
                f"#{seen[feed.sat_id]} — identities must be unique"
            )
            continue
        seen[feed.sat_id] = n
        feeds.append(feed)
    for feed in feeds:
        problems.extend(feed.validate())
    if problems:
        raise BridgeConfigError(
            f"{path}: {len(problems)} problem(s):" + "".join(f"\n  - {p}" for p in problems)
        )
    return feeds


def _parse_satellite(entry, n: int, err) -> SatelliteFeed | None:
    where = f"satellites #{n}"
    if not isinstance(entry, dict):
        err(f"{where}: must be a table")
        return None
    known = {"sat_id", "name", "host", "port", "def", "color", "pixel_size", "fov_half_angle_deg"}
    for key in sorted(set(entry) - known):
        err(f"{where}: unknown key {key!r}")
    sat_id = entry.get("sat_id")
    if not isinstance(sat_id, str) or not sat_id:
        err(f"{where}: sat_id must be a non-empty string")
        return None
    port = entry.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        err(f"{where}: port must be an integer between 1 and 65535")
        return None
    def_path = entry.get("def")
    if not isinstance(def_path, str):
        err(f"{where}: def must be a path to an XTCE .xml or cmd_tlm.json")
        return None
    try:
        simdef = load_definition_file(Path(def_path))
    except Exception as exc:
        err(f"{where}: def {def_path!r}: {exc}")
        return None
    return SatelliteFeed(
        sat_id=sat_id,
        name=str(entry.get("name", sat_id)),
        host=str(entry.get("host", "127.0.0.1")),
        port=port,
        simdef=simdef,
        display=_parse_display(entry, where, err),
    )


def _parse_display(entry: dict, where: str, err) -> dict:
    """The presentation hints: color, pixel size, sensor-cone half-angle."""
    display = {}
    color = entry.get("color")
    if color is not None:
        display["color"] = str(color)
    pixel_size = entry.get("pixel_size")
    if pixel_size is not None:
        if isinstance(pixel_size, bool) or not isinstance(pixel_size, int) or pixel_size < 1:
            err(f"{where}: pixel_size must be a positive integer")
        else:
            display["pixel_size"] = pixel_size
    fov = entry.get("fov_half_angle_deg")
    if fov is not None:
        if isinstance(fov, bool) or not isinstance(fov, (int, float)) or not 0.0 < fov <= 90.0:
            err(f"{where}: fov_half_angle_deg must be a number in (0, 90]")
        else:
            display["fov_half_angle_deg"] = float(fov)
    return display


def load_definition_file(path: Path) -> SimDefinition:
    """A definition from an XTCE .xml or an emitted cmd_tlm.json."""
    if path.suffix.lower() == ".json":
        return SimDefinition.from_json(path)
    return SimDefinition.from_xtce(path)
