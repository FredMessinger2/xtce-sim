"""
Blocking TCP client helpers for talking to a running simulator.

`send_command` fires a single CCSDS command frame; `stream_packets` yields
telemetry packets from a live connection. Both share the wire framing in
`xtce_sim.ccsds`, so anything the sim serves, these read — and vice versa.
"""

from __future__ import annotations

import socket
from typing import Iterator, Optional

from xtce_sim import ccsds, codec
from xtce_sim.definition import CommandDef


def send_command(
    host: str,
    port: int,
    command: CommandDef,
    args: Optional[dict] = None,
    *,
    apid: int = 1,
) -> bytes:
    """Connect, send one command frame, and disconnect. Returns the CCSDS packet."""
    payload = codec.encode_command(command, args)
    packet = (
        ccsds.CCSDSHeader(packet_type=int(ccsds.PacketType.COMMAND), apid=apid).pack()
        + bytes([command.opcode])
        + payload
    )
    with socket.create_connection((host, port)) as sock:
        sock.sendall(ccsds.frame(packet))
    return packet


def stream_packets(
    host: str, port: int, *, timeout: Optional[float] = None
) -> Iterator[bytes]:
    """Yield CCSDS packets (CRC-stripped) from a live server until it closes."""
    sock = socket.create_connection((host, port))
    if timeout is not None:
        sock.settimeout(timeout)
    buffer = b""
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            packets, buffer = ccsds.deframe(buffer + data)
            yield from packets
    finally:
        sock.close()
