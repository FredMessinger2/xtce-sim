"""
Ground-station bridge: CCSDS telemetry in, WebSocket JSON out.

The sim server plays the spacecraft and speaks only framed CCSDS packets
over TCP (see ``xtce_sim.ccsds``). This module plays the ground station:
it connects to the sim's TCP port exactly like ``monitor`` does, decodes
each packet against the XTCE definition, and re-publishes it as JSON to
every browser connected over WebSocket. The sim server never learns about
JSON or WebSocket — the spacecraft/ground boundary stays where it is in
real systems.

Message protocol (bridge -> browser):

  {"type": "definition", ...}   once, on connect — every packet's name,
                                APID, and field list (units, enum labels,
                                whether a calibrator applies), plus the
                                command table, so the page builds itself
                                from the definition instead of hardcoding.
  {"type": "telemetry", ...}    one per decoded packet: name, APID,
                                sequence count, bridge arrival time, and
                                per-field raw counts + engineering value
                                + enum label.
  {"type": "link", "up": ...}   whenever the bridge's connection to the
                                sim comes up or drops (it retries forever).

The browser page itself is served at ``/`` from ``static/ui.html``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime
from importlib import resources

from aiohttp import WSMsgType, web

from xtce_sim import ccsds, codec
from xtce_sim.definition import FieldInfo, SimDefinition

log = logging.getLogger("xtce_sim.webui")

_RECONNECT_DELAY_S = 2.0


# ---------------------------------------------------------------------------
# JSON views of the definition and of decoded packets
# ---------------------------------------------------------------------------


def _json_safe(value):
    """A value the browser's JSON parser will accept.

    Python's json module happily emits NaN/Infinity, which are invalid JSON
    and kill ``JSON.parse`` in the browser; a spline calibrator can produce
    NaN by design, so map non-finite floats to null.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    return value


def _field_json(field: FieldInfo) -> dict:
    return {
        "name": field.name,
        "type": field.python_type,
        "unit": field.unit,
        "enumerations": field.enumerations or None,
        "calibrated": field.calibrator is not None,
    }


def definition_message(simdef: SimDefinition) -> dict:
    """The one-time hello: everything the page needs to build its panels."""
    return {
        "type": "definition",
        "system": simdef.space_system_name,
        "packets": [
            {
                "name": p.name,
                "apid": p.apid,
                "description": p.description,
                "fields": [_field_json(f) for f in p.fields],
            }
            for p in simdef.packets
        ],
        "commands": [
            {
                "name": c.name,
                "opcode": c.opcode,
                "description": c.description,
                "params": [
                    {
                        "name": a.name,
                        "type": a.python_type,
                        "unit": a.unit,
                        "enumerations": a.enumerations or None,
                        "min": _json_safe(a.valid_min),
                        "max": _json_safe(a.valid_max),
                    }
                    for a in c.params
                ],
            }
            for c in simdef.commands
        ],
    }


def _field_values(field: FieldInfo, raw) -> dict:
    """raw counts + engineering value + enum label for one decoded field."""
    out: dict = {"raw": _json_safe(raw)}
    eu = raw
    if (
        field.calibrator is not None
        and isinstance(raw, (int, float))
        and not isinstance(raw, bool)
    ):
        eu = field.calibrator.apply(raw)
    out["eu"] = _json_safe(eu)
    if field.enumerations:
        out["label"] = next(
            (k for k, v in field.enumerations.items() if v == raw), None
        )
    return out


def telemetry_message(simdef: SimDefinition, packet: bytes) -> dict | None:
    """Decode one CCSDS packet into the JSON message, or None to skip it."""
    if len(packet) < 6:  # runt frame
        return None
    header = ccsds.CCSDSHeader.unpack(packet[:6])
    packet_def = simdef.packet_by_apid(header.apid)
    message = {
        "type": "telemetry",
        "apid": header.apid,
        "seq": header.seq_count,
        "time": datetime.now().isoformat(timespec="milliseconds"),
    }
    if packet_def is None:
        message["packet"] = f"APID_0x{header.apid:X}"
        message["undecoded"] = packet[6:22].hex()
        return message
    message["packet"] = packet_def.name
    try:
        values = codec.unpack_telemetry(packet_def, packet[6:])
        # Calibration lives inside the guard too: an extreme wire value can
        # overflow a polynomial (raw**e), and one poisoned packet must not
        # kill the bridge — degrade it and keep streaming.
        message["fields"] = {
            f.name: _field_values(f, values[f.name]) for f in packet_def.fields
        }
    except Exception:  # torn/short payload or calibration blow-up
        message.pop("fields", None)
        message["undecoded"] = packet[6:22].hex()
    return message


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class Bridge:
    """Fan decoded telemetry out to every connected WebSocket client.

    Same backpressure discipline as SimServer: each browser gets a bounded
    queue drained by its own writer task, and `_broadcast` never awaits a
    send — so one stalled browser (closed laptop lid, dropped wifi) can
    fill its own queue and get dropped, instead of blocking the downlink
    read loop and freezing every other console.
    """

    _QUEUE_MAX = 256  # packets buffered per browser before it's dropped

    def __init__(self, simdef: SimDefinition, sim_host: str, sim_port: int):
        self.simdef = simdef
        self.sim_host = sim_host
        self.sim_port = sim_port
        # ws -> (queue, writer task)
        self.clients: dict[web.WebSocketResponse, tuple[asyncio.Queue, asyncio.Task]] = {}
        self.link_up = False

    # -- broadcast ----------------------------------------------------------

    def _broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(message)
        stalled = []
        for ws, (queue, _task) in self.clients.items():
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                stalled.append(ws)
        for ws in stalled:
            # Browser can't keep up — drop it rather than stall the fleet.
            log.info("browser can't keep up (queue full), dropping")
            _queue, task = self.clients.pop(ws)
            task.cancel()

    async def _client_writer(self, ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
        """Drain one browser's queue; a send failure ends the task."""
        try:
            while True:
                await ws.send_str(await queue.get())
        except (ConnectionError, RuntimeError):
            self.clients.pop(ws, None)

    # -- downlink from the sim ----------------------------------------------

    async def downlink_loop(self) -> None:
        """Stay connected to the sim forever, decoding and re-publishing.

        A dropped or refused connection flips the link indicator and retries
        every couple of seconds, so the sim can restart under a live UI.
        """
        while True:
            try:
                reader, writer = await asyncio.open_connection(
                    self.sim_host, self.sim_port
                )
            except OSError:
                if self.link_up:
                    self.link_up = False
                    self._broadcast({"type": "link", "up": False})
                await asyncio.sleep(_RECONNECT_DELAY_S)
                continue
            log.info("downlink up: %s:%d", self.sim_host, self.sim_port)
            self.link_up = True
            self._broadcast({"type": "link", "up": True})
            try:
                await self._read_stream(reader)
            except ccsds.FrameError as exc:
                log.warning("downlink framing error: %s — reconnecting", exc)
            finally:
                # Only the close belongs in finally: a broadcast or sleep here
                # would run to completion on cancellation and hang Ctrl-C.
                writer.close()
            self.link_up = False
            self._broadcast({"type": "link", "up": False})
            log.info("downlink down; retrying")
            await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _read_stream(self, reader: asyncio.StreamReader) -> None:
        buffer = b""
        while True:
            data = await reader.read(4096)
            if not data:
                return
            packets, buffer = ccsds.deframe(buffer + data)
            for packet in packets:
                message = telemetry_message(self.simdef, packet)
                if message is not None:
                    self._broadcast(message)

    # -- HTTP / WebSocket handlers -------------------------------------------

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        # The hello goes direct: the socket is fresh, so these cannot stall.
        await ws.send_str(json.dumps(definition_message(self.simdef)))
        await ws.send_str(json.dumps({"type": "link", "up": self.link_up}))
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_MAX)
        task = asyncio.create_task(self._client_writer(ws, queue))
        self.clients[ws] = (queue, task)
        log.info("browser connected (%d total)", len(self.clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                # Uplink from the page is phase 2; ignore inbound for now.
        finally:
            self.clients.pop(ws, None)
            task.cancel()
            log.info("browser disconnected (%d total)", len(self.clients))
        return ws

    async def handle_index(self, _request: web.Request) -> web.Response:
        page = await asyncio.to_thread(
            resources.files("xtce_sim").joinpath("static/ui.html").read_text
        )
        return web.Response(text=page, content_type="text/html")


async def run_ui(
    simdef: SimDefinition,
    sim_host: str,
    sim_port: int,
    http_host: str,
    http_port: int,
) -> None:
    """Serve the console page and bridge sim telemetry to it until cancelled."""
    bridge = Bridge(simdef, sim_host, sim_port)
    app = web.Application()
    app.router.add_get("/", bridge.handle_index)
    app.router.add_get("/ws", bridge.handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, http_host, http_port)
    await site.start()
    log.info("console at http://%s:%d/ (sim %s:%d)", http_host, http_port, sim_host, sim_port)

    try:
        await bridge.downlink_loop()
    finally:
        await runner.cleanup()
