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
                                whether a calibrator applies), the command
                                table, and the event-only APIDs (packets
                                that downlink on events, never on the
                                beacon), so the page builds itself from
                                the definition instead of hardcoding.
  {"type": "telemetry", ...}    one per decoded packet: name, APID,
                                sequence count, bridge arrival time, and
                                per-field raw counts + engineering value
                                + enum label.
  {"type": "command", ...}      one per command echo (see ccsds.py):
                                name, opcode, decoded arguments (enum
                                labels as commanded), and execution
                                status.
  {"type": "link", "up": ...}   whenever the bridge's connection to the
                                sim comes up or drops (it retries forever).

Message protocol (browser -> bridge):

  {"type": "send", "id": N, "name": CMD, "args": {K: V, ...}}
                                one command to uplink. The bridge is the
                                ground station: it validates and encodes
                                exactly as ``xtce-sim send`` does, and a
                                command that fails ground validation is
                                REFUSED here — answered with a
                                {"type": "send_result", "id": N, "ok":
                                false, "error": ...} to the asking browser
                                only, and never transmitted. A command
                                that encodes cleanly is framed onto the
                                bridge's live sim connection; its outcome
                                arrives as the command echo every console
                                sees, so an accepted send gets its
                                send_result (ok: true) AND a command
                                message.

The browser page itself is served at ``/`` from ``static/ui.html``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
from datetime import datetime
from importlib import resources

from aiohttp import WSMsgType, web
from yarl import URL

from xtce_sim import ccsds, codec, fileservice
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
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
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
                "significance": c.significance,
                "significance_reason": c.significance_reason,
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
        # Packets that downlink on events rather than the beacon (the file
        # service's receipt, today): quiet is NORMAL for these, and the
        # page must not dim their last event as stale.
        "event_only": sorted(fileservice.event_only_apids(simdef)),
    }


def _field_values(field: FieldInfo, raw) -> dict:
    """raw counts + engineering value + enum label for one decoded field."""
    out: dict = {"raw": _json_safe(raw)}
    eu = raw
    if field.python_type == "string" and isinstance(raw, (bytes, bytearray)):
        # A string field's engineering value is its text (NUL padding
        # stripped); the raw view keeps the hex. Without this the console
        # shows a filename as a hex blob.
        eu = bytes(raw).split(b"\x00", 1)[0].decode("utf-8", "replace")
    elif (
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


def command_message(simdef: SimDefinition, packet: bytes) -> dict:
    """Decode a command-echo packet into the browser's command-log entry.

    The echo carries the original command packet verbatim (see ccsds.py);
    decoding it against the command definitions recovers the name and every
    argument. Enum arguments are shown as their labels, matching how they
    were commanded.
    """
    status, cmd_packet = ccsds.parse_command_echo(packet)
    status_name = "invalid_echo" if status is None else (
        ccsds.ECHO_STATUS_NAMES.get(status, f"status_{status}")
    )
    message: dict = {
        "type": "command",
        "time": datetime.now().isoformat(timespec="milliseconds"),
        "status": status_name,
    }
    opcode, payload = ccsds.parse_command_packet(cmd_packet)
    if opcode is None:
        message["name"] = "<undecodable>"
        message["raw"] = cmd_packet.hex()
        return message
    message["opcode"] = opcode
    command = simdef.command_by_opcode(opcode)
    if command is None:
        message["name"] = f"OPCODE_0x{opcode:02X}"
        message["raw"] = payload.hex()
        return message
    message["name"] = command.name
    try:
        args = codec.decode_command(command, payload)
    except Exception:
        message["raw"] = payload.hex()
        return message
    # decode_command owns enum labeling: matched enum values arrive here
    # already as their label strings, so only JSON safety is left to do.
    message["args"] = {
        p.name: _json_safe(args.get(p.name)) for p in command.params
    }
    return message


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
        # The live uplink half of the sim connection (None while down):
        # commands from the page go up the SAME link telemetry comes down,
        # exactly as a real ground station holds one bidirectional link.
        self._sim_writer: asyncio.StreamWriter | None = None

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
            self._sim_writer = writer
            self._broadcast({"type": "link", "up": True})
            try:
                await self._read_stream(reader)
            except ccsds.FrameError as exc:
                log.warning("downlink framing error: %s — reconnecting", exc)
            finally:
                # Only the close belongs in finally: a broadcast or sleep here
                # would run to completion on cancellation and hang Ctrl-C.
                self._sim_writer = None
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
                message = self._decode(packet)
                if message is not None:
                    self._broadcast(message)

    def _decode(self, packet: bytes) -> dict | None:
        """Route one downlink packet: command echoes to the command log,
        everything else to the telemetry panels."""
        if len(packet) >= 6:
            header = ccsds.CCSDSHeader.unpack(packet[:6])
            if header.apid == ccsds.CMD_ECHO_APID:
                return command_message(self.simdef, packet)
        return telemetry_message(self.simdef, packet)

    # -- uplink to the sim ----------------------------------------------------

    async def uplink(self, name, args) -> tuple[bool, str]:
        """Validate, encode, and transmit one command; (ok, message).

        The ground does not trust the page: the command must exist, every
        argument must coerce and encode, and declared ValidRanges are
        enforced — the same strict-uplink stance as ``xtce-sim send``. A
        refusal here never touches the wire. TypeError joins the catch
        because JSON can deliver null/arrays/objects where the codec
        expects a number, and one bad value must cost a refusal, not the
        socket.
        """
        command = self.simdef.command_by_name(str(name))
        if command is None:
            return False, f"unknown command {name!r}"
        if not isinstance(args, dict):
            return False, "args must be an object of NAME=VALUE pairs"
        try:
            payload = codec.encode_command(command, args)
        except (ValueError, TypeError, OverflowError, struct.error) as exc:
            return False, str(exc)
        writer = self._sim_writer
        if writer is None or writer.is_closing():
            return False, "sim link is down — nothing was transmitted"
        try:
            writer.write(ccsds.frame(ccsds.build_command_packet(command.opcode, payload)))
            await writer.drain()
        except OSError as exc:
            return False, f"sim link failed mid-send: {exc}"
        if writer.is_closing():
            # The link died under the send: a closing transport discards
            # writes without raising, so honesty requires saying the
            # command may never have left the ground.
            return False, "sim link dropped during the send — the command may not have left"
        return True, "transmitted"

    async def _handle_send(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """One page-originated command; the verdict goes to the asker only."""
        # Only an ABSENT args defaults to {}: a falsy non-dict ([], 0, "")
        # is a malformed request and must reach uplink's refusal, not be
        # silently promoted into "send with every argument zero".
        args = msg.get("args")
        if args is None:
            args = {}
        ok, detail = await self.uplink(msg.get("name"), args)
        reply: dict = {
            "type": "send_result",
            "id": msg.get("id"),
            "name": str(msg.get("name")),
            "ok": ok,
        }
        if not ok:
            reply["error"] = detail
            log.warning("refused page command %r: %s", msg.get("name"), detail)
        entry = self.clients.get(ws)
        if entry is not None:
            try:
                entry[0].put_nowait(json.dumps(reply))
            except asyncio.QueueFull:
                pass  # the stalled-client rule already governs this browser

    # -- HTTP / WebSocket handlers -------------------------------------------

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        if not _origin_allowed(request):
            # Browsers do NOT enforce same-origin on WebSockets: without
            # this check, any web page open in the operator's browser could
            # connect to the localhost bridge and command the vehicle. A
            # browser always sends Origin on a WS handshake; it must match
            # the host this console was served from. Non-browser clients
            # (tests, scripts) send no Origin and pass.
            log.warning(
                "rejected WS from foreign origin %r", request.headers.get("Origin")
            )
            raise web.HTTPForbidden(text="cross-origin WebSocket refused")
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
                if msg.type != WSMsgType.TEXT:
                    continue
                # Inbound is untrusted text off a socket: malformed JSON or
                # an unknown shape is ignored, never fatal to the console.
                try:
                    inbound = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(inbound, dict) and inbound.get("type") == "send":
                    await self._handle_send(ws, inbound)
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


def _origin_allowed(request: web.Request) -> bool:
    """Whether a WS handshake's Origin is this console's own page.

    Absent Origin (non-browser client) is allowed; a present one must name
    exactly the host:port the request was addressed to.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    try:
        parsed = URL(origin)
        origin_host = parsed.host or ""
        origin_port = parsed.explicit_port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    host, _, port_text = request.host.partition(":")
    port = int(port_text) if port_text else 80
    return origin_host == host and origin_port == port


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
