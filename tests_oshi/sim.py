"""Tiny in-memory world: Meshtastic islands, one MeshCore medium, OSHI end nodes
modelled on oshi-mesh-firmware's OshiModule (receiveData / Outbox) closely enough to
exercise the bridge: the destination SACKs the Meshtastic sender on each last fragment
and on completion, the origin records RECEIPTs addressed to it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from oshi_bridge import omp
from oshi_bridge.core import BridgeSettings, OshiBridgeCore

BCAST = 0xFFFFFFFF


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Island:
    """A Meshtastic mesh: everything sent reaches every other member."""

    def __init__(self, name: str):
        self.name = name
        self.members: List[object] = []
        self.log: List[Tuple[int, int, bytes]] = []
        self.drop: Callable[[int, int, bytes], bool] = lambda frm, to, p: False

    def send(self, frm: int, to: int, payload: bytes, sender: object) -> None:
        self.log.append((frm, to, payload))
        if self.drop(frm, to, payload):
            return
        for m in self.members:
            if m is not sender:
                m.mesh_rx(frm, to, payload)


class MeshCoreAir:
    def __init__(self):
        self.bridges: List["SimBridge"] = []
        self.log: List[Tuple[int, bytes]] = []
        self.drop: Callable[[bytes], bool] = lambda dg: False

    def send(self, sender: "SimBridge", dg: bytes) -> None:
        self.log.append((sender.num, dg))
        if self.drop(dg):
            return
        for b in self.bridges:
            if b is not sender:
                b.core.on_mc_datagram(dg)


class SimBridge:
    def __init__(self, num: int, islands: List[Island], air: MeshCoreAir, clock: Clock,
                 settings: Optional[BridgeSettings] = None):
        self.num = num
        self.islands = islands
        self.air = air
        self.core = OshiBridgeCore(
            num,
            mesh_send=self._mesh_send,
            mc_send=lambda dg: air.send(self, dg),
            settings=settings or BridgeSettings(),
            clock=clock,
        )
        for i in islands:
            i.members.append(self)
        air.bridges.append(self)

    def _mesh_send(self, payload: bytes, to: int) -> None:
        for i in self.islands:
            i.send(self.num, to, payload, self)

    def mesh_rx(self, frm: int, to: int, payload: bytes) -> None:
        self.core.on_mesh_packet(frm, to, omp.PORTNUM_PRIVATE_APP, payload)


@dataclass
class OshiNode:
    num: int
    island: Island
    rx: Dict[Tuple[int, int], Dict[int, bytes]] = field(default_factory=dict)
    delivered: List[Tuple[int, int, bytes]] = field(default_factory=list)
    receipts: List[Tuple[int, int, int]] = field(default_factory=list)  # (from, msgId, dest)
    sacks_sent: List[Tuple[int, omp.SackFrame]] = field(default_factory=list)
    seen: Set[Tuple[int, int]] = field(default_factory=set)

    def __post_init__(self):
        self.island.members.append(self)

    def mesh_rx(self, frm: int, to: int, payload: bytes) -> None:
        if to not in (BCAST, self.num):
            return
        f = omp.decode(payload)
        if isinstance(f, omp.DataFrame) and f.dest == self.num and f.origin != self.num:
            key = (f.origin, f.msg_id)
            if key in self.seen:
                if f.idx + 1 == f.count:
                    self._sack(frm, f.origin, f.msg_id, f.count, omp.full_bitmap(f.count))
                return
            frags = self.rx.setdefault(key, {})
            frags[f.idx] = f.data
            have = sum(1 << i for i in frags)
            complete = len(frags) == f.count
            if f.idx + 1 == f.count or complete:
                self._sack(frm, f.origin, f.msg_id, f.count, have)
            if complete:
                self.seen.add(key)
                self.delivered.append((f.origin, f.msg_id, b"".join(frags[i] for i in range(f.count))))
        elif isinstance(f, omp.NoticeFrame) and f.type == omp.FrameType.RECEIPT and f.origin == self.num:
            self.receipts.append((frm, f.msg_id, f.dest))

    def _sack(self, to: int, origin: int, msg_id: int, count: int, have: int) -> None:
        s = omp.SackFrame(msg_id, origin, count, have)
        self.sacks_sent.append((to, s))
        # firmware transmit(): DM when it has the peer's key, else broadcast; the sim uses DM.
        self.island.send(self.num, to, s.encode(), self)

    def send_message(self, msg_id: int, dest: int, body: bytes, only: Optional[Set[int]] = None) -> List[bytes]:
        frames = fragments(msg_id, self.num, dest, body)
        for i, fr in enumerate(frames):
            if only is None or i in only:
                self.island.send(self.num, BCAST, fr, self)
        return frames


def fragments(msg_id: int, origin: int, dest: int, body: bytes, flags: int = omp.FLAG_CUSTODY_OK) -> List[bytes]:
    F = omp.MAX_FRAG_DATA
    n = max(1, -(-len(body) // F))
    return [
        omp.DataFrame(msg_id, origin, dest, i, n, flags, body[i * F:(i + 1) * F]).encode() for i in range(n)
    ]


def run(clock: Clock, bridges: List[SimBridge], seconds: float, step: float = 0.5) -> None:
    end = clock.t + seconds
    while clock.t < end:
        for b in bridges:
            b.core.pump()
        clock.t += step
