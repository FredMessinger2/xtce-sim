"""Population satellites: spreading, identity, config, and the wire."""

import asyncio
import json
import math

import aiohttp
import pytest
from conftest import IMAGING

from xtce_sim.bridge import (
    BridgeConfigError,
    ViewerBridge,
    load_bridge_config,
    run_bridge,
)
from xtce_sim.constellation import parse_constellation, state_km
from xtce_sim.dynamics.environment import R_EARTH


def _parse(entry):
    problems = []
    constellation = parse_constellation(entry, 1, problems.append)
    return constellation, problems


def _walker(**overrides):
    entry = {
        "name_prefix": "COSMOS",
        "count": 8,
        "planes": 4,
        "altitude_km": 800.0,
        "inclination_deg": 63.4,
        **overrides,
    }
    constellation, problems = _parse(entry)
    assert problems == []
    return constellation


# ---- spreading and identity -------------------------------------------------


def test_planes_and_phases_are_spaced_evenly():
    constellation = _walker()
    sats = constellation.sats
    assert len(sats) == 8
    # Four planes, evenly spaced in right ascension around the axis.
    raans = sorted({sat.orbit.raan for sat in sats})
    assert raans == pytest.approx([0.0, math.pi / 2, math.pi, 3 * math.pi / 2])
    # Two satellites per plane, opposite each other along the orbit.
    for plane_start in range(0, 8, 2):
        first, second = sats[plane_start], sats[plane_start + 1]
        assert first.orbit.raan == pytest.approx(second.orbit.raan)
        spacing = second.orbit.mean_anomaly0 - first.orbit.mean_anomaly0
        assert spacing == pytest.approx(math.pi)
    # Every satellite flies the same shape and tilt.
    assert {sat.orbit.semi_major for sat in sats} == {R_EARTH + 800e3}
    assert {sat.orbit.inclination for sat in sats} == {math.radians(63.4)}


def test_identities_are_numbered_and_zero_padded():
    constellation = _walker()
    assert [sat.name for sat in constellation.sats[:2]] == ["COSMOS-001", "COSMOS-002"]
    assert constellation.sats[7].sat_id == "COSMOS-008"  # id prefix defaults to the name prefix
    prefixed = _walker(sat_id_prefix="RU")
    assert prefixed.sats[0].sat_id == "RU-001" and prefixed.sats[0].name == "COSMOS-001"
    big = _walker(count=1500, planes=1)
    assert big.sats[0].sat_id == "COSMOS-0001" and big.sats[-1].sat_id == "COSMOS-1500"


def test_raan_and_phase_rotate_the_whole_pattern():
    constellation = _walker(raan_deg=45.0, phase_deg=90.0)
    first = constellation.sats[0].orbit
    assert first.raan == pytest.approx(math.radians(45.0))
    assert first.mean_anomaly0 == pytest.approx(math.radians(90.0))


def test_an_elliptical_constellation_carries_the_ellipse():
    constellation, problems = _parse(
        {
            "name_prefix": "TUNDRA",
            "nation": "RU",
            "count": 3,
            "planes": 3,
            "perigee_km": 600.0,
            "apogee_km": 39700.0,
            "argp_deg": 270.0,
            "inclination_deg": 63.4,
            "color": "#3366ff",
            "fov_half_angle_deg": 7.95,
            "cone_enabled": False,
        }
    )
    assert problems == []
    assert constellation.nation == "RU"
    assert constellation.describe() == "TUNDRA x3 (600 x 39700 km)"
    orbit = constellation.sats[0].orbit
    assert orbit.arg_perigee == pytest.approx(math.radians(270.0))
    assert orbit.perigee_radius == pytest.approx(R_EARTH + 600e3)
    # The cone hints ride along: the viewer draws a default cone when no
    # hint arrives, so the roster must be able to say what the cone is —
    # including the contract's off switch, width kept for the flip back.
    assert constellation.sats[0].display == {
        "color": "#3366ff",
        "fov_half_angle_deg": 7.95,
        "cone_enabled": False,
    }


def test_state_km_is_the_orbit_in_contract_units():
    sat = _walker().sats[0]
    position_km, velocity_kms = state_km(sat, 123.0)
    pos, vel = sat.orbit.position(123.0), sat.orbit.velocity(123.0)
    assert position_km == {"x": pos[0] / 1000, "y": pos[1] / 1000, "z": pos[2] / 1000}
    assert velocity_kms == {"vx": vel[0] / 1000, "vy": vel[1] / 1000, "vz": vel[2] / 1000}
    magnitude = math.hypot(*position_km.values())
    assert magnitude == pytest.approx((R_EARTH + 800e3) / 1000, rel=1e-9)


# ---- configuration ----------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"name_prefix": None}, "name_prefix must be a non-empty string"),
        ({"count": None}, "count must be a positive integer"),
        ({"count": 0}, "count must be a positive integer"),
        ({"count": True}, "count must be a positive integer"),
        ({"count": 10}, "count (10) must divide evenly among planes (4)"),
        ({"warp": 9}, "unknown key 'warp'"),
        ({"fov_half_angle_deg": 91.0}, "fov_half_angle_deg must be a number in (0, 90]"),
        ({"cone_enabled": "off"}, "cone_enabled must be true or false"),
        ({"update_period_s": 0}, "update_period_s must be a positive number"),
        ({"perigee_km": 600.0}, "altitude_km is the circular spelling"),
        ({"inclination_deg": "steep"}, "inclination_deg: must be a finite number"),
    ],
)
def test_parse_refuses_bad_entries(overrides, expected):
    entry = {
        "name_prefix": "COSMOS",
        "count": 8,
        "planes": 4,
        "altitude_km": 800.0,
        **overrides,
    }
    entry = {k: v for k, v in entry.items() if v is not None}
    # Strict and total: the problem is always reported (an unknown key
    # still lets the rest parse; load_bridge_config fails on any problem).
    _, problems = _parse(entry)
    assert any(expected in p for p in problems), problems
    # The flat roster spelling never mentions a nested .orbit table.
    assert all(".orbit" not in p for p in problems), problems


def test_roster_mixes_heroes_and_populations(tmp_path):
    path = tmp_path / "fleet.toml"
    path.write_text(
        f"""
[[satellites]]
sat_id = "90001"
port = 5001
def = "{IMAGING}"

[[constellations]]
name_prefix = "COSMOS"
sat_id_prefix = "RU"
count = 4
planes = 2
altitude_km = 800.0
"""
    )
    roster = load_bridge_config(path)
    assert [f.sat_id for f in roster.feeds] == ["90001"]
    assert len(roster.constellations) == 1
    assert [s.sat_id for s in roster.constellations[0].sats] == [
        "RU-001", "RU-002", "RU-003", "RU-004",
    ]


def test_roster_may_be_population_only(tmp_path):
    path = tmp_path / "fleet.toml"
    path.write_text(
        """
[[constellations]]
name_prefix = "COSMOS"
count = 2
altitude_km = 800.0
"""
    )
    roster = load_bridge_config(path)
    assert roster.feeds == [] and len(roster.constellations[0].sats) == 2


def test_identities_are_unique_across_kinds(tmp_path):
    path = tmp_path / "fleet.toml"
    path.write_text(
        f"""
[[satellites]]
sat_id = "RU-001"
port = 5001
def = "{IMAGING}"

[[constellations]]
name_prefix = "COSMOS"
sat_id_prefix = "RU"
count = 2
altitude_km = 800.0
"""
    )
    with pytest.raises(BridgeConfigError) as excinfo:
        load_bridge_config(path)
    assert "sat_id 'RU-001' already used by satellites #1" in str(excinfo.value)


def test_client_queue_scales_with_the_fleet():
    assert ViewerBridge([])._queue_max == 16  # the old floor survives
    constellation = _walker(count=100, planes=4)
    assert ViewerBridge([], [constellation])._queue_max == 200


# ---- the wire: no sims at all, just the catalog -----------------------------


async def test_stream_carries_the_whole_population():
    constellation = _walker(count=4, planes=2, update_period_s=0.2, color="#cc3333")
    ready: asyncio.Future = asyncio.get_running_loop().create_future()
    bridge_task = asyncio.create_task(
        run_bridge([], "127.0.0.1", 0, on_ready=ready.set_result,
                   constellations=[constellation])
    )
    states: dict[str, dict] = {}
    try:
        sse_port = await asyncio.wait_for(ready, timeout=5.0)
        async with asyncio.timeout(10.0):
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{sse_port}/telemetry/stream"
                ) as response:
                    frame: dict[str, str] = {}
                    async for line_bytes in response.content:
                        line = line_bytes.decode().rstrip("\n")
                        if line:
                            key, _, value = line.partition(": ")
                            frame[key] = value
                            continue
                        if frame.get("event") == "state":
                            state = json.loads(frame["data"])
                            states[state["sat_id"]] = state
                            if len(states) == 4:
                                break
                        frame = {}
    finally:
        bridge_task.cancel()
    assert sorted(states) == ["COSMOS-001", "COSMOS-002", "COSMOS-003", "COSMOS-004"]
    for state in states.values():
        assert state["frame"] == "J2000" and state["epoch"].endswith("Z")
        assert state["display"] == {"color": "#cc3333"}
        magnitude = math.hypot(*state["position_km"].values())
        assert magnitude == pytest.approx((R_EARTH + 800e3) / 1000, rel=1e-4)
