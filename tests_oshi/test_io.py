"""Radio adapters against fake meshtastic / meshcore objects (no hardware)."""

import asyncio
import hashlib
import types

import pytest
from meshcore import EventType
from meshcore.events import Event

from oshi_bridge.meshcore_io import MeshCoreLink, channel_data_command
from oshi_bridge.meshtastic_io import MeshtasticLink, find_channel_index, packet_fields

SECRET = hashlib.sha256(b"#oshi-bridge").digest()[:16]


class FakeCommands:
    def __init__(self, queue, channel=None, send_result=EventType.OK):
        self.sent = []
        self.queue = list(queue)  # events returned to CMD_SYNC_NEXT_MESSAGE
        self.channel = channel
        self.set_calls = []
        self.send_result = send_result
        self.sink = None

    async def send(self, data, expected):
        self.sent.append(bytes(data))
        if data[0] == 0x0A:
            if not self.queue:
                return Event(EventType.NO_MORE_MSGS, {})
            ev = self.queue.pop(0)
            assert ev.type in expected
            if ev.type == EventType.CHANNEL_DATA_RECV:
                self.sink(ev)  # the real dispatcher notifies subscribers too
            return ev
        return Event(self.send_result, {})

    async def get_channel(self, idx):
        return self.channel

    async def set_channel(self, idx, name, secret):
        self.set_calls.append((idx, name, secret))
        return Event(EventType.OK, {})


class FakeMeshCore:
    def __init__(self, commands):
        self.commands = commands
        self.subs = {}
        self.disconnected = False

    def subscribe(self, et, cb, attribute_filters=None):
        self.subs[et] = cb
        if et == EventType.CHANNEL_DATA_RECV:
            self.commands.sink = cb
        return (et, cb)

    def unsubscribe(self, s):
        self.subs.pop(s[0], None)

    async def disconnect(self):
        self.disconnected = True


def link(**kw):
    args = dict(transport="serial", port="/dev/null-fake", baud=115200, host=None, tcp_port=5000, ble_address=None,
                channel_idx=7, channel_name="#oshi-bridge", channel_secret=SECRET, data_type=0xFF4F)
    args.update(kw)
    return MeshCoreLink(**args)


def data_event(payload: bytes, idx=7, dt=0xFF4F):
    return Event(EventType.CHANNEL_DATA_RECV, {
        "channel_idx": idx, "data_type": dt, "data_len": len(payload), "payload": payload.hex(), "SNR": 5.0, "path_len": 2,
    })


def test_channel_data_command_bytes():
    assert channel_data_command(1, 0xFFFF, b"\xa1\xb2\xc3") == bytes.fromhex("3e01ffffffa1b2c3")  # doc example
    assert channel_data_command(7, 0xFF4F, b"OB") == bytes.fromhex("3e07ff4fff") + b"OB"


def test_open_configures_channel_drains_and_filters():
    async def go():
        got = []
        cmds = FakeCommands([data_event(b"OB-one"), data_event(b"other-app", dt=0xFFFF),
                             data_event(b"other-channel", idx=0), data_event(b"OB-two")])
        mc = FakeMeshCore(cmds)
        lk = link()
        await lk.open(got.append, mc=mc)
        assert cmds.set_calls == [(7, "#oshi-bridge", SECRET)]  # slot was empty
        assert got == [b"OB-one", b"OB-two"]
        assert cmds.sent.count(b"\x0a") == 5  # four messages + NO_MORE
        await lk.send(b"OBxyz")
        assert cmds.sent[-1] == bytes.fromhex("3e07ff4fff") + b"OBxyz"
        await lk.close()
        assert mc.disconnected
    asyncio.run(go())


def test_existing_matching_channel_is_left_alone():
    async def go():
        info = Event(EventType.CHANNEL_INFO, {"channel_idx": 7, "channel_name": "#oshi-bridge", "channel_secret": SECRET})
        cmds = FakeCommands([], channel=info)
        await link().open(lambda d: None, mc=FakeMeshCore(cmds))
        assert cmds.set_calls == []
    asyncio.run(go())


def test_send_error_raises():
    async def go():
        cmds = FakeCommands([], send_result=EventType.ERROR)
        lk = link(configure_channel=False)
        await lk.open(lambda d: None, mc=FakeMeshCore(cmds))
        with pytest.raises(RuntimeError):
            await lk.send(b"OB")
    asyncio.run(go())


def test_packet_fields_from_meshtastic_dict():
    p = {"from": 0x0A0A0A0A, "to": 0xFFFFFFFF, "channel": 1, "pkiEncrypted": False,
         "decoded": {"portnum": "PRIVATE_APP", "payload": b"OS\x11rest"}}
    assert packet_fields(p) == (0x0A0A0A0A, 0xFFFFFFFF, 256, b"OS\x11rest", False)
    assert packet_fields({"from": 1, "to": 2, "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hi"}}) is None
    assert packet_fields({"from": 1, "to": 2, "encrypted": b"xx"}) is None  # not decodable by the bridge radio


def test_find_channel_index():
    def ch(i, role, name):
        return types.SimpleNamespace(index=i, role=role, settings=types.SimpleNamespace(name=name))

    node = types.SimpleNamespace(channels=[ch(0, 1, ""), ch(1, 0, "OSHI"), ch(2, 2, "OSHI")])
    assert find_channel_index(node, "OSHI") == 2  # index 1 is DISABLED
    assert find_channel_index(types.SimpleNamespace(channels=[]), "OSHI") is None


def test_meshtastic_send_and_receive_filtering():
    calls = []
    iface = types.SimpleNamespace(
        sendData=lambda payload, **kw: calls.append((payload, kw)),
        nodesByNum={0x0A0A0A0A: {"user": {"publicKey": "abc"}}, 0x0B0B0B0B: {"user": {}}},
    )
    lk = MeshtasticLink("serial", None, None, "OSHI")
    lk.iface, lk.channel_index = iface, 2
    got = []
    lk._sink = lambda *a: got.append(a)
    lk.send(b"OS\x11data", 0xFFFFFFFF)
    lk.send(b"OS\x14rcpt", 0x0A0A0A0A)
    assert calls[0][1] == dict(destinationId=0xFFFFFFFF, portNum=256, wantAck=False, channelIndex=2)
    assert calls[1][1]["wantAck"] is True and calls[1][1]["destinationId"] == 0x0A0A0A0A
    lk._on_receive({"from": 5, "to": 0xFFFFFFFF, "decoded": {"portnum": "PRIVATE_APP", "payload": b"OS\x11x"}}, iface)
    lk._on_receive({"from": 5, "to": 0xFFFFFFFF, "decoded": {"portnum": "PRIVATE_APP", "payload": b"zz"}}, iface)
    lk._on_receive({"from": 5, "to": 0xFFFFFFFF, "decoded": {"portnum": "PRIVATE_APP", "payload": b"OS\x11x"}}, object())
    assert got == [(5, 0xFFFFFFFF, 256, b"OS\x11x", False)]
    assert lk.peer_has_key(0x0A0A0A0A) and not lk.peer_has_key(0x0B0B0B0B) and not lk.peer_has_key(1)
    assert lk.node_known(0x0B0B0B0B) and not lk.node_known(1)
