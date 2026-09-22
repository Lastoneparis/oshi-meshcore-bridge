# oshi_bridge/meshtastic_io.py
"""Meshtastic side: a radio running STOCK Meshtastic firmware with the "OSHI" channel.

Not an OSHI-firmware radio: OshiModule swallows OMP frames before they reach the
client API (MeshService: ``oshiModule->swallowsForPhone``) and consumes OMP frames the
client sends as its own outbox, so a bridge on OSHI firmware would see nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from . import omp

log = logging.getLogger(__name__)

# (from_num, to_num, portnum, payload, pki)
PacketSink = Callable[[int, int, int, bytes, bool], None]


def packet_fields(packet: Dict[str, Any]) -> Optional[tuple]:
    """Extract (from, to, portnum, payload, pki) from a meshtastic-python packet dict,
    or None when it is not a decoded PRIVATE_APP packet."""
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return None
    port = decoded.get("portnum")
    if port == "PRIVATE_APP":
        port = omp.PORTNUM_PRIVATE_APP
    if port != omp.PORTNUM_PRIVATE_APP:
        return None
    payload = decoded.get("payload")
    if not isinstance(payload, (bytes, bytearray)):
        return None
    try:
        frm = int(packet.get("from", 0)) & 0xFFFFFFFF
        to = int(packet.get("to", 0xFFFFFFFF)) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None
    return frm, to, omp.PORTNUM_PRIVATE_APP, bytes(payload), bool(packet.get("pkiEncrypted", False))


def find_channel_index(local_node: Any, name: str) -> Optional[int]:
    for ch in getattr(local_node, "channels", None) or []:
        role = getattr(ch, "role", 0)
        settings = getattr(ch, "settings", None)
        if role and settings is not None and getattr(settings, "name", "") == name:
            return int(getattr(ch, "index", 0))
    return None


class MeshtasticLink:
    def __init__(self, transport: str, port: Optional[str], host: Optional[str], channel_name: str):
        self.transport, self.port, self.host = transport, port, host
        self.channel_name = channel_name
        self.iface: Any = None
        self.channel_index: Optional[int] = None
        self.node_num: Optional[int] = None
        self._sink: Optional[PacketSink] = None
        self._on_lost: Optional[Callable[[], None]] = None

    def open(self, sink: PacketSink, on_lost: Callable[[], None]) -> None:
        import meshtastic.serial_interface
        import meshtastic.tcp_interface
        from pubsub import pub

        self._sink, self._on_lost = sink, on_lost
        if self.transport == "tcp":
            self.iface = meshtastic.tcp_interface.TCPInterface(hostname=self.host)
        else:
            self.iface = meshtastic.serial_interface.SerialInterface(devPath=self.port)
        self.node_num = int(self.iface.myInfo.my_node_num)
        self.channel_index = find_channel_index(self.iface.localNode, self.channel_name)
        if self.channel_index is None:
            self.close()
            raise RuntimeError(
                f"no channel named {self.channel_name!r} on the Meshtastic radio; add it first "
                "(README: 'Meshtastic radio setup')"
            )
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")
        log.info("Meshtastic !%08x ready, OSHI channel index %d", self.node_num, self.channel_index)

    def close(self) -> None:
        try:
            from pubsub import pub

            pub.unsubscribe(self._on_receive, "meshtastic.receive")
            pub.unsubscribe(self._on_connection_lost, "meshtastic.connection.lost")
        except Exception:
            pass
        if self.iface is not None:
            try:
                self.iface.close()
            except Exception:
                pass
        self.iface = None

    def _on_receive(self, packet: Dict[str, Any], interface: Any = None) -> None:
        if interface is not None and interface is not self.iface:
            return
        f = packet_fields(packet)
        if f and omp.is_omp(f[3]) and self._sink:
            self._sink(*f)

    def _on_connection_lost(self, interface: Any = None) -> None:
        if interface is self.iface and self._on_lost:
            self._on_lost()

    def send(self, payload: bytes, to: int) -> None:
        if self.iface is None:
            raise RuntimeError("Meshtastic not connected")
        unicast = to != 0xFFFFFFFF
        # Unicast (a RECEIPT to the origin) goes as a DM: firmware PKI-encrypts it when it holds the
        # origin's key and retries it at the link layer with want_ack.
        self.iface.sendData(
            payload,
            destinationId=to,
            portNum=omp.PORTNUM_PRIVATE_APP,
            wantAck=unicast,
            channelIndex=self.channel_index or 0,
        )

    def node_known(self, num: int) -> bool:
        nodes = getattr(self.iface, "nodesByNum", None) or {}
        return num in nodes

    def peer_has_key(self, num: int) -> bool:
        nodes = getattr(self.iface, "nodesByNum", None) or {}
        user = (nodes.get(num) or {}).get("user") or {}
        return bool(user.get("publicKey"))
