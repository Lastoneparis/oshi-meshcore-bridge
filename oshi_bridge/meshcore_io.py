# oshi_bridge/meshcore_io.py
"""MeshCore side: a companion-radio firmware (v1.15+, which has CMD_SEND_CHANNEL_DATA /
RESP_CODE_CHANNEL_DATA_RECV) driven through meshcore_py.

OMP frames travel as group-channel *datagrams* (PAYLOAD_TYPE_GRP_DATA) on a dedicated
channel, tagged with our ``data_type``. Stock MeshCore apps that do not carry that
channel cannot decrypt them, so ordinary MeshCore users never see them (repeaters still
relay them - they cost airtime, not screen space).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

CMD_SYNC_NEXT_MESSAGE = 0x0A
CMD_SEND_CHANNEL_DATA = 0x3E
PATH_FLOOD = 0xFF


def channel_data_command(channel_idx: int, data_type: int, payload: bytes) -> bytes:
    return bytes([CMD_SEND_CHANNEL_DATA, channel_idx & 0xFF, PATH_FLOOD]) + data_type.to_bytes(2, "little") + payload


class MeshCoreLink:
    def __init__(
        self,
        transport: str,
        port: Optional[str],
        baud: int,
        host: Optional[str],
        tcp_port: int,
        ble_address: Optional[str],
        channel_idx: int,
        channel_name: str,
        channel_secret: bytes,
        data_type: int,
        configure_channel: bool = True,
    ):
        self.transport, self.port, self.baud = transport, port, baud
        self.host, self.tcp_port, self.ble_address = host, tcp_port, ble_address
        self.channel_idx, self.channel_name, self.channel_secret = channel_idx, channel_name, channel_secret
        self.data_type = data_type
        self.configure_channel = configure_channel
        self.mc: Any = None
        self._sink: Optional[Callable[[bytes], None]] = None
        self._drain_lock = asyncio.Lock()
        self._subs: list = []

    async def open(
        self, sink: Callable[[bytes], None], mc: Any = None, on_lost: Optional[Callable[[], None]] = None
    ) -> None:
        """``mc`` lets tests pass a fake MeshCore object."""
        from meshcore import EventType, MeshCore

        self._sink = sink
        if mc is not None:
            self.mc = mc
        elif self.transport == "tcp":
            self.mc = await MeshCore.create_tcp(self.host, self.tcp_port)
        elif self.transport == "ble":
            self.mc = await MeshCore.create_ble(self.ble_address)
        else:
            self.mc = await MeshCore.create_serial(self.port, self.baud)
        if self.mc is None:
            raise RuntimeError("MeshCore companion radio did not answer")
        if self.configure_channel:
            await self._ensure_channel()
        self._subs.append(
            self.mc.subscribe(EventType.CHANNEL_DATA_RECV, self._on_channel_data)
        )
        self._subs.append(self.mc.subscribe(EventType.MESSAGES_WAITING, self._on_waiting))
        if on_lost is not None:
            self._subs.append(self.mc.subscribe(EventType.DISCONNECTED, lambda e: on_lost()))
        log.info(
            "MeshCore ready, channel %d %r, data_type 0x%04x", self.channel_idx, self.channel_name, self.data_type
        )
        await self.drain()

    async def _ensure_channel(self) -> None:
        from meshcore import EventType

        cur = await self.mc.commands.get_channel(self.channel_idx)
        if cur is not None and cur.type == EventType.CHANNEL_INFO:
            p = cur.payload or {}
            if p.get("channel_name") == self.channel_name and p.get("channel_secret") == self.channel_secret:
                return
            if p.get("channel_name"):
                log.warning(
                    "MeshCore channel slot %d holds %r; overwriting with %r",
                    self.channel_idx, p.get("channel_name"), self.channel_name,
                )
        res = await self.mc.commands.set_channel(self.channel_idx, self.channel_name, self.channel_secret)
        if res is None or res.type == EventType.ERROR:
            raise RuntimeError(f"could not set MeshCore channel {self.channel_idx}: {getattr(res, 'payload', None)}")

    def _on_channel_data(self, event: Any) -> None:
        p = event.payload or {}
        if p.get("channel_idx") != self.channel_idx or p.get("data_type") != self.data_type:
            return
        try:
            data = bytes.fromhex(p.get("payload", ""))
        except ValueError:
            return
        if self._sink:
            self._sink(data)

    async def _on_waiting(self, event: Any) -> None:
        await self.drain()

    async def drain(self) -> None:
        """Pull the companion's offline queue. meshcore_py's own ``get_msg`` does not list
        CHANNEL_DATA_RECV as a reply, so it would wait for its timeout on every datagram."""
        from meshcore import EventType

        if self.mc is None:
            return
        async with self._drain_lock:
            for _ in range(64):
                res = await self.mc.commands.send(
                    bytes([CMD_SYNC_NEXT_MESSAGE]),
                    [
                        EventType.CONTACT_MSG_RECV,
                        EventType.CHANNEL_MSG_RECV,
                        EventType.CHANNEL_DATA_RECV,
                        EventType.NO_MORE_MSGS,
                        EventType.ERROR,
                    ],
                )
                if res is None or res.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                    return

    async def send(self, datagram: bytes) -> None:
        from meshcore import EventType

        if self.mc is None:
            raise RuntimeError("MeshCore not connected")
        res = await self.mc.commands.send(
            channel_data_command(self.channel_idx, self.data_type, datagram), [EventType.OK, EventType.ERROR]
        )
        if res is None or res.type == EventType.ERROR:
            raise RuntimeError(f"CMD_SEND_CHANNEL_DATA refused: {getattr(res, 'payload', None)}")

    async def close(self) -> None:
        for s in self._subs:
            try:
                self.mc.unsubscribe(s)
            except Exception:
                pass
        self._subs.clear()
        if self.mc is not None:
            try:
                await self.mc.disconnect()
            except Exception:
                pass
        self.mc = None
