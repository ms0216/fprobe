"""拡張ヘッダの汎用入出力（fprobe.gpio）と、その上のビットバングの SPI マスタ（fprobe.spi）。偽の機器と偽の相手で。"""

import pytest

from fprobe import generated as g
from fprobe.gpio import ExchangeUnsupported, Gpio
from fprobe.spi import SpiConfig, SpiMaster, SpiPins
from spi_fakes import GpioDevice, SpiSlave


class Echo(SpiSlave):
    """受け取った語に 0x5A を xor して次に返す（最初の語は 0x3C）。"""

    def selected_begin(self):
        return 0x3C

    def received(self, word):
        return word ^ 0x5A


def test_set_keeps_the_other_pins_and_writes_drive_before_enable():
    dev = GpioDevice()
    gpio = Gpio(dev)
    gpio.set(drive=0b0000_0100, enable=0b0000_0100, mask=0b0000_0100)
    st = gpio.set(drive=0b0000_0001, enable=0b0000_0011, mask=0b0000_0011)
    assert (st.drive, st.enable) == (0b0000_0101, 0b0000_0111)
    assert st.level & 0b111 == 0b101
    assert gpio.release(0b0000_0001).enable == 0b0000_0110
    assert gpio.release().enable == 0


def test_sequence_uses_one_exchange_per_call_and_needs_protocol_2():
    dev = GpioDevice()
    gpio = Gpio(dev)
    gpio.set(drive=0, enable=0x0F, mask=0xFF)
    assert gpio.sequence([1, 2, 3, 3])[-1] & 0x0F == 3
    assert dev.exchange_calls == 1
    old = Gpio(GpioDevice(protocol_version=1))
    with pytest.raises(ExchangeUnsupported, match="プロトコル 2"):
        old.sequence([1])


@pytest.mark.parametrize("lag", [0, 1])
@pytest.mark.parametrize("mode", [0, 1, 2, 3])
@pytest.mark.parametrize("bits,lsb_first", [(8, False), (8, True), (12, False), (1, False), (32, True)])
def test_transfer_is_full_duplex_in_every_mode(lag, mode, bits, lsb_first):
    slave = Echo(mode=mode, bits=bits, lsb_first=lsb_first)
    dev = GpioDevice([slave], lag=lag)
    spi = SpiMaster(Gpio(dev), SpiPins(sck=0, mosi=1, miso=6, cs=3), SpiConfig(mode=mode, bits=bits,
                                                                              lsb_first=lsb_first))
    mask = (1 << bits) - 1
    words = [0x9F & mask, 0x0123_4567 & mask, mask, 0]
    received = spi.transfer(words, read_count=2)
    assert slave.words == [words + [mask, mask]]                          # 読む間は MOSI = 1（既定の埋め）
    assert received == [0x3C & mask] + [(w ^ 0x5A) & mask for w in words + [mask]]
    assert dev.level() >> 3 & 1 == 1 and dev.level() & 1 == mode >> 1     # CS は戻り、SCK は休止の値


def test_keep_cs_continues_the_same_selection():
    slave = Echo()
    dev = GpioDevice([slave])
    spi = SpiMaster(Gpio(dev), SpiPins(sck=0, mosi=1, miso=6, cs=3))
    spi.transfer([0x01], keep_cs=True)
    assert dev.level() >> 3 & 1 == 0
    spi.transfer([0x02, 0x03])
    assert slave.words == [[0x01, 0x02, 0x03]]


def test_stretch_repeats_each_state_and_other_pins_are_untouched():
    slave = Echo()
    dev = GpioDevice([slave])
    gpio = Gpio(dev)
    gpio.set(drive=0b0000_0000, enable=0b0000_0100, mask=0b0000_0100)    # nRESET を Low に保っている
    plain = SpiMaster(gpio, SpiPins(sck=0, mosi=1, miso=6, cs=3))
    plain.transfer([0xA5])
    n0 = dev.exchanged_words
    slow = SpiMaster(gpio, SpiPins(sck=0, mosi=1, miso=6, cs=3), SpiConfig(stretch=2))
    assert slow.transfer([0xA5]) == [0x3C]
    assert dev.exchanged_words - n0 == 3 * n0
    st = gpio.state()
    assert st.enable & 0b100 and not st.drive & 0b100                    # nRESET は Low のまま
    assert not st.enable >> 6 & 1                                         # MISO は入力


def test_states_per_bit_for_the_speed_estimate():
    dev = GpioDevice([Echo()])
    SpiMaster(Gpio(dev), SpiPins(sck=0, mosi=1, miso=6, cs=3)).transfer([0] * 100)
    assert dev.exchanged_words == 2 * 8 * 100 + 3                         # 1 ビット 2 状態 + CS の前後


def test_pins_are_checked():
    with pytest.raises(ValueError, match="重なる"):
        SpiPins(sck=0, mosi=0, miso=6, cs=3)
    with pytest.raises(ValueError, match="0〜7"):
        SpiPins(sck=8, mosi=1, miso=6, cs=3)
    with pytest.raises(ValueError, match="mode"):
        SpiConfig(mode=4)
    with pytest.raises(ValueError, match="語長"):
        SpiConfig(bits=33)
    spi = SpiMaster(Gpio(GpioDevice()), SpiPins(sck=0, mosi=None, miso=6, cs=None))   # MOSI・CS 無しも使える
    assert spi.transfer([], read_count=1) == [0xFF]                       # 誰も駆動しない MISO はプルアップ
    with pytest.raises(ValueError, match="MOSI"):
        spi.transfer([0x12])
    assert g.GPIO_IO == g.GPIO_BASE + 0x0C
