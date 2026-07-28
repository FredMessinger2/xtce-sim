"""The viewer bridge: contract conformance, config validation, and the wire."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import aiohttp
import pytest
from conftest import DATA, EXAMPLES, IMAGING

from xtce_sim import ccsds, codec
from xtce_sim.bridge import (
    BridgeConfigError,
    SatelliteFeed,
    ViewerBridge,
    load_bridge_config,
    run_bridge,
    sse_frame,
)
from xtce_sim.definition import SimDefinition

_NAV_VALUES = {
    "NAV_TIMESTAMP": 600,
    "NAV_POS_X": 6871.0,
    "NAV_POS_Y": 123.25,
    "NAV_POS_Z": -45.5,
    "NAV_VEL_X": 0.0,
    "NAV_VEL_Y": 4.5,
    "NAV_VEL_Z": 6.0,
    "NAV_GPS_VALID": 1,
}


def _feed(simdef, **overrides) -> SatelliteFeed:
    feed = SatelliteFeed(
        sat_id=overrides.pop("sat_id", "90001"),
        name=overrides.pop("name", "IMAGING-SAT-1"),
        host="127.0.0.1",
        port=overrides.pop("port", 5001),
        simdef=simdef,
        **overrides,
    )
    assert feed.validate() == []
    return feed


def _nav_packet(simdef, **overrides) -> bytes:
    packet_def = simdef.packet_by_name("NAV_STATUS")
    payload = codec.pack_telemetry(packet_def, {**_NAV_VALUES, **overrides})
    return ccsds.build_telemetry_packet(packet_def.apid, payload)


# ---- decode: the contract's state event -------------------------------------


def test_decode_conforms_to_the_contract(simdef):
    feed = _feed(simdef, display={"color": "#ffcc00"})
    bridge = ViewerBridge([feed])
    state = bridge.decode(feed, _nav_packet(simdef))
    assert state is not None
    assert set(state) == {
        "sat_id", "name", "epoch", "frame", "position_km", "velocity_kms", "display",
    }
    assert state["sat_id"] == "90001" and state["name"] == "IMAGING-SAT-1"
    assert state["frame"] == "J2000"
    assert state["position_km"] == {
        "x": pytest.approx(6871.0),
        "y": pytest.approx(123.25),
        "z": pytest.approx(-45.5),
    }
    assert state["velocity_kms"] == {
        "vx": pytest.approx(0.0),
        "vy": pytest.approx(4.5),
        "vz": pytest.approx(6.0),
    }
    assert state["display"] == {"color": "#ffcc00"}
    # ISO-8601 UTC with the contract's Z suffix, and actually parseable.
    assert state["epoch"].endswith("Z")
    datetime.fromisoformat(state["epoch"].replace("Z", "+00:00"))


def test_decode_skips_other_packets_and_invalid_fixes(simdef):
    feed = _feed(simdef)
    bridge = ViewerBridge([feed])
    power = simdef.packet_by_name("POWER_STATUS")
    power_packet = ccsds.build_telemetry_packet(power.apid, codec.pack_telemetry(power, {}))
    assert bridge.decode(feed, power_packet) is None
    # An invalid GNSS fix must not be published as truth.
    assert bridge.decode(feed, _nav_packet(simdef, NAV_GPS_VALID=0)) is None
    assert bridge.decode(feed, _nav_packet(simdef, NAV_GPS_VALID=1)) is not None


def test_validate_refuses_a_vehicle_without_nav_fields():
    other = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    feed = SatelliteFeed(
        sat_id="1", name="MY-VEHICLE", host="127.0.0.1", port=5002, simdef=other
    )
    problems = feed.validate()
    assert len(problems) == 1 and "not bridgeable" in problems[0]


def test_sse_frame_shape():
    frame = sse_frame("state", 7, {"sat_id": "90001"})
    lines = frame.split("\n")
    assert lines[0] == "event: state"
    assert lines[1] == "id: 7"
    assert lines[2] == 'data: {"sat_id": "90001"}'
    assert frame.endswith("\n\n") and "\n" not in lines[2]


# ---- configuration ----------------------------------------------------------


def _write_config(tmp_path, text: str) -> Path:
    path = tmp_path / "bridge.toml"
    path.write_text(text)
    return path


def test_config_loads_a_roster(tmp_path):
    path = _write_config(
        tmp_path,
        f"""
[[satellites]]
sat_id = "90001"
name = "IMAGING-SAT-1"
port = 5001
def = "{IMAGING}"
color = "#ffcc00"
pixel_size = 8
fov_half_angle_deg = 7.95

[[satellites]]
sat_id = "90002"
port = 5002
def = "{IMAGING}"
""",
    )
    roster = load_bridge_config(path)
    feeds = roster.feeds
    assert [f.sat_id for f in feeds] == ["90001", "90002"]
    assert feeds[0].display == {"color": "#ffcc00", "pixel_size": 8, "fov_half_angle_deg": 7.95}
    assert feeds[1].name == "90002" and feeds[1].display == {}
    assert feeds[0].apid == feeds[1].apid == 29
    assert roster.constellations == []


def test_config_rejects_a_bad_cone(tmp_path):
    for bad in ("0.0", "91.0", "true", '"wide"'):
        path = _write_config(
            tmp_path,
            f"""
[[satellites]]
sat_id = "90001"
port = 5001
def = "{IMAGING}"
fov_half_angle_deg = {bad}
""",
        )
        with pytest.raises(BridgeConfigError, match="fov_half_angle_deg"):
            load_bridge_config(path)


def test_config_reports_every_problem_at_once(tmp_path):
    path = _write_config(
        tmp_path,
        f"""
[[satellites]]
sat_id = "90001"
port = 5001
def = "{IMAGING}"
warp = 9

[[satellites]]
sat_id = "90001"
port = 5002
def = "{IMAGING}"

[[satellites]]
sat_id = "90003"
port = 99999
def = "{IMAGING}"

[[satellites]]
sat_id = "90004"
port = 5004
def = "/no/such/definition.xml"
""",
    )
    with pytest.raises(BridgeConfigError) as excinfo:
        load_bridge_config(path)
    message = str(excinfo.value)
    assert "unknown key 'warp'" in message
    assert "already used by satellites #1" in message
    assert "port must be an integer between 1 and 65535" in message
    assert "/no/such/definition.xml" in message


def test_config_needs_at_least_one_satellite(tmp_path):
    path = _write_config(tmp_path, "# empty\n")
    with pytest.raises(BridgeConfigError, match="at least one"):
        load_bridge_config(path)


# ---- the wire: a real sim, the real stream ----------------------------------


async def test_stream_carries_live_state_vectors(simdef):
    from xtce_sim import behavior
    from xtce_sim.behavior.loader import load_behavior
    from xtce_sim.server import SimServer

    spec = load_behavior(EXAMPLES / "imaging_sat", simdef)
    server = SimServer(
        simdef,
        host="127.0.0.1",
        port=0,
        beacon_interval=0.05,
        behavior_engine=behavior.BehaviorEngine(spec, simdef),
    )
    await server.start()
    feed = _feed(simdef, port=server.bound_port)
    ready: asyncio.Future = asyncio.get_running_loop().create_future()
    bridge_task = asyncio.create_task(
        run_bridge([feed], "127.0.0.1", 0, on_ready=ready.set_result)
    )
    try:
        sse_port = await asyncio.wait_for(ready, timeout=5.0)
        # The whole loop, end to end: sim beacons CCSDS, the bridge's feed
        # decodes, and this HTTP client reads the served SSE stream.
        async with asyncio.timeout(10.0):
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{sse_port}/telemetry/stream"
                ) as response:
                    assert response.headers["Content-Type"].startswith("text/event-stream")
                    frame: dict[str, str] = {}
                    async for line_bytes in response.content:
                        line = line_bytes.decode().rstrip("\n")
                        if line:
                            key, _, value = line.partition(": ")
                            frame[key] = value
                            continue
                        if frame.get("event") == "state":
                            break
                        frame = {}
        assert frame["id"] == "1"
        state = json.loads(frame["data"])
        assert state["sat_id"] == "90001" and state["frame"] == "J2000"
        radius_km = spec.environment.orbit.semi_major / 1000.0
        magnitude = (
            state["position_km"]["x"] ** 2
            + state["position_km"]["y"] ** 2
            + state["position_km"]["z"] ** 2
        ) ** 0.5
        assert magnitude == pytest.approx(radius_km, rel=1e-4)
    finally:
        bridge_task.cancel()
        await server.stop()
