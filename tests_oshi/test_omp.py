from oshi_bridge import omp


def test_data_layout_matches_firmware():
    f = omp.DataFrame(0x11223344, 0xA1B2C3D4, 0x01020304, 2, 3, omp.FLAG_CUSTODY_OK, b"hi")
    b = f.encode()
    # "OS", (1<<4)|1, msgId LE, origin LE, dest LE, idx, count, flags, data
    assert b == bytes.fromhex("4f5311" "44332211" "d4c3b2a1" "04030201" "02" "03" "01") + b"hi"
    assert len(b) == omp.DATA_HEADER + 2
    assert omp.decode(b) == f


def test_sack_and_notice_layout():
    s = omp.SackFrame(1, 2, 3, 0b101)
    assert s.encode() == bytes.fromhex("4f5312" "01000000" "02000000" "03" "0500000000000000")
    assert omp.decode(s.encode()) == s and not s.complete
    assert omp.SackFrame(1, 2, 3, 0b111).complete
    r = omp.NoticeFrame(omp.FrameType.RECEIPT, 7, 8, 9)
    assert r.encode() == bytes.fromhex("4f5314" "07000000" "08000000" "09000000")
    assert omp.decode(r.encode()) == r


def test_sack_bitmap_masked_to_count():
    raw = bytes.fromhex("4f5312" "01000000" "02000000" "02" "ffffffffffffffff")
    assert omp.decode(raw).bitmap == 0b11


def test_rejects_malformed():
    good = omp.DataFrame(1, 2, 3, 0, 1, 0, b"x").encode()
    assert omp.decode(b"OM" + good[2:]) is None  # legacy OSHI app framing
    assert omp.decode(good[:2] + bytes([0x21]) + good[3:]) is None  # version 2
    assert omp.decode(good[:10]) is None  # truncated
    bad_idx = bytearray(good)
    bad_idx[15] = 1  # idx == count
    assert omp.decode(bytes(bad_idx)) is None
    too_long = omp.DataFrame(1, 2, 3, 0, 1, 0, b"x" * omp.MAX_FRAG_DATA).encode() + b"y"
    assert omp.decode(too_long) is None
    assert omp.decode(bytes.fromhex("4f5315") + b"\x01\x00\x01\x05") is None  # BEACON not relayed


def test_beacon_layout():
    assert omp.BeaconFrame(omp.CAP_BRIDGE).encode() == bytes.fromhex("4f5315" "04" "0001" "00")
    assert omp.decode_beacon(bytes.fromhex("4f5315" "03" "0001" "05")) == omp.BeaconFrame(3, 0x0100, 5)
    assert omp.decode_beacon(bytes.fromhex("4f5315" "04")) is None
    assert omp.CAP_BRIDGE == 4


def test_max_frame_is_200():
    f = omp.DataFrame(1, 2, 3, 0, 1, 0, b"z" * omp.MAX_FRAG_DATA)
    assert len(f.encode()) == 200
