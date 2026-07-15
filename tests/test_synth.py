"""Tests for synthetic live telemetry."""

import struct
from pathlib import Path

from xtce_sim import codec, synth
from xtce_sim.definition import FieldInfo, SimDefinition
from xtce_sim.generate import fields_to_struct_format

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DATA = Path(__file__).resolve().parent / "data"


def _simdef():
    return SimDefinition.from_xtce(
        [DATA / "my_vehicle/my_vehicle_commands.xml", DATA / "my_vehicle/my_vehicle_telemetry.xml"]
    )


def test_values_pack_for_every_packet():
    """Synthetic values must fit each field's type so packing never overflows."""
    simdef = _simdef()
    live = synth.LiveTelemetry()
    for packet in simdef.packets:
        for t in (0.0, 7.3, 120.0, 9999.0):
            values = live.values_at(packet, t)
            payload = codec.pack_telemetry(packet, values)  # must not raise
            assert struct.calcsize(packet.struct_format) == len(payload)


def test_values_change_over_time():
    simdef = _simdef()
    hk = simdef.packet_by_name("HOUSEKEEPING")
    live = synth.LiveTelemetry()
    a = live.values_at(hk, 0.0)
    b = live.values_at(hk, 30.0)
    # At least some fields move between two distant times.
    assert a != b


def test_counter_fields_rise():
    simdef = _simdef()
    hk = simdef.packet_by_name("HOUSEKEEPING")
    live = synth.LiveTelemetry()
    early = live.values_at(hk, 1.0)
    late = live.values_at(hk, 50.0)
    assert late["HK_UPTIME"] > early["HK_UPTIME"]


def test_clock_injection_makes_it_deterministic():
    now = [100.0]
    live = synth.LiveTelemetry(clock=lambda: now[0])  # start captured at 100.0
    pkt = _simdef().packet_by_name("HOUSEKEEPING")
    now[0] = 100.0
    first = live(pkt)
    now[0] = 130.0
    later = live(pkt)
    assert first == live.values_at(pkt, 0.0)
    assert later == live.values_at(pkt, 30.0)


def test_int_fields_stay_in_range():
    """uint8 fields must never exceed 255, etc."""
    field_types = {
        "uint8": (0, 255),
        "int8": (-128, 127),
        "uint16": (0, 65535),
    }
    simdef = _simdef()
    live = synth.LiveTelemetry()
    for packet in simdef.packets:
        for t in (0.0, 13.0, 500.0):
            vals = live.values_at(packet, t)
            for f in packet.fields:
                if f.python_type in field_types:
                    lo, hi = field_types[f.python_type]
                    assert lo <= vals[f.name] <= hi, (f.name, f.python_type, vals[f.name])


def test_strings_stay_empty():
    simdef = _simdef()
    live = synth.LiveTelemetry()
    for packet in simdef.packets:
        vals = live.values_at(packet, 42.0)
        for f in packet.fields:
            if f.python_type in ("string", "bytes"):
                assert vals[f.name] == b""


def test_synth_saturates_not_wraps():
    # WHEEL_SPEED signal is ~1500; on a uint8 field it must saturate to 255,
    # not modulo-wrap (1500 % 256 == 220). This distinguishes clamp from wrap.
    assert synth._synth_value(FieldInfo("WHEEL_SPEED_1", 8, "uint8"), 0.0) == 255


def test_synth_negative_on_unsigned_stays_in_range():
    # No unsigned field should ever go below its floor.
    for t in (0.0, 3.0, 12.0, 47.0):
        v = synth._synth_value(FieldInfo("ANGULAR_RATE_X", 16, "uint16"), t)
        assert 0 <= v <= 65535


def test_synth_supports_64bit():
    v = synth._synth_value(FieldInfo("TIMESTAMP", 64, "uint64"), 5.0)
    assert 0 <= v <= 18446744073709551615
    struct.pack(">Q", v)  # must fit


def test_fields_to_struct_format_used():
    # Sanity: a hand-built packet packs with synthetic values.
    from xtce_sim.definition import FieldInfo, PacketDef

    pkt = PacketDef(
        name="X",
        apid=1,
        fields=[FieldInfo("A_VOLTAGE", 16, "uint16"), FieldInfo("A_TEMP", 32, "float32")],
    )
    pkt.struct_format = fields_to_struct_format(pkt.fields)
    vals = synth.LiveTelemetry().values_at(pkt, 5.0)
    codec.pack_telemetry(pkt, vals)  # must not raise


def test_live_values_are_raw_counts_for_calibrated_fields():
    # The heuristic thinks "about 8 volts"; the wire wants the count that
    # calibrates back to it (0.125 V/count -> ~64 counts), so the monitor's
    # engineering view shows plausible volts, not sub-volt garbage.
    from xtce_sim.definition import SimDefinition
    from xtce_sim.synth import LiveTelemetry

    simdef = SimDefinition.from_xtce(DATA / "my_vehicle/my_vehicle.xml")
    hk = simdef.packet_by_name("HOUSEKEEPING")
    values = LiveTelemetry(clock=lambda: 0.0).values_at(hk, 3.0)
    volts_raw = values["HK_BATTERY_VOLTAGE"]
    volts_eu = 0.125 * volts_raw
    assert 5.0 < volts_eu < 10.0  # plausible bus voltage after round-trip
    therm_raw = values["HK_TEMP_THERMISTOR"]
    assert 1024 <= therm_raw <= 3072  # counts for a sane temperature, not 23
