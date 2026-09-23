# oshi_bridge/omp.py
"""OSHI Mesh Protocol (OMP) v1 frame codec, byte-for-byte compatible with
oshi-mesh-firmware src/oshi/OshiProtocol.cpp (branch ``oshi``).

Every frame is the payload of a Meshtastic PRIVATE_APP (256) packet and starts
with ``"OS"`` followed by ``(version << 4) | type``. All integers are
little-endian.

    DATA    : prefix(3) msgId u32 | origin u32 | dest u32 | idx u8 | count u8 | flags u8 | data (<=182)
    SACK    : prefix(3) msgId u32 | origin u32 | count u8 | bitmap u64
    CUSTODY / RECEIPT : prefix(3) msgId u32 | origin u32 | dest u32
    BEACON  : prefix(3) caps u8 | version u16 | custodyFreeKb u8
    PULL    : prefix(3) nodeNum u32 | afterSeq u32 | tsSec u32 | sig[64]
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Union

MAGIC = b"OS"
VERSION = 1
PORTNUM_PRIVATE_APP = 256

MAX_FRAME = 200
DATA_HEADER = 18
MAX_FRAG_DATA = MAX_FRAME - DATA_HEADER
MAX_FRAGS = 64

DEST_BROADCAST = 0xFFFFFFFF
DEST_INTERNET = 0xFFFFFFF0

FLAG_CUSTODY_OK = 1 << 0
FLAG_VIA_CUSTODY = 1 << 1
FLAG_CUSTODY_REQ = 1 << 2

# BEACON caps (BeaconCaps in OshiProtocol.h)
CAP_CUSTODIAN = 1 << 0
CAP_GATEWAY_ONLINE = 1 << 1
CAP_BRIDGE = 1 << 2  # the firmware accepts a RECEIPT from a node whose fresh beacon carries this

_PREFIX = 3
_SACK_LEN = _PREFIX + 4 + 4 + 1 + 8
_NOTICE_LEN = _PREFIX + 4 + 4 + 4
_BEACON_LEN = _PREFIX + 1 + 2 + 1


class FrameType(IntEnum):
    DATA = 1
    SACK = 2
    CUSTODY = 3
    RECEIPT = 4
    BEACON = 5
    STATUS = 6
    PULL = 7


def full_bitmap(count: int) -> int:
    return (1 << 64) - 1 if count >= 64 else (1 << count) - 1


def is_omp(buf: bytes) -> bool:
    return (
        buf is not None
        and len(buf) >= _PREFIX
        and buf[0:2] == MAGIC
        and (buf[2] >> 4) == VERSION
    )


def frame_type(buf: bytes) -> Optional[FrameType]:
    if not is_omp(buf):
        return None
    t = buf[2] & 0x0F
    try:
        return FrameType(t)
    except ValueError:
        return None


def _prefix(t: FrameType) -> bytes:
    return MAGIC + bytes([(VERSION << 4) | int(t)])


@dataclass(frozen=True)
class DataFrame:
    msg_id: int
    origin: int
    dest: int
    idx: int
    count: int
    flags: int
    data: bytes

    def encode(self) -> bytes:
        if not (0 < self.count <= MAX_FRAGS) or self.idx >= self.count:
            raise ValueError("bad idx/count")
        if len(self.data) > MAX_FRAG_DATA:
            raise ValueError("fragment too long")
        return (
            _prefix(FrameType.DATA)
            + struct.pack("<IIIBBB", self.msg_id, self.origin, self.dest, self.idx, self.count, self.flags)
            + self.data
        )


@dataclass(frozen=True)
class SackFrame:
    msg_id: int
    origin: int
    count: int
    bitmap: int

    def encode(self) -> bytes:
        if not (0 < self.count <= MAX_FRAGS):
            raise ValueError("bad count")
        return _prefix(FrameType.SACK) + struct.pack(
            "<IIBQ", self.msg_id, self.origin, self.count, self.bitmap & full_bitmap(self.count)
        )

    @property
    def complete(self) -> bool:
        return self.bitmap & full_bitmap(self.count) == full_bitmap(self.count)


@dataclass(frozen=True)
class NoticeFrame:
    """CUSTODY or RECEIPT."""

    type: FrameType
    msg_id: int
    origin: int
    dest: int

    def encode(self) -> bytes:
        if self.type not in (FrameType.CUSTODY, FrameType.RECEIPT):
            raise ValueError("not a notice type")
        return _prefix(self.type) + struct.pack("<III", self.msg_id, self.origin, self.dest)


@dataclass(frozen=True)
class BeaconFrame:
    caps: int
    version: int = VERSION << 8  # OMP_IMPL_VERSION: (OMP_VERSION << 8) | 0
    custody_free_kb: int = 0

    def encode(self) -> bytes:
        return _prefix(FrameType.BEACON) + struct.pack("<BHB", self.caps, self.version, self.custody_free_kb)


def decode_beacon(buf: bytes) -> Optional[BeaconFrame]:
    if frame_type(buf) != FrameType.BEACON or len(buf) < _BEACON_LEN:
        return None
    caps, version, free_kb = struct.unpack_from("<BHB", buf, _PREFIX)
    return BeaconFrame(caps, version, free_kb)


Frame = Union[DataFrame, SackFrame, NoticeFrame]


def decode(buf: bytes) -> Optional[Frame]:
    """Decode the frame types the bridge cares about. Returns None for anything
    malformed, and for BEACON / STATUS / PULL (which the bridge never relays)."""
    t = frame_type(buf)
    if t is None:
        return None
    p = buf[_PREFIX:]
    if t == FrameType.DATA:
        if len(buf) < DATA_HEADER or len(buf) > MAX_FRAME:
            return None
        msg_id, origin, dest, idx, count, flags = struct.unpack_from("<IIIBBB", p)
        if not (0 < count <= MAX_FRAGS) or idx >= count:
            return None
        return DataFrame(msg_id, origin, dest, idx, count, flags, bytes(buf[DATA_HEADER:]))
    if t == FrameType.SACK:
        if len(buf) < _SACK_LEN:
            return None
        msg_id, origin, count, bitmap = struct.unpack_from("<IIBQ", p)
        if not (0 < count <= MAX_FRAGS):
            return None
        return SackFrame(msg_id, origin, count, bitmap & full_bitmap(count))
    if t in (FrameType.CUSTODY, FrameType.RECEIPT):
        if len(buf) < _NOTICE_LEN:
            return None
        msg_id, origin, dest = struct.unpack_from("<III", p)
        return NoticeFrame(t, msg_id, origin, dest)
    return None
