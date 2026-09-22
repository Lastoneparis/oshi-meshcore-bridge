# oshi_bridge/core.py
"""Hardware-free bridge logic: OMP frames between a Meshtastic mesh and a MeshCore mesh.

Roles, per message (origin, msgId):

* near bridge  - heard the DATA frames on its Meshtastic side and sent them into MeshCore.
* far bridge   - received them from MeshCore and injected them into its Meshtastic side.

What crosses, and why (see README "Why SACKs are not relayed as-is"):

* DATA  mesh -> mc -> mesh, byte-for-byte. The OMP header already carries origin and
  final dest, so the destination accepts a frame whose Meshtastic ``from`` is a bridge.
* SACK  from the destination is consumed by the far bridge. The OSHI origin only accepts
  a SACK whose Meshtastic sender is the node it sent to (``Outbox::onSack`` checks
  ``e.linkTo == from``), so a SACK re-injected by a bridge would be ignored. Instead:
    - complete SACK  -> far bridge sends an OMP RECEIPT over MeshCore, the near bridge
      injects it as a DM to the origin (``OshiModule`` accepts RECEIPT from any sender
      that is trusted for the link) -> the phone shows DELIVERED.
    - partial SACK   -> far bridge re-injects the fragments it holds, and asks the near
      bridge (an OMP SACK over MeshCore) for the ones it never got.
* CUSTODY / BEACON / PULL / STATUS never cross: they are link-local by design, and a
  relayed BEACON would make OSHI nodes believe the bridge is a custodian.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import omp, wire
from .airtime import (
    MESHCORE_EU_NARROW,
    MESHTASTIC_LONG_FAST,
    AirtimeBudget,
    LoraParams,
    meshcore_on_air_len,
    meshtastic_on_air_len,
)
from .dedup import Deduper

log = logging.getLogger(__name__)

BROADCAST_NUM = 0xFFFFFFFF

PRIO_CONTROL = 0  # RECEIPT, repair requests
PRIO_REPAIR = 1
PRIO_DATA = 2


@dataclass
class BridgeSettings:
    forward_broadcast: bool = True
    inject_only_known_dests: bool = False
    records_ttl_s: float = 1800.0
    max_records: int = 512
    receipt_resend_s: float = 60.0
    repair_gap_s: float = 10.0
    max_repair_rounds: int = 4
    max_queue: int = 256
    max_datagram: int = wire.DEFAULT_MAX_DATAGRAM
    # dedup
    loop_ttl_s: float = 600.0
    repeat_s: float = 15.0
    max_forwards: int = 6
    # airtime
    mesh_lora: LoraParams = MESHTASTIC_LONG_FAST
    mc_lora: LoraParams = MESHCORE_EU_NARROW
    mesh_duty_percent: float = 5.0
    mc_duty_percent: float = 5.0
    duty_window_s: float = 3600.0
    mesh_min_gap_s: float = 2.5
    mc_min_gap_s: float = 2.5


@dataclass
class _Rec:
    dest: int
    count: int
    created: float
    frames: Dict[int, bytes] = field(default_factory=dict)
    delivered: bool = False
    last_receipt: float = float("-inf")
    last_repair: float = float("-inf")
    repair_rounds: int = 0

    @property
    def have(self) -> int:
        b = 0
        for i in self.frames:
            b |= 1 << i
        return b


class _Records:
    def __init__(self, ttl_s: float, max_n: int, clock: Callable[[], float]):
        self.ttl_s, self.max_n, self.clock = ttl_s, max_n, clock
        self._d: "OrderedDict[Tuple[int, int], _Rec]" = OrderedDict()

    def get(self, key: Tuple[int, int]) -> Optional[_Rec]:
        r = self._d.get(key)
        if r is not None and self.clock() - r.created > self.ttl_s:
            del self._d[key]
            return None
        return r

    def upsert(self, key: Tuple[int, int], dest: int, count: int) -> _Rec:
        r = self.get(key)
        if r is None or r.count != count or r.dest != dest:
            r = _Rec(dest, count, self.clock())
            self._d[key] = r
            while len(self._d) > self.max_n:
                self._d.popitem(last=False)
        self._d.move_to_end(key)
        return r

    def __len__(self) -> int:
        return len(self._d)


@dataclass(order=True)
class _Item:
    prio: int
    seq: int
    payload: bytes = field(compare=False)
    to: int = field(compare=False, default=BROADCAST_NUM)
    airtime: float = field(compare=False, default=0.0)
    label: str = field(compare=False, default="")


class _Outbound:
    def __init__(self, name: str, budget: AirtimeBudget, max_queue: int):
        self.name, self.budget, self.max_queue = name, budget, max_queue
        self._heap: List[_Item] = []
        self.dropped_full = 0
        self.sent = 0

    def put(self, item: _Item) -> bool:
        if len(self._heap) >= self.max_queue:
            worst = max(self._heap)
            if worst.prio <= item.prio:
                self.dropped_full += 1
                return False
            self._heap.remove(worst)
            heapq.heapify(self._heap)
            self.dropped_full += 1
        heapq.heappush(self._heap, item)
        return True

    def __len__(self) -> int:
        return len(self._heap)


class OshiBridgeCore:
    """Feed it packets with :meth:`on_mesh_packet` / :meth:`on_mc_datagram`, call
    :meth:`pump` regularly; it calls ``mesh_send(payload, to)`` and
    ``mc_send(datagram)`` when the duty-cycle budget allows."""

    def __init__(
        self,
        node_num: int,
        mesh_send: Callable[[bytes, int], None],
        mc_send: Callable[[bytes], None],
        settings: Optional[BridgeSettings] = None,
        node_known: Optional[Callable[[int], bool]] = None,
        peer_has_key: Optional[Callable[[int], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.node_num = node_num & 0xFFFFFFFF
        self.mesh_send = mesh_send
        self.mc_send = mc_send
        self.s = settings or BridgeSettings()
        self.node_known = node_known or (lambda n: True)
        self.peer_has_key = peer_has_key or (lambda n: False)
        self.clock = clock
        s = self.s
        self.dedup = Deduper(s.loop_ttl_s, s.repeat_s, s.max_forwards, clock=clock)
        self.near = _Records(s.records_ttl_s, s.max_records, clock)
        self.far = _Records(s.records_ttl_s, s.max_records, clock)
        self.joiner = wire.Joiner(clock=clock)
        self.to_mesh = _Outbound(
            "mesh", AirtimeBudget(s.mesh_duty_percent, s.duty_window_s, s.mesh_min_gap_s, clock), s.max_queue
        )
        self.to_mc = _Outbound(
            "mc", AirtimeBudget(s.mc_duty_percent, s.duty_window_s, s.mc_min_gap_s, clock), s.max_queue
        )
        self._seq = itertools.count()
        self._env_seq = 0
        self.stats: Dict[str, int] = {
            "mesh_rx_omp": 0,
            "mc_rx_omp": 0,
            "data_to_mc": 0,
            "data_to_mesh": 0,
            "receipts_to_mc": 0,
            "receipts_to_origin": 0,
            "repair_to_mesh": 0,
            "repair_to_mc": 0,
            "repair_requests": 0,
            "send_errors": 0,
        }

    # ------------------------------------------------------------------ Meshtastic side

    def on_mesh_packet(self, from_num: int, to_num: int, portnum: int, payload: bytes, pki: bool = False) -> None:
        if portnum != omp.PORTNUM_PRIVATE_APP or not omp.is_omp(payload):
            return
        if from_num == self.node_num:
            return
        if to_num not in (BROADCAST_NUM, self.node_num):
            return
        f = omp.decode(payload)
        if f is None:
            return
        self.stats["mesh_rx_omp"] += 1
        if isinstance(f, omp.DataFrame):
            self._mesh_data(f, payload)
        elif isinstance(f, omp.SackFrame):
            self._mesh_sack(f, from_num, pki)
        # RECEIPT / CUSTODY heard on the mesh are link-local: never relayed.

    def _mesh_data(self, f: omp.DataFrame, raw: bytes) -> None:
        if f.dest == omp.DEST_INTERNET:
            return  # gateway business, stays on the mesh
        if f.dest == omp.DEST_BROADCAST and not self.s.forward_broadcast:
            return
        key = (f.origin, f.msg_id)
        if self.far.get(key) is not None:
            return  # a frame we (or a sibling bridge) injected on this mesh
        rec = self.near.get(key)
        if rec is not None and rec.delivered:
            # The origin is still polling: its firmware ignores RECEIPT for the outbox. Re-send
            # the receipt locally instead of spending MeshCore airtime on a delivered message.
            self._receipt_to_origin(rec, f.origin, f.msg_id)
            return
        if not self.dedup.should_forward(("D", f.origin, f.msg_id, f.idx), "mesh"):
            return
        rec = self.near.upsert(key, f.dest, f.count)
        rec.frames[f.idx] = raw
        self._to_mc(raw, PRIO_DATA, "data")
        self.stats["data_to_mc"] += 1

    def _mesh_sack(self, s: omp.SackFrame, from_num: int, pki: bool) -> None:
        key = (s.origin, s.msg_id)
        rec = self.far.get(key)
        if rec is None or rec.count != s.count:
            return  # not a message we delivered on this mesh
        if rec.dest != omp.DEST_BROADCAST and from_num != rec.dest:
            return  # only the destination may acknowledge
        if self.peer_has_key(from_num) and not pki:
            return  # same rule as OshiModule::controlFrameTrusted: a PKI peer must answer over PKI
        if s.complete:
            rec.delivered = True
            self._receipt_to_mc(rec, s.origin, s.msg_id)
            return
        now = self.clock()
        if now - rec.last_repair < self.s.repair_gap_s or rec.repair_rounds >= self.s.max_repair_rounds:
            return
        rec.last_repair = now
        rec.repair_rounds += 1
        missing = omp.full_bitmap(s.count) & ~s.bitmap
        for idx in _bits(missing & rec.have):
            self._to_mesh(rec.frames[idx], BROADCAST_NUM, PRIO_REPAIR, "repair")
            self.stats["repair_to_mesh"] += 1
        still = missing & ~rec.have
        if still:
            ask = omp.SackFrame(s.msg_id, s.origin, s.count, omp.full_bitmap(s.count) & ~still)
            self._to_mc(ask.encode(), PRIO_CONTROL, "repair-req")
            self.stats["repair_requests"] += 1

    # ------------------------------------------------------------------ MeshCore side

    def on_mc_datagram(self, datagram: bytes) -> None:
        part = wire.parse(datagram)
        if part is None or part.bridge == self.node_num:
            return
        raw = self.joiner.push(part)
        if raw is None:
            return
        f = omp.decode(raw)
        if f is None:
            return
        self.stats["mc_rx_omp"] += 1
        if isinstance(f, omp.DataFrame):
            self._mc_data(f, raw)
        elif isinstance(f, omp.SackFrame):
            self._mc_repair_request(f)
        elif isinstance(f, omp.NoticeFrame) and f.type == omp.FrameType.RECEIPT:
            self._mc_receipt(f)

    def _mc_data(self, f: omp.DataFrame, raw: bytes) -> None:
        key = (f.origin, f.msg_id)
        if self.near.get(key) is not None:
            return  # we sent it into MeshCore ourselves (heard back via another bridge)
        if f.dest != omp.DEST_BROADCAST and self.s.inject_only_known_dests and not self.node_known(f.dest):
            return
        rec = self.far.upsert(key, f.dest, f.count)
        rec.frames[f.idx] = raw
        if rec.delivered:
            self._receipt_to_mc(rec, f.origin, f.msg_id)
            return
        if not self.dedup.should_forward(("D", f.origin, f.msg_id, f.idx), "mc"):
            return
        self._to_mesh(raw, BROADCAST_NUM, PRIO_DATA, "data")
        self.stats["data_to_mesh"] += 1

    def _mc_repair_request(self, s: omp.SackFrame) -> None:
        rec = self.near.get((s.origin, s.msg_id))
        if rec is None or rec.count != s.count or rec.delivered:
            return
        now = self.clock()
        if now - rec.last_repair < self.s.repair_gap_s or rec.repair_rounds >= self.s.max_repair_rounds:
            return
        rec.last_repair = now
        rec.repair_rounds += 1
        for idx in _bits(omp.full_bitmap(s.count) & ~s.bitmap & rec.have):
            self._to_mc(rec.frames[idx], PRIO_REPAIR, "repair")
            self.stats["repair_to_mc"] += 1

    def _mc_receipt(self, n: omp.NoticeFrame) -> None:
        rec = self.near.get((n.origin, n.msg_id))
        if rec is None:
            return  # another bridge carried this message; it will tell the origin
        rec.delivered = True
        self._receipt_to_origin(rec, n.origin, n.msg_id)

    # ------------------------------------------------------------------ receipts

    def _receipt_to_mc(self, rec: _Rec, origin: int, msg_id: int) -> None:
        now = self.clock()
        if now - rec.last_receipt < self.s.receipt_resend_s:
            return
        rec.last_receipt = now
        r = omp.NoticeFrame(omp.FrameType.RECEIPT, msg_id, origin, rec.dest)
        self._to_mc(r.encode(), PRIO_CONTROL, "receipt")
        self.stats["receipts_to_mc"] += 1

    def _receipt_to_origin(self, rec: _Rec, origin: int, msg_id: int) -> None:
        now = self.clock()
        if now - rec.last_receipt < self.s.receipt_resend_s:
            return
        rec.last_receipt = now
        r = omp.NoticeFrame(omp.FrameType.RECEIPT, msg_id, origin, rec.dest)
        # A DM, so the bridge radio PKI-encrypts it when it knows the origin's key; OshiModule
        # rejects a channel-encrypted control frame from a peer it has a key for.
        self._to_mesh(r.encode(), origin, PRIO_CONTROL, "receipt")
        self.stats["receipts_to_origin"] += 1

    # ------------------------------------------------------------------ queues

    def _to_mesh(self, payload: bytes, to: int, prio: int, label: str) -> None:
        at = self.s.mesh_lora.airtime_s(meshtastic_on_air_len(len(payload)))
        self.to_mesh.put(_Item(prio, next(self._seq), payload, to, at, label))

    def _to_mc(self, frame: bytes, prio: int, label: str) -> None:
        self._env_seq = (self._env_seq + 1) & 0xFF
        for dg in wire.split(frame, self.node_num, self._env_seq, self.s.max_datagram):
            at = self.s.mc_lora.airtime_s(meshcore_on_air_len(len(dg)))
            self.to_mc.put(_Item(prio, next(self._seq), dg, BROADCAST_NUM, at, label))

    def pump(self) -> float:
        """Send whatever the budgets allow. Returns seconds until it is worth calling again."""
        wake = 1.0
        for q, send in ((self.to_mesh, lambda it: self.mesh_send(it.payload, it.to)),
                        (self.to_mc, lambda it: self.mc_send(it.payload))):
            while q._heap:
                it = q._heap[0]
                w = q.budget.wait_time(it.airtime)
                if w == float("inf"):
                    heapq.heappop(q._heap)  # can never fit the budget
                    continue
                if w > 0:
                    wake = min(wake, w)
                    break
                heapq.heappop(q._heap)
                try:
                    send(it)
                    q.sent += 1
                except Exception as e:  # the radio is gone; OMP's own retries recover
                    self.stats["send_errors"] += 1
                    log.warning("%s send failed (%s): %s", q.name, it.label, e)
                q.budget.spend(it.airtime)
        return max(wake, 0.05)

    def snapshot(self) -> Dict[str, object]:
        return {
            **self.stats,
            "queue_mesh": len(self.to_mesh),
            "queue_mc": len(self.to_mc),
            "dropped_queue_full": self.to_mesh.dropped_full + self.to_mc.dropped_full,
            "dropped_loop": self.dedup.dropped_loop,
            "dropped_dup": self.dedup.dropped_dup,
            "airtime_mesh_s": round(self.to_mesh.budget.used_s, 2),
            "airtime_mc_s": round(self.to_mc.budget.used_s, 2),
        }


def _bits(v: int):
    i = 0
    while v:
        if v & 1:
            yield i
        v >>= 1
        i += 1
