# oshi_bridge/airtime.py
"""LoRa time-on-air estimate (Semtech AN1200.13) and a sliding-window airtime
budget, so the bridge never pushes either radio past its duty cycle."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Tuple


@dataclass(frozen=True)
class LoraParams:
    sf: int = 11
    bw_khz: float = 250.0
    cr: int = 5  # coding rate denominator: 5 means 4/5, 8 means 4/8
    preamble: int = 16
    explicit_header: bool = True
    crc: bool = True

    def airtime_s(self, payload_len: int) -> float:
        t_sym = (2 ** self.sf) / (self.bw_khz * 1000.0)
        de = 1 if t_sym > 0.016 else 0
        ih = 0 if self.explicit_header else 1
        num = 8 * payload_len - 4 * self.sf + 28 + (16 if self.crc else 0) - 20 * ih
        den = 4 * (self.sf - 2 * de)
        n_payload = 8 + max(math.ceil(num / den) * self.cr, 0)
        return (self.preamble + 4.25) * t_sym + n_payload * t_sym


# Defaults: Meshtastic LONG_FAST and MeshCore's EU/UK narrow preset.
MESHTASTIC_LONG_FAST = LoraParams(sf=11, bw_khz=250.0, cr=5, preamble=16)
MESHCORE_EU_NARROW = LoraParams(sf=8, bw_khz=62.5, cr=8, preamble=16)


def meshtastic_on_air_len(payload_len: int) -> int:
    # 16-byte radio header + protobuf Data (portnum + length tags) + payload; AES-CTR adds no padding.
    return 16 + payload_len + 6


def meshcore_on_air_len(payload_len: int) -> int:
    # header + path_len + channel hash + 2-byte MAC + AES-ECB ciphertext of (data_type u16, len u8, data)
    return 1 + 1 + 1 + 2 + 16 * math.ceil((3 + payload_len) / 16)


class AirtimeBudget:
    """At most ``duty_percent`` of airtime in any ``window_s``, and at least
    ``min_gap_s`` between two transmissions."""

    def __init__(
        self,
        duty_percent: float = 5.0,
        window_s: float = 3600.0,
        min_gap_s: float = 2.5,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.budget_s = window_s * duty_percent / 100.0
        self.window_s = window_s
        self.min_gap_s = min_gap_s
        self.clock = clock
        self._log: Deque[Tuple[float, float]] = deque()
        self._used = 0.0
        self._last_tx = None

    def _trim(self, now: float) -> None:
        while self._log and now - self._log[0][0] >= self.window_s:
            _, a = self._log.popleft()
            self._used -= a

    def wait_time(self, airtime_s: float) -> float:
        """Seconds to wait before a transmission of ``airtime_s`` fits (0 = now)."""
        now = self.clock()
        self._trim(now)
        wait = 0.0
        if self._last_tx is not None:
            wait = max(wait, self._last_tx + self.min_gap_s - now)
        if self._used + airtime_s > self.budget_s:
            if airtime_s > self.budget_s:
                return float("inf")
            need = self._used + airtime_s - self.budget_s
            freed = 0.0
            for t, a in self._log:
                freed += a
                if freed >= need:
                    wait = max(wait, t + self.window_s - now)
                    break
        return max(wait, 0.0)

    def spend(self, airtime_s: float) -> None:
        now = self.clock()
        self._trim(now)
        self._log.append((now, airtime_s))
        self._used += airtime_s
        self._last_tx = now

    @property
    def used_s(self) -> float:
        self._trim(self.clock())
        return self._used
