# oshi_bridge/wire.py
"""Envelope carrying OMP frames inside MeshCore group-channel datagrams
(PAYLOAD_TYPE_GRP_DATA, companion CMD_SEND_CHANNEL_DATA / RESP_CODE_CHANNEL_DATA_RECV).

A MeshCore channel datagram carries at most ~163-165 bytes of application data
(MAX_CHANNEL_DATA_LENGTH / MAX_GROUP_DATA_LENGTH in MeshCore), while an OMP frame
can be 200 bytes, so large frames are split in two parts.

Envelope (all little-endian):

    0-1  "OB"                      marker (OSHI Bridge)
    2    version << 4              version = 1, low nibble reserved (0)
    3-6  bridge u32                Meshtastic node number of the bridge that sent it
    7    seq u8                    per-bridge sequence number of the OMP frame
    8    part << 4 | total         part index (0-based) and number of parts (1..15)
    9..  slice of the OMP frame
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

MARKER = b"OB"
VERSION = 1
HEADER_LEN = 9
DEFAULT_MAX_DATAGRAM = 160  # conservative vs MeshCore's 163 (docs) / 165 (code)


@dataclass(frozen=True)
class Part:
    bridge: int
    seq: int
    part: int
    total: int
    chunk: bytes


def split(frame: bytes, bridge: int, seq: int, max_datagram: int = DEFAULT_MAX_DATAGRAM) -> List[bytes]:
    room = max_datagram - HEADER_LEN
    if room <= 0:
        raise ValueError("max_datagram too small")
    chunks = [frame[i : i + room] for i in range(0, len(frame), room)] or [b""]
    if len(chunks) > 15:
        raise ValueError("frame too large for envelope")
    out = []
    for i, c in enumerate(chunks):
        out.append(
            MARKER
            + bytes([VERSION << 4])
            + struct.pack("<I", bridge & 0xFFFFFFFF)
            + bytes([seq & 0xFF, (i << 4) | len(chunks)])
            + c
        )
    return out


def parse(datagram: bytes) -> Optional[Part]:
    if len(datagram) < HEADER_LEN or datagram[0:2] != MARKER or (datagram[2] >> 4) != VERSION:
        return None
    (bridge,) = struct.unpack_from("<I", datagram, 3)
    seq = datagram[7]
    part, total = datagram[8] >> 4, datagram[8] & 0x0F
    if total == 0 or part >= total:
        return None
    return Part(bridge, seq, part, total, bytes(datagram[HEADER_LEN:]))


@dataclass
class _Pending:
    total: int
    first: float
    chunks: Dict[int, bytes] = field(default_factory=dict)


class Joiner:
    """Reassembles multi-part envelopes. Keyed by (bridge, seq); incomplete
    sets expire after ``timeout_s``."""

    def __init__(self, timeout_s: float = 90.0, max_pending: int = 128, clock: Callable[[], float] = time.monotonic):
        self.timeout_s = timeout_s
        self.max_pending = max_pending
        self.clock = clock
        self._pending: Dict[Tuple[int, int], _Pending] = {}

    def push(self, p: Part) -> Optional[bytes]:
        if p.total == 1:
            return p.chunk
        now = self.clock()
        self._expire(now)
        key = (p.bridge, p.seq)
        e = self._pending.get(key)
        if e is None or e.total != p.total:
            if len(self._pending) >= self.max_pending:
                oldest = min(self._pending, key=lambda k: self._pending[k].first)
                del self._pending[oldest]
            e = _Pending(p.total, now)
            self._pending[key] = e
        e.chunks[p.part] = p.chunk
        if len(e.chunks) == e.total:
            del self._pending[key]
            return b"".join(e.chunks[i] for i in range(e.total))
        return None

    def _expire(self, now: float) -> None:
        for k in [k for k, e in self._pending.items() if now - e.first > self.timeout_s]:
            del self._pending[k]
