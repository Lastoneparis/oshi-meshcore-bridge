import pytest

from oshi_bridge import wire


def test_small_frame_single_part():
    parts = wire.split(b"OS" + b"a" * 20, bridge=0x1234, seq=5)
    assert len(parts) == 1
    p = wire.parse(parts[0])
    assert (p.bridge, p.seq, p.part, p.total, p.chunk) == (0x1234, 5, 0, 1, b"OS" + b"a" * 20)
    assert wire.Joiner().push(p) == b"OS" + b"a" * 20


def test_200_byte_frame_needs_two_datagrams_within_limit():
    frame = bytes(range(200))
    parts = wire.split(frame, bridge=1, seq=9, max_datagram=160)
    assert len(parts) == 2 and all(len(p) <= 160 for p in parts)
    j = wire.Joiner()
    assert j.push(wire.parse(parts[1])) is None  # out of order
    assert j.push(wire.parse(parts[0])) == frame


def test_joiner_expiry_and_separation():
    t = [0.0]
    j = wire.Joiner(timeout_s=10, clock=lambda: t[0])
    a = wire.split(bytes(200), 1, 1)
    b = wire.split(bytes([1]) * 200, 2, 1)  # same seq, other bridge
    assert j.push(wire.parse(a[0])) is None
    assert j.push(wire.parse(b[0])) is None
    assert j.push(wire.parse(b[1])) == bytes([1]) * 200
    t[0] = 20
    assert j.push(wire.parse(a[1])) is None  # its first half expired


def test_parse_rejects_foreign_datagrams():
    assert wire.parse(b"hello world") is None
    assert wire.parse(b"OB" + bytes([0x20]) + bytes(6)) is None  # version 2
    assert wire.parse(b"OB\x10" + bytes(4) + b"\x00\x21") is None  # part >= total


def test_split_rejects_tiny_datagram():
    with pytest.raises(ValueError):
        wire.split(b"x", 1, 1, max_datagram=9)
