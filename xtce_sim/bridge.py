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

Beside those hero sims, the roster's ``[[constellations]]`` entries add
population satellites (see ``xtce_sim.constellation``): orbits
propagated inside this process and published on the same stream, one
satellite at a time, spread evenly across each constellation's update
period so a large catalog never bursts the fan-out. The per-client queue
is sized from the whole fleet for the same reason.

Honesty notes, mirrored from the nav model's docstring: the epoch on
each event is wall-clock UTC stamped at decode (the sim has no onboard
calendar), and the frame is reported as J2000, which at this fidelity
(unperturbed Keplerian orbit, fixed sun) is the honest nearest label.

One event per received packet; ``event: bye`` per satellite on clean
shutdown. A ``Last-Event-ID`` from a reconnecting client is acknowledged
in the log and the stream simply continues — every state event is a
complete snapshot, so resume needs no replay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from xtce_sim import ccsds, codec
from xtce_sim.constellation import (
    Constellation,
    parse_constellation,
    parse_display,
    state_km,
)
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


def contract_state(
    sat_id: str, name: str, position_km: dict, velocity_kms: dict, display: dict
) -> dict:
    """One contract ``state`` event, epoch-stamped now — the one shape
    every publisher (hero feed or population catalog) emits."""
    epoch = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    state = {
        "sat_id": sat_id,
        "name": name,
        "epoch": epoch.replace("+00:00", "Z"),
        "frame": "J2000",
        "position_km": position_km,
        "velocity_kms": velocity_kms,
    }
    if display:
        state["display"] = display
    return state


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
    """Fan state vectors from every publisher out to every SSE client."""

    def __init__(
        self,
        feeds: list[SatelliteFeed],
        constellations: list[Constellation] | tuple[Constellation, ...] = (),
    ) -> None:
        self.feeds = feeds
        self.constellations = list(constellations)
        self._clients: set[asyncio.Queue] = set()
        # A slow client sheds oldest-first, but the queue must at least
        # hold a full fleet of snapshots or satellites vanish wholesale.
        fleet = len(feeds) + sum(len(c.sats) for c in self.constellations)
        self._queue_max = max(_CLIENT_QUEUE_MAX, 2 * fleet)

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
        return contract_state(
            feed.sat_id,
            feed.name,
            {
                "x": values[_POS_FIELDS[0]],
                "y": values[_POS_FIELDS[1]],
                "z": values[_POS_FIELDS[2]],
            },
            {
                "vx": values[_VEL_FIELDS[0]],
                "vy": values[_VEL_FIELDS[1]],
                "vz": values[_VEL_FIELDS[2]],
            },
            feed.display,
        )

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

    # -- the catalog side (one task per constellation) -------------------------

    async def population_loop(self, constellation: Constellation) -> None:
        """Propagate one constellation forever, one satellite at a time.

        Updates are spread evenly across the constellation's period
        instead of bursting at its top — a real catalog refreshes object
        by object as observations arrive, and a burst of a thousand
        events would overflow every client queue at once. t = 0 is the
        moment this loop starts, wall clock, matching the epoch stamped
        on each event."""
        interval = constellation.update_period_s / len(constellation.sats)
        start = time.time()
        while True:
            for sat in constellation.sats:
                position_km, velocity_kms = state_km(sat, time.time() - start)
                self._broadcast(
                    "state",
                    contract_state(sat.sat_id, sat.name, position_km, velocity_kms, sat.display),
                )
                await asyncio.sleep(interval)

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
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_max)
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
        for constellation in self.constellations:
            for sat in constellation.sats:
                self._broadcast("bye", {"sat_id": sat.sat_id})
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
    constellations: list[Constellation] | tuple[Constellation, ...] = (),
) -> None:
    """Serve the SSE stream and keep every publisher running: one task
    per hero feed, one task per constellation.

    ``on_ready`` (optional) is called with the actually-bound port once the
    stream is listening — how a test on an ephemeral port finds the bridge.
    """
    bridge = ViewerBridge(feeds, constellations)
    app = web.Application()
    app.router.add_get("/telemetry/stream", bridge.handle_stream)
    app.on_shutdown.append(bridge.shutdown)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, sse_host, sse_port)
    await site.start()
    if on_ready is not None:
        on_ready(runner.addresses[0][1])
    loops = [bridge.feed_loop(feed) for feed in bridge.feeds] + [
        bridge.population_loop(c) for c in bridge.constellations
    ]
    tasks = [asyncio.create_task(loop) for loop in loops]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class BridgeRoster:
    """Everything a bridge config names: hero feeds and populations."""

    feeds: list[SatelliteFeed]
    constellations: list[Constellation]


def load_bridge_config(path: Path) -> BridgeRoster:
    """Parse a bridge TOML: one [[satellites]] entry per running sim,
    one [[constellations]] entry per propagated population.

    Strict and total, like every config in this project: every problem in
    the file is reported in one BridgeConfigError. Identities (sat_ids)
    must be unique across everything the roster names.
    """
    import tomllib

    try:
        body = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BridgeConfigError(f"{path}: {exc}") from exc
    problems: list[str] = []
    for key in sorted(set(body) - {"satellites", "constellations"}):
        problems.append(f"unknown table {key!r}")
    sat_entries = _entry_list(body, "satellites", problems.append)
    con_entries = _entry_list(body, "constellations", problems.append)
    if not sat_entries and not con_entries:
        raise BridgeConfigError(
            f"{path}: needs at least one [[satellites]] or [[constellations]] entry"
            + ("".join(f"\n  - {p}" for p in problems))
        )
    seen: dict[str, str] = {}
    feeds = _collect_satellites(sat_entries, seen, problems)
    constellations = _collect_constellations(con_entries, seen, problems)
    for feed in feeds:
        problems.extend(feed.validate())
    if problems:
        raise BridgeConfigError(
            f"{path}: {len(problems)} problem(s):" + "".join(f"\n  - {p}" for p in problems)
        )
    return BridgeRoster(feeds=feeds, constellations=constellations)


def _entry_list(body: dict, key: str, err) -> list:
    """An optional array-of-tables; absent is an empty list, not an error."""
    entries = body.get(key, [])
    if not isinstance(entries, list):
        err(f"{key} must be an array of tables ([[{key}]])")
        return []
    return entries


def _collect_satellites(entries: list, seen: dict[str, str], problems: list[str]) -> list[SatelliteFeed]:
    feeds = []
    for n, entry in enumerate(entries, 1):
        feed = _parse_satellite(entry, n, problems.append)
        if feed is None:
            continue
        where = f"satellites #{n}"
        if feed.sat_id in seen:
            problems.append(
                f"{where}: sat_id {feed.sat_id!r} already used by "
                f"{seen[feed.sat_id]} — identities must be unique"
            )
            continue
        seen[feed.sat_id] = where
        feeds.append(feed)
    return feeds


def _collect_constellations(
    entries: list, seen: dict[str, str], problems: list[str]
) -> list[Constellation]:
    constellations = []
    for n, entry in enumerate(entries, 1):
        constellation = parse_constellation(entry, n, problems.append)
        if constellation is None:
            continue
        where = f"constellations #{n}"
        clash = next((s.sat_id for s in constellation.sats if s.sat_id in seen), None)
        if clash is not None:
            problems.append(
                f"{where}: sat_id {clash!r} already used by "
                f"{seen[clash]} — identities must be unique"
            )
            continue
        for sat in constellation.sats:
            seen[sat.sat_id] = where
        constellations.append(constellation)
    return constellations


def _parse_satellite(entry, n: int, err) -> SatelliteFeed | None:
    where = f"satellites #{n}"
    if not isinstance(entry, dict):
        err(f"{where}: must be a table")
        return None
    known = {
        "sat_id", "name", "host", "port", "def",
        "color", "pixel_size", "fov_half_angle_deg", "cone_enabled",
    }
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
        display=parse_display(entry, where, err),
    )


def load_definition_file(path: Path) -> SimDefinition:
    """A definition from an XTCE .xml or an emitted cmd_tlm.json."""
    if path.suffix.lower() == ".json":
        return SimDefinition.from_json(path)
    return SimDefinition.from_xtce(path)
