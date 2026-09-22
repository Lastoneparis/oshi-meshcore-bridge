"""The asyncio runner with fake radio links: threads, queues and the pump loop."""

import asyncio
import threading

from oshi_bridge import config, wire
from oshi_bridge.runner import Runner
from sim import fragments

ORIGIN, DEST, ME = 0x0A0A0A0A, 0x0B0B0B0B, 0xA0000001


class FakeMesh:
    node_num = ME

    def __init__(self):
        self.sent = []

    def open(self, sink, on_lost):
        self.sink, self.on_lost = sink, on_lost

    def close(self):
        pass

    def send(self, payload, to):
        self.sent.append((payload, to))

    def node_known(self, n):
        return True

    def peer_has_key(self, n):
        return False


class FakeMC:
    def __init__(self):
        self.sent = []

    async def open(self, sink, on_lost=None):
        self.sink, self.on_lost = sink, on_lost

    async def send(self, dg):
        self.sent.append(dg)

    async def drain(self):
        pass

    async def close(self):
        pass


def test_runner_relays_both_ways():
    cfg = config.Config()
    cfg.bridge.mesh_min_gap_s = cfg.bridge.mc_min_gap_s = 0.01
    mesh, mc = FakeMesh(), FakeMC()

    async def go():
        r = Runner(cfg, mesh=mesh, mc=mc)
        task = asyncio.create_task(r.run())
        await asyncio.sleep(0.05)
        fr = fragments(1, ORIGIN, DEST, b"hello")[0]
        # meshtastic-python calls back on its own reader thread
        t = threading.Thread(target=mesh.sink, args=(ORIGIN, 0xFFFFFFFF, 256, fr, False))
        t.start()
        t.join()
        back = fragments(2, DEST, ORIGIN, b"reply")[0]
        mc.sink(wire.split(back, 0xB0000002, 1)[0])
        for _ in range(100):
            if mc.sent and mesh.sent:
                break
            await asyncio.sleep(0.02)
        r.stop()
        assert await task == 0
        assert wire.parse(mc.sent[0]).chunk == fr
        assert mesh.sent == [(back, 0xFFFFFFFF)]

    asyncio.run(go())


def test_runner_exits_nonzero_when_a_radio_is_lost():
    mesh, mc = FakeMesh(), FakeMC()

    async def go():
        r = Runner(config.Config(), mesh=mesh, mc=mc)
        task = asyncio.create_task(r.run())
        await asyncio.sleep(0.05)
        threading.Thread(target=mesh.on_lost).start()
        assert await asyncio.wait_for(task, 2) == 1

    asyncio.run(go())
