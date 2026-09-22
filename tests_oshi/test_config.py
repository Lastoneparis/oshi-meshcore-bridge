import os

from oshi_bridge import config

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "oshi_bridge.ini.example")


def test_example_config_loads():
    c = config.load(EXAMPLE)
    assert c.mesh_channel_name == "OSHI"
    assert c.mc_channel_name == "#oshi-bridge" and len(c.mc_secret) == 16
    assert c.data_type == 0xFF4F
    assert c.bridge.max_datagram == 160
    assert c.bridge.mesh_lora.sf == 11 and c.bridge.mc_lora.bw_khz == 62.5


def test_bad_secret_rejected(tmp_path):
    p = tmp_path / "c.ini"
    p.write_text("[meshcore]\nchannel_secret_hex = abcd\n")
    try:
        config.load(str(p))
    except ValueError:
        return
    raise AssertionError("expected ValueError")
