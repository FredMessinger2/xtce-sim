"""Tests for the command encoder, JSON round-trip, and the send/monitor verbs."""

import asyncio
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from xtce_sim import client, codec
from xtce_sim.cli import main
from xtce_sim.definition import CommandDef, ParamInfo, SimDefinition
from xtce_sim.generate import to_dict
from xtce_sim.server import SimServer

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
XTCE = [EXAMPLES / "my_vehicle_commands.xml", EXAMPLES / "my_vehicle_telemetry.xml"]


@pytest.fixture(scope="module")
def simdef() -> SimDefinition:
    return SimDefinition.from_xtce(XTCE)


def test_encode_decode_roundtrip():
    cmd = CommandDef(
        name="SET_POWER",
        opcode=0x10,
        params=[
            ParamInfo("SubsystemId", 8, "uint8"),
            ParamInfo("PowerState", 8, "uint8", enumerations={"OFF": 0, "ON": 1}),
        ],
    )
    payload = codec.encode_command(cmd, {"SubsystemId": 3, "PowerState": "ON"})
    assert codec.decode_command(cmd, payload) == {"SubsystemId": 3, "PowerState": "ON"}


def test_encode_rejects_unknown_arg():
    cmd = CommandDef(name="NOOP", opcode=0, params=[])
    with pytest.raises(ValueError):
        codec.encode_command(cmd, {"Bogus": 1})


def test_encode_hex_and_defaults():
    cmd = CommandDef(
        name="C",
        opcode=1,
        params=[ParamInfo("A", 8, "uint8"), ParamInfo("B", 16, "uint16")],
    )
    # "0x10" parses as hex; missing B defaults to 0.
    assert codec.encode_command(cmd, {"A": "0x10"}) == struct.pack(">BH", 0x10, 0)


def test_json_roundtrip(simdef: SimDefinition):
    restored = SimDefinition.from_dict(to_dict(simdef))
    assert restored.space_system_name == simdef.space_system_name
    assert len(restored.commands) == len(simdef.commands)
    assert len(restored.packets) == len(simdef.packets)
    # Struct formats must survive the round-trip (needed to decode telemetry).
    assert [p.struct_format for p in restored.packets] == [
        p.struct_format for p in simdef.packets
    ]
    c0, r0 = simdef.commands[0], restored.commands[0]
    assert (r0.name, r0.opcode) == (c0.name, c0.opcode)


def _run_server(simdef, handler=None):
    return SimServer(
        simdef, host="127.0.0.1", port=0, beacon_interval=0.05, command_handler=handler
    )


async def test_send_command_verb_reaches_server(simdef: SimDefinition, tmp_path):
    received: list = []

    async def handler(srv, command, args):
        received.append((command.name, args))

    server = _run_server(simdef, handler)
    await server.start()
    try:
        # Dump a cmd_tlm.json so `send --def <json>` has a definition to load.
        from xtce_sim.generate import format_json

        def_json = tmp_path / "cmd_tlm.json"
        def_json.write_text(format_json(simdef))

        runner = CliRunner()
        # Run the blocking CLI in a thread so the event loop keeps serving.
        result = await asyncio.to_thread(
            runner.invoke,
            main,
            [
                "send",
                "--def",
                str(def_json),
                "--port",
                str(server.bound_port),
                "SET_POWER",
                "SubsystemId=3",
                "PowerState=ON",
            ],
        )
        assert result.exit_code == 0, result.output

        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received == [("SET_POWER", {"SubsystemId": 3, "PowerState": "ON"})]
    finally:
        await server.stop()


def test_stream_packets_decodes(simdef: SimDefinition):
    """stream_packets + unpack_telemetry decode a beacon end-to-end (threaded server)."""

    async def _main():
        server = _run_server(simdef)
        await server.start()
        port = server.bound_port
        try:
            packets = await asyncio.to_thread(
                lambda: [p for _, p in zip(range(3), client.stream_packets("127.0.0.1", port))]
            )
        finally:
            await server.stop()
        return packets

    packets = asyncio.run(_main())
    assert len(packets) == 3
    from xtce_sim import ccsds

    header = ccsds.CCSDSHeader.unpack(packets[0][:6])
    pkt_def = simdef.packet_by_apid(header.apid)
    assert pkt_def is not None
    values = codec.unpack_telemetry(pkt_def, packets[0][6:])
    assert len(values) == len(pkt_def.fields)
