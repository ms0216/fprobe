"""Probe の道具 gpio.* と spi.transfer（本体の汎用の API）。偽の機器と偽の SPI の相手で、API の名前（ProbeCaller）から。"""

from types import SimpleNamespace

import pytest

from fprobe.api.spec import InvalidParams
from fprobe.mcp_server import Probe
from fprobe.plugins import ProbeCaller
from spi_fakes import GpioDevice, SpiSlave


class Counter(SpiSlave):
    def selected_begin(self):
        self.n = 0x10
        return self.n

    def received(self, word):
        self.n += 1
        return self.n


@pytest.fixture
def setup():
    slave = Counter(sck=0, mosi=1, miso=6, cs=3)
    device = GpioDevice([slave])
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=device)
    return ProbeCaller(probe), probe, device, slave


def test_gpio_set_read_release(setup):
    api, _, device, _ = setup
    st = api.call("gpio.set", drive=0b100, enable=0b100, mask=0b100)
    assert (st["drive"], st["enable"]) == (0b100, 0b100) and st["level"] & 0b100
    assert api.call("gpio.read")["enable"] == 0b100
    assert api.call("gpio.release", mask=0b100)["enable"] == 0
    assert device.enable == 0


def test_gpio_sequence_returns_the_levels(setup):
    api, _, _, _ = setup
    api.call("gpio.set", drive=0, enable=0b1_0000, mask=0b1_0000)
    out = api.call("gpio.sequence", states=[0b1_0000, 0b1_0000, 0, 0])
    assert [v & 0b1_0000 for v in out["levels"]][1::2] == [0b1_0000, 0]


def test_spi_transfer_in_hex_words(setup):
    api, _, device, slave = setup
    out = api.call("spi.transfer", sck=0, mosi=1, miso=6, cs=3, data="9f00", read_length=3)
    assert out["received"] == "1011121314" and out["words"] == 5
    assert slave.words == [[0x9F, 0x00, 0xFF, 0xFF, 0xFF]]
    assert out["states"] == device.exchanged_words
    slave.bits = 12                                                        # 相手も 12 ビットの語
    wide = api.call("spi.transfer", sck=0, mosi=1, miso=6, cs=3, data="0abc", bits=12, mode=0)
    assert wide["received"] == "0010" and slave.words[-1] == [0xABC]      # 12 ビットの語 = 2 バイト（上位から）


def test_spi_transfer_checks_its_arguments(setup):
    api, _, _, _ = setup
    with pytest.raises(ValueError, match="16 進"):
        api.call("spi.transfer", sck=0, mosi=1, miso=6, cs=3, data="abc")       # 1 語 = 1 バイトに足りない
    with pytest.raises(InvalidParams):
        api.call("spi.transfer", sck=0, mosi=1, miso=6, cs=3, mode=4)
    with pytest.raises(ValueError, match="重なる"):
        api.call("spi.transfer", sck=0, mosi=0, miso=6, cs=3, data="00")


def test_driving_pins_is_refused_while_a_target_is_connected(setup):
    api, probe, _, _ = setup
    probe._debug = object()
    assert api.call("gpio.read")["enable"] == 0                                 # 読むだけはよい
    for method, params in (("gpio.set", {"drive": 0, "enable": 1}), ("gpio.sequence", {"states": [0]}),
                           ("spi.transfer", {"sck": 0, "mosi": 1, "miso": 6, "cs": 3, "data": "00"})):
        with pytest.raises(RuntimeError, match="接続中"):
            api.call(method, **params)
    probe._debug = None
