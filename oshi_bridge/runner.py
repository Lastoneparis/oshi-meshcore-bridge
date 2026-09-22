# oshi_bridge/runner.py
"""Process entry point: wires MeshtasticLink + MeshCoreLink to OshiBridgeCore.

On losing either radio the process exits non-zero; run it under systemd
(packaging/oshi-bridge.service) so it restarts cleanly with fresh state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Optional

from . import config as cfgmod
from .core import OshiBridgeCore
from .meshcore_io import MeshCoreLink
from .meshtastic_io import MeshtasticLink

log = logging.getLogger("oshi_bridge")


class Runner:
    def __init__(self, cfg: cfgmod.Config, mesh: Optional[MeshtasticLink] = None, mc: Optional[MeshCoreLink] = None):
        self.cfg = cfg
        self.mesh = mesh or MeshtasticLink(cfg.mesh_transport, cfg.mesh_port, cfg.mesh_host, cfg.mesh_channel_name)
        self.mc = mc or MeshCoreLink(
            cfg.mc_transport, cfg.mc_port, cfg.mc_baud, cfg.mc_host, cfg.mc_tcp_port, cfg.mc_ble_address,
            cfg.mc_channel_idx, cfg.mc_channel_name, cfg.mc_secret, cfg.data_type, cfg.mc_configure_channel,
        )
        self.core: Optional[OshiBridgeCore] = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.exit_code = 0
        self._tasks: set = set()

    def _poke(self) -> None:
        self._wake.set()

    def _lost(self, what: str) -> None:
        log.error("%s connection lost", what)
        self.exit_code = 1
        self._stop.set()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def mesh_sink(frm, to, port, payload, pki):
            def deliver():
                if self.core:
                    self.core.on_mesh_packet(frm, to, port, payload, pki)
                    self._poke()
            loop.call_soon_threadsafe(deliver)

        await loop.run_in_executor(
            None, self.mesh.open, mesh_sink, lambda: loop.call_soon_threadsafe(self._lost, "Meshtastic")
        )

        def mc_sink(datagram: bytes) -> None:
            if self.core:
                self.core.on_mc_datagram(datagram)
                self._poke()

        def mc_send(datagram: bytes) -> None:
            t = asyncio.ensure_future(self.mc.send(datagram))
            self._tasks.add(t)
            t.add_done_callback(self._mc_sent)

        s = self.cfg.bridge
        self.core = OshiBridgeCore(
            self.mesh.node_num,
            mesh_send=self.mesh.send,
            mc_send=mc_send,
            settings=s,
            node_known=self.mesh.node_known,
            peer_has_key=self.mesh.peer_has_key if self.cfg.strict_sack_auth else None,
        )
        await self.mc.open(mc_sink, on_lost=lambda: self._lost("MeshCore"))

    def _mc_sent(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None and self.core:
            self.core.stats["send_errors"] += 1
            log.warning("MeshCore send failed: %s", t.exception())

    async def run(self) -> int:
        await self.start()
        last_stats = asyncio.get_running_loop().time()
        last_drain = last_stats
        while not self._stop.is_set():
            wait = self.core.pump()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            now = asyncio.get_running_loop().time()
            if now - last_drain > 60:
                last_drain = now
                asyncio.ensure_future(self.mc.drain())  # in case a MSG_WAITING push was missed
            if self.cfg.stats_interval_s and now - last_stats > self.cfg.stats_interval_s:
                last_stats = now
                log.info("stats %s", self.core.snapshot())
        await self.shutdown()
        return self.exit_code

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def shutdown(self) -> None:
        await self.mc.close()
        await asyncio.get_running_loop().run_in_executor(None, self.mesh.close)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Relay OSHI Mesh Protocol frames between Meshtastic and MeshCore")
    ap.add_argument("-c", "--config", default="oshi_bridge.ini")
    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.config)
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    async def _amain() -> int:
        r = Runner(cfg)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, r.stop)
            except NotImplementedError:
                pass
        return await r.run()

    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
