from oshi_bridge.airtime import MESHCORE_EU_NARROW, MESHTASTIC_LONG_FAST, AirtimeBudget, LoraParams
from oshi_bridge.dedup import Deduper


def test_airtime_reference_values():
    # SF7/125k/4:5, 8-symbol preamble, 20 bytes: 56.6 ms (Semtech calculator)
    assert abs(LoraParams(sf=7, bw_khz=125, cr=5, preamble=8).airtime_s(20) - 0.0566) < 0.001
    # LongFast SF11/250k, 222 bytes on air: 233.25 symbols x 8.192 ms = 1.911 s
    assert abs(MESHTASTIC_LONG_FAST.airtime_s(222) - 1.9108) < 0.001
    assert MESHCORE_EU_NARROW.airtime_s(180) > MESHCORE_EU_NARROW.airtime_s(40)


def test_budget_min_gap_and_window():
    t = [0.0]
    b = AirtimeBudget(duty_percent=1.0, window_s=100.0, min_gap_s=2.0, clock=lambda: t[0])  # 1 s per 100 s
    assert b.wait_time(0.6) == 0
    b.spend(0.6)
    assert b.wait_time(0.1) == 2.0  # gap
    t[0] = 3.0
    assert b.wait_time(0.6) == 97.0  # window: the first 0.6 s must age out
    assert b.wait_time(5.0) == float("inf")
    t[0] = 100.0
    assert b.wait_time(0.6) == 0


def test_dedup_loop_repeat_and_cap():
    t = [0.0]
    d = Deduper(loop_ttl_s=600, repeat_s=15, max_forwards=3, clock=lambda: t[0])
    k = ("D", 1, 2, 0)
    assert d.should_forward(k, "mesh")
    assert not d.should_forward(k, "mesh")  # duplicate
    assert not d.should_forward(k, "mc")  # coming back from the other side = loop
    t[0] = 20
    assert d.should_forward(k, "mesh")  # the origin's retransmission gets through
    t[0] = 40
    assert d.should_forward(k, "mesh")
    t[0] = 60
    assert not d.should_forward(k, "mesh")  # cap of 3 per ttl
    t[0] = 700
    assert d.should_forward(k, "mesh")  # new ttl period
    assert d.dropped_loop == 1


def test_mark_injected():
    d = Deduper(clock=lambda: 0.0)
    d.mark_injected("k", into="mesh")
    assert not d.should_forward("k", "mesh")
    assert d.should_forward("k2", "mesh")
