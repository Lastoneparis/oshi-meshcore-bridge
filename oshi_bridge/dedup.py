# oshi_bridge/dedup.py
"""Loop and duplicate suppression between the two networks.

A key is (origin, msgId, frame type, idx-or-bitmap). ``side`` is the network a
frame was *received* from ("mesh" or "mc"). A frame is forwarded unless:

  A. the same key was received from the OTHER side within ``loop_ttl_s``:
     it is our own (or another bridge's) injection coming back, i.e. a loop;
  B. it was already forwarded from this side within ``repeat_s``: a plain
     duplicate (a neighbour's rebroadcast, a second bridge's copy);
  C. it was already forwarded ``max_forwards`` times from this side within
     ``loop_ttl_s``: a hard cap, so even a slow loop terminates.

Rule B's window is short on purpose: the OMP origin re-sends its last fragment
every ~25 s when no SACK comes back, and those repair polls must get across.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, Hashable

SIDES = ("mesh", "mc")


def other(side: str) -> str:
    return "mc" if side == "mesh" else "mesh"


@dataclass
class _Entry:
    rx: Dict[str, float] = field(default_factory=dict)  # side -> last time received and forwarded
    first: Dict[str, float] = field(default_factory=dict)  # side -> first forward in the current ttl
    count: Dict[str, int] = field(default_factory=dict)


class Deduper:
    def __init__(
        self,
        loop_ttl_s: float = 600.0,
        repeat_s: float = 15.0,
        max_forwards: int = 6,
        max_keys: int = 8192,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.loop_ttl_s = loop_ttl_s
        self.repeat_s = repeat_s
        self.max_forwards = max_forwards
        self.max_keys = max_keys
        self.clock = clock
        self._t: "OrderedDict[Hashable, _Entry]" = OrderedDict()
        self.dropped_loop = 0
        self.dropped_dup = 0

    def mark_injected(self, key: Hashable, into: str) -> None:
        """Record that the bridge itself put ``key`` onto network ``into``, so
        hearing it back from ``into`` counts as a loop (rule A for the other side)."""
        self._entry(key).rx[other(into)] = self.clock()

    def should_forward(self, key: Hashable, side: str) -> bool:
        now = self.clock()
        e = self._t.get(key)
        if e is not None:
            o = e.rx.get(other(side))
            if o is not None and now - o < self.loop_ttl_s:
                self.dropped_loop += 1
                return False
            last = e.rx.get(side)
            if last is not None and now - last < self.repeat_s:
                self.dropped_dup += 1
                return False
            first = e.first.get(side)
            if first is not None and now - first < self.loop_ttl_s:
                if e.count.get(side, 0) >= self.max_forwards:
                    self.dropped_dup += 1
                    return False
            else:
                e.first[side] = now
                e.count[side] = 0
        else:
            e = self._entry(key)
            e.first[side] = now
            e.count[side] = 0
        e.rx[side] = now
        e.count[side] = e.count.get(side, 0) + 1
        self._t.move_to_end(key)
        return True

    def _entry(self, key: Hashable) -> _Entry:
        e = self._t.get(key)
        if e is None:
            e = _Entry()
            self._t[key] = e
            while len(self._t) > self.max_keys:
                self._t.popitem(last=False)
        return e

    def __len__(self) -> int:
        return len(self._t)
