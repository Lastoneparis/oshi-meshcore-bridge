# oshi_bridge/config.py
"""INI configuration for the OSHI <-> MeshCore bridge (see examples/oshi_bridge.ini.example)."""

from __future__ import annotations

import configparser
import hashlib
from dataclasses import dataclass, field
from typing import Optional

from .airtime import LoraParams
from .core import BridgeSettings

OSHI_CHANNEL_NAME = "OSHI"
OSHI_CHANNEL_PSK_HEX = "29436d19af55bd4e20247323c8ff455786f003119817e16cf7df3003684cb950"
DEFAULT_DATA_TYPE = 0xFF4F  # MeshCore dev range 0xFF00-0xFFFE, no registration needed
DEFAULT_MC_CHANNEL_NAME = "#oshi-bridge"


@dataclass
class Config:
    # Meshtastic side (a radio running STOCK Meshtastic firmware, see README)
    mesh_transport: str = "serial"  # serial | tcp
    mesh_port: Optional[str] = None  # serial device, or None to auto-detect
    mesh_host: Optional[str] = None
    mesh_channel_name: str = OSHI_CHANNEL_NAME
    # MeshCore side (a companion-radio firmware, USB serial or TCP)
    mc_transport: str = "serial"  # serial | tcp | ble
    mc_port: Optional[str] = None
    mc_baud: int = 115200
    mc_host: Optional[str] = None
    mc_tcp_port: int = 5000
    mc_ble_address: Optional[str] = None
    mc_channel_idx: int = 7
    mc_channel_name: str = DEFAULT_MC_CHANNEL_NAME
    mc_channel_secret: Optional[bytes] = None  # 16 bytes; None = derived from a '#name'
    mc_configure_channel: bool = True
    data_type: int = DEFAULT_DATA_TYPE
    log_level: str = "INFO"
    stats_interval_s: float = 300.0
    # Accept a SACK from a destination whose key the bridge radio holds only if it came over PKI
    # (OshiModule::controlFrameTrusted). Off by default: it silently loses receipts when the
    # destination does not know the bridge's key.
    strict_sack_auth: bool = False
    bridge: BridgeSettings = field(default_factory=BridgeSettings)

    @property
    def mc_secret(self) -> bytes:
        if self.mc_channel_secret is not None:
            return self.mc_channel_secret
        # MeshCore hashtag channels: secret = sha256(name)[:16] (meshcore_py set_channel does the same)
        return hashlib.sha256(self.mc_channel_name.encode("utf-8")).digest()[:16]


def _lora(sec: configparser.SectionProxy, prefix: str, default: LoraParams) -> LoraParams:
    return LoraParams(
        sf=sec.getint(f"{prefix}_sf", default.sf),
        bw_khz=sec.getfloat(f"{prefix}_bw_khz", default.bw_khz),
        cr=sec.getint(f"{prefix}_cr", default.cr),
        preamble=sec.getint(f"{prefix}_preamble", default.preamble),
    )


def load(path: str) -> Config:
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise FileNotFoundError(path)
    c = Config()
    m = cp["meshtastic"] if cp.has_section("meshtastic") else cp["DEFAULT"]
    c.mesh_transport = m.get("transport", c.mesh_transport)
    c.mesh_port = m.get("port", fallback=None) or None
    c.mesh_host = m.get("host", fallback=None) or None
    c.mesh_channel_name = m.get("channel_name", c.mesh_channel_name)

    k = cp["meshcore"] if cp.has_section("meshcore") else cp["DEFAULT"]
    c.mc_transport = k.get("transport", c.mc_transport)
    c.mc_port = k.get("port", fallback=None) or None
    c.mc_baud = k.getint("baud", c.mc_baud)
    c.mc_host = k.get("host", fallback=None) or None
    c.mc_tcp_port = k.getint("tcp_port", c.mc_tcp_port)
    c.mc_ble_address = k.get("ble_address", fallback=None) or None
    c.mc_channel_idx = k.getint("channel_idx", c.mc_channel_idx)
    c.mc_channel_name = k.get("channel_name", c.mc_channel_name)
    sec_hex = k.get("channel_secret_hex", fallback="").strip()
    if sec_hex:
        secret = bytes.fromhex(sec_hex)
        if len(secret) != 16:
            raise ValueError("meshcore.channel_secret_hex must be 16 bytes (32 hex chars)")
        c.mc_channel_secret = secret
    c.mc_configure_channel = k.getboolean("configure_channel", c.mc_configure_channel)
    c.data_type = int(k.get("data_type", hex(c.data_type)), 0)
    if not 0 < c.data_type < 0x10000:
        raise ValueError("meshcore.data_type must be 0x0001..0xFFFF")

    b = cp["bridge"] if cp.has_section("bridge") else cp["DEFAULT"]
    s = c.bridge
    s.forward_broadcast = b.getboolean("forward_broadcast", s.forward_broadcast)
    s.inject_only_known_dests = b.getboolean("inject_only_known_dests", s.inject_only_known_dests)
    s.max_datagram = b.getint("max_datagram", s.max_datagram)
    s.loop_ttl_s = b.getfloat("loop_ttl_s", s.loop_ttl_s)
    s.repeat_s = b.getfloat("repeat_s", s.repeat_s)
    s.max_forwards = b.getint("max_forwards", s.max_forwards)
    s.receipt_resend_s = b.getfloat("receipt_resend_s", s.receipt_resend_s)
    s.max_queue = b.getint("max_queue", s.max_queue)
    s.mesh_duty_percent = b.getfloat("mesh_duty_percent", s.mesh_duty_percent)
    s.mc_duty_percent = b.getfloat("mc_duty_percent", s.mc_duty_percent)
    s.duty_window_s = b.getfloat("duty_window_s", s.duty_window_s)
    s.mesh_min_gap_s = b.getfloat("mesh_min_gap_s", s.mesh_min_gap_s)
    s.mc_min_gap_s = b.getfloat("mc_min_gap_s", s.mc_min_gap_s)
    s.mesh_lora = _lora(b, "mesh", s.mesh_lora)
    s.mc_lora = _lora(b, "mc", s.mc_lora)
    c.log_level = b.get("log_level", c.log_level)
    c.stats_interval_s = b.getfloat("stats_interval_s", c.stats_interval_s)
    c.strict_sack_auth = b.getboolean("strict_sack_auth", c.strict_sack_auth)
    if not 40 <= s.max_datagram <= 165:
        raise ValueError("bridge.max_datagram must be within 40..165 (MeshCore GRP_DATA limit)")
    return c
