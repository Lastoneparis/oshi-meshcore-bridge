from oshi_bridge import omp, wire
from oshi_bridge.core import BridgeSettings, OshiBridgeCore
from sim import BCAST, Clock, Island, MeshCoreAir, OshiNode, SimBridge, fragments, run

ORIGIN, DEST = 0x0A0A0A0A, 0x0B0B0B0B
BRIDGE_A, BRIDGE_B = 0xA0000001, 0xB0000002


def world(settings=None):
    clock = Clock()
    west, east, air = Island("west"), Island("east"), MeshCoreAir()
    origin, dest = OshiNode(ORIGIN, west), OshiNode(DEST, east)
    a = SimBridge(BRIDGE_A, [west], air, clock, settings)
    b = SimBridge(BRIDGE_B, [east], air, clock, settings)
    return clock, west, east, air, origin, dest, a, b


def test_three_fragment_message_crosses_and_origin_gets_receipt():
    clock, west, east, air, origin, dest, a, b = world()
    body = bytes(range(256)) * 2  # 512 bytes -> 3 OMP fragments, two of them 200 bytes
    frames = origin.send_message(0x1234, DEST, body)
    assert len(frames) == 3 and len(frames[0]) == 200
    run(clock, [a, b], 120)

    assert dest.delivered == [(ORIGIN, 0x1234, body)]
    # dest SACKed the bridge (its Meshtastic sender), not the origin
    assert all(to == BRIDGE_B for to, _ in dest.sacks_sent)
    # the origin got a RECEIPT, as a DM, from its local bridge
    assert origin.receipts == [(BRIDGE_A, 0x1234, DEST)]
    receipt_pkts = [(f, t) for f, t, p in west.log if omp.frame_type(p) == omp.FrameType.RECEIPT]
    assert receipt_pkts == [(BRIDGE_A, ORIGIN)]
    # frames are re-injected byte-for-byte on the far mesh
    injected = [p for f, t, p in east.log if f == BRIDGE_B and omp.frame_type(p) == omp.FrameType.DATA]
    assert injected == frames
    # every MeshCore datagram is an "OB" envelope within the GRP_DATA limit
    assert all(wire.parse(dg) and len(dg) <= 160 for _, dg in air.log)
    # SACKs from the destination are never re-injected on the origin's mesh (it would ignore them)
    assert not [p for _, _, p in west.log if omp.frame_type(p) == omp.FrameType.SACK]


def test_missing_fragment_on_meshcore_is_repaired_from_the_near_bridge():
    clock, west, east, air, origin, dest, a, b = world()
    body = b"x" * 400  # 3 fragments
    dropped = {"n": 0}

    def drop_first_copy_of_frag1(dg):
        p = wire.parse(dg)
        if p and p.part == 0 and dropped["n"] == 0:
            raw = p.chunk
            if raw[:3] == b"OS\x11" and raw[15] == 1:
                dropped["n"] += 1
                return True
        return False

    air.drop = drop_first_copy_of_frag1
    origin.send_message(7, DEST, body)
    run(clock, [a, b], 200)
    assert dropped["n"] == 1
    assert dest.delivered and dest.delivered[0][2] == body
    assert a.core.stats["repair_to_mc"] >= 1 and b.core.stats["repair_requests"] >= 1
    assert origin.receipts and origin.receipts[0][1] == 7


def test_fragment_lost_on_far_mesh_is_reinjected_by_far_bridge():
    clock, west, east, air, origin, dest, a, b = world()
    lost = {"n": 0}

    def drop(frm, to, p):
        if frm == BRIDGE_B and p[:3] == b"OS\x11" and p[15] == 0 and lost["n"] == 0:
            lost["n"] += 1
            return True
        return False

    east.drop = drop
    origin.send_message(9, DEST, b"y" * 300)  # 2 fragments; dest SACKs 0b10 after the last one
    run(clock, [a, b], 120)
    assert b.core.stats["repair_to_mesh"] == 1
    assert b.core.stats["repair_requests"] == 0  # far bridge had it, no MeshCore round trip
    assert dest.delivered and origin.receipts


def test_origin_retransmission_after_delivery_stays_local():
    clock, west, east, air, origin, dest, a, b = world()
    frames = origin.send_message(5, DEST, b"hello")
    run(clock, [a, b], 60)
    assert origin.receipts
    n_air = len(air.log)
    clock.t += 70  # past repeat window and receipt_resend_s
    west.send(ORIGIN, BCAST, frames[-1], origin)  # the origin polls its last fragment
    run(clock, [a, b], 30)
    assert len(air.log) == n_air  # nothing new on MeshCore
    assert len(origin.receipts) == 2  # the near bridge answered locally


def test_two_bridges_between_joined_islands_do_not_ping_pong():
    clock = Clock()
    mesh, air = Island("one"), MeshCoreAir()
    origin = OshiNode(ORIGIN, mesh)
    OshiNode(DEST, mesh)
    a = SimBridge(BRIDGE_A, [mesh], air, clock)
    b = SimBridge(BRIDGE_B, [mesh], air, clock)
    origin.send_message(1, 0x0C0C0C0C, b"z" * 10)  # dest not on any mesh: nobody SACKs
    run(clock, [a, b], 900, step=1.0)
    data_on_air = [dg for _, dg in air.log if wire.parse(dg).chunk[:3] == b"OS\x11"]
    assert 1 <= len(data_on_air) <= 2  # each bridge at most once, never a loop
    data_injected = [p for f, _, p in mesh.log if f in (BRIDGE_A, BRIDGE_B) and omp.frame_type(p) == omp.FrameType.DATA]
    assert len(data_injected) <= 2


def test_two_near_bridges_one_far_bridge_deliver_once():
    clock = Clock()
    west, east, air = Island("west"), Island("east"), MeshCoreAir()
    origin, dest = OshiNode(ORIGIN, west), OshiNode(DEST, east)
    a1 = SimBridge(BRIDGE_A, [west], air, clock)
    a2 = SimBridge(0xA0000003, [west], air, clock)
    b = SimBridge(BRIDGE_B, [east], air, clock)
    origin.send_message(3, DEST, b"q" * 50)
    run(clock, [a1, a2, b], 60)
    assert len(dest.delivered) == 1
    assert b.core.stats["data_to_mesh"] == 1  # second copy deduplicated
    assert 1 <= len(origin.receipts) <= 2


def test_ignored_traffic():
    sent = []
    core = OshiBridgeCore(BRIDGE_A, lambda p, t: sent.append(p), lambda d: sent.append(d),
                          BridgeSettings(beacon_interval_s=0))
    beacon = bytes.fromhex("4f5315") + b"\x01\x00\x01\x05"
    core.on_mesh_packet(ORIGIN, BCAST, 256, beacon)  # BEACON: link-local
    core.on_mesh_packet(ORIGIN, BCAST, 1, fragments(1, ORIGIN, DEST, b"a")[0])  # TEXT_MESSAGE_APP
    core.on_mesh_packet(ORIGIN, BCAST, 256, b"OM\x01legacy")  # legacy OSHI framing
    core.on_mesh_packet(ORIGIN, BCAST, 256, fragments(2, ORIGIN, omp.DEST_INTERNET, b"a")[0])
    core.on_mesh_packet(ORIGIN, 0x12345678, 256, fragments(3, ORIGIN, DEST, b"a")[0])  # DM to someone else
    core.on_mesh_packet(BRIDGE_A, BCAST, 256, fragments(4, ORIGIN, DEST, b"a")[0])  # our own echo
    core.on_mc_datagram(b"random meshcore app data")
    core.on_mc_datagram(wire.split(fragments(5, ORIGIN, DEST, b"a")[0], BRIDGE_A, 1)[0])  # our own envelope
    core.pump()
    assert sent == []


def test_broadcast_forwarding_is_optional():
    sent = []
    core = OshiBridgeCore(BRIDGE_A, lambda p, t: None, sent.append, BridgeSettings(forward_broadcast=False))
    core.on_mesh_packet(ORIGIN, BCAST, 256, fragments(1, ORIGIN, omp.DEST_BROADCAST, b"a")[0])
    core.pump()
    assert sent == []


def test_forged_sack_from_non_destination_is_ignored():
    clock = Clock()
    out_mc = []
    core = OshiBridgeCore(BRIDGE_B, lambda p, t: None, out_mc.append, clock=clock)
    fr = fragments(1, ORIGIN, DEST, b"a")[0]
    core.on_mc_datagram(wire.split(fr, BRIDGE_A, 1)[0])
    s = omp.SackFrame(1, ORIGIN, 1, 1).encode()
    core.on_mesh_packet(0x66666666, BRIDGE_B, 256, s)
    clock.t += 5
    core.pump()
    assert core.stats["receipts_to_mc"] == 0
    core.on_mesh_packet(DEST, BRIDGE_B, 256, s)
    assert core.stats["receipts_to_mc"] == 1


def test_strict_sack_auth_requires_pki_when_key_known():
    core = OshiBridgeCore(BRIDGE_B, lambda p, t: None, lambda d: None, peer_has_key=lambda n: True)
    core.on_mc_datagram(wire.split(fragments(1, ORIGIN, DEST, b"a")[0], BRIDGE_A, 1)[0])
    s = omp.SackFrame(1, ORIGIN, 1, 1).encode()
    core.on_mesh_packet(DEST, BRIDGE_B, 256, s, pki=False)
    assert core.stats["receipts_to_mc"] == 0
    core.on_mesh_packet(DEST, BRIDGE_B, 256, s, pki=True)
    assert core.stats["receipts_to_mc"] == 1


def test_inject_only_known_dests():
    out = []
    core = OshiBridgeCore(BRIDGE_B, lambda p, t: out.append(p), lambda d: None,
                          BridgeSettings(inject_only_known_dests=True, beacon_interval_s=0), node_known=lambda n: n == DEST)
    core.on_mc_datagram(wire.split(fragments(1, ORIGIN, 0x0D0D0D0D, b"a")[0], BRIDGE_A, 1)[0])
    core.on_mc_datagram(wire.split(fragments(2, ORIGIN, DEST, b"a")[0], BRIDGE_A, 2)[0])
    core.pump()
    assert len(out) == 1 and omp.decode(out[0]).dest == DEST


def test_duty_cycle_is_respected():
    clock = Clock()
    times = []
    s = BridgeSettings(mc_duty_percent=1.0, duty_window_s=3600.0, mc_min_gap_s=3.0, repeat_s=0.0)
    core = OshiBridgeCore(BRIDGE_A, lambda p, t: None, lambda d: times.append((clock.t, d)), s, clock=clock)
    for i in range(40):  # 40 single-fragment 200-byte frames -> 80 MeshCore datagrams
        core.on_mesh_packet(ORIGIN, BCAST, 256, fragments(100 + i, ORIGIN, DEST, b"p" * 182)[0])
    end = clock.t + 3600
    while clock.t < end:
        core.pump()
        clock.t += 0.5
    gaps = [b[0] - a[0] for a, b in zip(times, times[1:])]
    assert gaps and min(gaps) >= 3.0
    at = s.mc_lora.airtime_s
    from oshi_bridge.airtime import meshcore_on_air_len
    used = sum(at(meshcore_on_air_len(len(d))) for _, d in times)
    assert used <= 36.0 + 1e-6  # 1 % of an hour
    assert len(times) < 80  # the rest is still queued, not blasted out


def test_queue_bound_prefers_control_traffic():
    core = OshiBridgeCore(BRIDGE_A, lambda p, t: None, lambda d: None, BridgeSettings(max_queue=4, repeat_s=0))
    for i in range(10):
        core.on_mesh_packet(ORIGIN, BCAST, 256, fragments(200 + i, ORIGIN, DEST, b"a")[0])
    assert len(core.to_mc) == 4
    assert core.to_mc.dropped_full == 6


def test_bridge_beacons_cap_bridge_at_start_and_every_interval():
    clock = Clock()
    sent = []
    core = OshiBridgeCore(BRIDGE_A, lambda p, t: sent.append((clock.t, p, t)), lambda d: None, clock=clock)
    t0 = clock.t
    for _ in range(int(3700 / 0.5)):
        core.pump()
        clock.t += 0.5
    beacons = [(t, p, to) for t, p, to in sent if omp.frame_type(p) == omp.FrameType.BEACON]
    # "OS" 0x15 | caps = CAP_BRIDGE | version 0x0100 LE | custodyFreeKb 0
    assert beacons[0][1] == bytes.fromhex("4f5315" "04" "0001" "00")
    assert all(to == BCAST for _, _, to in beacons)
    assert beacons[0][0] == t0
    assert [round(t - t0) for t, _, _ in beacons] == [0, 900, 1800, 2700, 3600]
    b = omp.decode_beacon(beacons[0][1])
    assert b.caps == omp.CAP_BRIDGE and not b.caps & (omp.CAP_CUSTODIAN | omp.CAP_GATEWAY_ONLINE)


def test_beacon_waits_for_airtime_and_is_not_duplicated():
    clock = Clock()
    sent = []
    s = BridgeSettings(mesh_min_gap_s=0, mesh_duty_percent=0.1, duty_window_s=3600)  # 3.6 s per hour
    core = OshiBridgeCore(BRIDGE_A, lambda p, t: sent.append(p), lambda d: None, s, clock=clock)
    for i in range(3):  # queue DATA ahead of the first pump; the beacon still goes first
        core.on_mc_datagram(wire.split(fragments(50 + i, ORIGIN, DEST, b"d" * 182)[0], BRIDGE_B, i + 1)[0])
    core.pump()
    assert omp.frame_type(sent[0]) == omp.FrameType.BEACON  # control traffic goes first
    n_before = len(core.to_mesh)
    clock.t += 1000  # interval elapsed while DATA is still waiting for budget
    core.pump()
    clock.t += 1000
    core.pump()
    beacons_queued = sum(1 for it in core.to_mesh._heap if it.label == "beacon")
    assert beacons_queued <= 1
    assert len(core.to_mesh) <= n_before + 1


def test_beacon_disabled():
    sent = []
    core = OshiBridgeCore(BRIDGE_A, lambda p, t: sent.append(p), lambda d: None, BridgeSettings(beacon_interval_s=0))
    core.pump()
    assert sent == []


def test_origin_rejects_receipt_without_bridge_beacon():
    clock, west, east, air, origin, dest, a, b = world(BridgeSettings(beacon_interval_s=0))
    origin.send_message(0x77, DEST, b"hi")
    run(clock, [a, b], 60)
    assert dest.delivered and not origin.receipts and origin.rejected_receipts == 1


def test_bridge_does_not_relay_beacons():
    clock, west, east, air, origin, dest, a, b = world()
    run(clock, [a, b], 5)
    assert air.log == []  # both bridges beaconed locally; nothing crossed MeshCore
    assert BRIDGE_A in origin.bridges and BRIDGE_B in dest.bridges
    assert BRIDGE_B not in origin.bridges
