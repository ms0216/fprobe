"""解読器の通信の設定（プロトコルの枠の値・チャンネルの割り当て）は既定を持たず必須（利用者の判断 2026-10-24）。

CLAUDE.md の原則「境界の値を使うときは、使う側が明示して渡す引数にし、既定の暗黙の値にしない」。典型の値（8N1・
SPI のモード 0・24C02 のデバイスアドレス等）で黙って補うと、違う機器では誤りか「何も出ない」になって気づきにくい。
つながない線は None を明示して渡す（省略は「つながない」の意味にしない）。（ハードウェア不要）
"""

import pytest

from fprobe.decode import decode, get_decoder
from fprobe.decode.synth import Wave

# 解読器ごとの、省けない設定と、そろえた設定の例（値は試験の道具）
FULL = {
    "uart": {"channel": 0, "baud": 9600, "data_bits": 8, "parity": "none", "stop_bits": 1, "invert": False,
             "lsb_first": True},
    "spi": {"sck": 0, "mosi": 1, "miso": 2, "cs": 3, "mode": 0, "msb_first": True, "word_bits": 8,
            "cs_active_low": True},
    "i2c": {"scl": 0, "sda": 1},
    "swd": {"swclk": 0, "swdio": 1, "drive": None, "turnaround": 1, "orundetect": False},
    "onewire": {"channel": 0, "overdrive": False},
    "jtag": {"tck": 0, "tms": 1, "tdi": 2, "tdo": 3, "trst": None},
    "i2s": {"sck": 0, "ws": 1, "sd": 2, "format": "i2s", "word_bits": 16, "signed": True},
    "parallel": {"data": [0, 1], "clock": None, "enable": None},
    "manchester": {"channel": 0, "bitrate": 1000, "tolerance": 0.2, "convention": "ieee", "word_bits": 8,
                   "msb_first": True},
    "can": {"channel": 0, "bitrate": 500e3, "sample_point": 0.875, "invert": False},
}
STACKED = {
    "jtag_arm": ("jtag", {"ir_offset": 0, "dr_offset": 0}),
    "spiflash": ("spi", {"address_bytes": 3}),
    "eeprom24": ("i2c", {"device": 0x50, "address_bytes": 1, "block_bits": 0}),
}


def capture():
    w = Wave(channels=8, sample_rate_hz=1e6, idle={0: 1, 1: 1, 3: 1})
    return w.build(end_s=1e-3)


@pytest.mark.parametrize("name", sorted(FULL))
def test_every_setting_is_required_and_named_in_the_error(name):
    tr = capture()
    decode(name, tr, **FULL[name])                                   # そろえれば解読できる
    required = {p["name"] for p in get_decoder(name).describe()["params"] if p["required"]}
    assert set(FULL[name]) <= required
    for key in FULL[name]:
        settings = {k: v for k, v in FULL[name].items() if k != key}
        with pytest.raises(ValueError, match=key):
            decode(name, tr, **settings)


@pytest.mark.parametrize("name", sorted(STACKED))
def test_stacked_settings_are_required_too(name):
    lower, own = STACKED[name]
    tr = capture()
    decode(name, tr, **{lower: FULL[lower]}, **own)
    for key in own:
        with pytest.raises(ValueError, match=key):
            decode(name, tr, **{lower: FULL[lower]}, **{k: v for k, v in own.items() if k != key})
    some = next(iter(FULL[lower]))
    with pytest.raises(ValueError, match=some):                     # 下の解読の設定も省けない
        decode(name, tr, **{lower: {k: v for k, v in FULL[lower].items() if k != some}}, **own)


def test_functions_themselves_have_no_defaults_for_the_settings():
    from fprobe.decode import decode_eeprom24, decode_spi, decode_uart
    tr = capture()
    with pytest.raises(TypeError, match="data_bits"):
        decode_uart(tr, 0, baud=9600, parity="none", stop_bits=1, invert=False, lsb_first=True)
    with pytest.raises(TypeError, match="mode"):
        decode_spi(tr, sck=0, mosi=1, miso=None, cs=None, msb_first=True, word_bits=8, cs_active_low=True)
    lower = decode("i2c", tr, scl=0, sda=1)
    with pytest.raises(TypeError, match="device"):
        decode_eeprom24(lower, address_bytes=1, block_bits=0)


def test_lines_that_may_be_absent_say_so_and_none_is_explicit():
    params = {p["name"]: p for p in get_decoder("spi").describe()["params"]}
    assert params["cs"]["required"] and params["cs"]["nullable"]
    assert params["sck"]["required"] and not params["sck"]["nullable"]
    assert not params["mode"]["nullable"]


def test_settings_that_only_apply_with_their_line_are_required_with_it():
    tr = capture()
    base = {"data": [0, 1], "enable": None}
    with pytest.raises(ValueError, match="edge"):
        decode("parallel", tr, **base, clock=2)                    # クロックを使うなら辺が要る
    with pytest.raises(ValueError, match="edge"):
        decode("parallel", tr, **base, clock=None, edge="rising")  # クロックが無いのに辺（黙って捨てない）
    decode("parallel", tr, **base, clock=2, edge="rising")
    with pytest.raises(ValueError, match="enable_level"):
        decode("parallel", tr, data=[0, 1], clock=None, enable=3)
    decode("parallel", tr, data=[0, 1], clock=None, enable=3, enable_level=0)
    can = dict(FULL["can"])
    with pytest.raises(ValueError, match="data_sample_point"):
        decode("can", tr, **can, data_bitrate=2e6)                  # データ相のサンプル点を調停相から借りない
    with pytest.raises(ValueError, match="data_bitrate"):
        decode("can", tr, **can, data_sample_point=0.75)
    decode("can", tr, **can, data_bitrate=2e6, data_sample_point=0.75)


def test_i2s_word_bits_none_means_the_whole_slot_and_is_explicit():
    tr = capture()
    decode("i2s", tr, sck=0, ws=1, sd=2, format="i2s", word_bits=None, signed=True)
    with pytest.raises(ValueError, match="word_bits"):
        decode("i2s", tr, sck=0, ws=1, sd=2, format="i2s", signed=True)
