"""解読器の追加と積み重ね（U20）の実機試験: Primer 20K + Nucleo F401RE。

- JTAG: pyOCD を JTAG（鎖 [4, 5]）で繋ぎ、メモリを読むときの線を捕捉して jtag / jtag_arm で読む。
  J12 の割り当て: TCK = ch0（SWCLK, PA14）、TDI = ch1（PA15）、TMS = ch3（SWDIO, PA13）、TDO = ch6（SWO のピン, PB3）。
- Modbus RTU: USART1（PA9 = TX → ch5）をレジスタで動かし、19200 bps・偶数パリティで Modbus の要求を送って
  uart の上の modbus で読む（Nucleo にはファームを入れず、コアを止めたまま）。
- .sr: 実機の捕捉を la_export で書き、la_load で読み戻して同じ解読になること（PulseView で開けるかは手で確かめる:
  `pulseview <path>` で 10 チャンネルが見え、UART のデコーダを ch5 に付けると "01 03 …" が読める）。

CAN・1-Wire・I2S・SPI Flash・24 系 EEPROM は手持ちのターゲットに相手の部品が無いので、ここでは試さない
（合成した波形の単体試験 host/tests/test_decode_*.py で確かめている）。
"""

import time

import pytest

from fprobe.decode.modbus import modbus_crc
from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
RCC_AHB1ENR, RCC_APB2ENR = 0x40023830, 0x40023844
GPIOA_MODER, GPIOA_AFRH = 0x40020000, 0x40020024
USART1_SR, USART1_DR, USART1_BRR, USART1_CR1 = 0x40011000, 0x40011004, 0x40011008, 0x4001100C
JTAG = {"tck": 0, "tms": 3, "tdi": 1, "tdo": 6}
STM32_CHAIN = [4, 5]


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    yield p
    p.target_connect("stm32f401xe", frequency_hz=4_000_000, connect_mode="attach")     # SWD に戻す
    p.target_reset()
    p.close()


def test_jtag_scans_and_arm_dp_accesses_are_decoded(probe):
    probe.target_connect("stm32f401xe", frequency_hz=1_000_000, jtag_chain=STM32_CHAIN)
    probe.la_arm(rate_hz=10e6, pretrigger=256, fall=1 << JTAG["tck"])        # 819 µs: 16 バイトの読み（約 300 µs）が収まる
    probe.read_memory(0x20000000, 16)
    probe.la_collect(timeout_s=3)
    jtag = probe.la_decode("jtag", {**JTAG, "initial_state": "run_test_idle"}, limit=1000)
    assert jtag["counts"].get("dr", 0) >= 4 and jtag["errors"] == 0, jtag["counts"]
    arm = probe.la_decode("jtag_arm", {"jtag": {**JTAG, "initial_state": "run_test_idle"}}, limit=1000)
    texts = [a["text"] for a in arm["annotations"] if a["error"] is None]
    assert any(t.startswith("AP W 0x4 = 0x20000000") for t in texts), texts     # TAR
    assert sum("AP R 0xC" in t for t in texts) >= 4, texts                        # DRW を 4 回


def test_modbus_request_over_usart1_is_decoded(probe):
    probe.target_connect("stm32f401xe", frequency_hz=4_000_000)
    probe.target_reset(halt=True)
    t = probe.target
    t.write32(RCC_AHB1ENR, t.read32(RCC_AHB1ENR) | 1)
    t.write32(RCC_APB2ENR, t.read32(RCC_APB2ENR) | (1 << 4))
    t.write32(GPIOA_MODER, (t.read32(GPIOA_MODER) & ~(0xF << 18)) | (2 << 18) | (2 << 20))
    t.write32(GPIOA_AFRH, (t.read32(GPIOA_AFRH) & ~0xFF0) | (7 << 4) | (7 << 8))
    t.write32(USART1_CR1, 0)
    t.write32(USART1_BRR, round(16e6 / 19_200))
    t.write32(USART1_CR1, (1 << 13) | (1 << 12) | (1 << 10) | (1 << 3))             # 9 ビット（8 + パリティ）・偶数
    time.sleep(0.01)
    body = bytes([0x11, 0x03, 0x00, 0x6B, 0x00, 0x03])
    crc = modbus_crc(body)
    frame = body + bytes([crc & 0xFF, crc >> 8])
    # SWD で 1 バイトずつ送るので、ホストが忙しいとバイトの間が空く: 窓を約 41 ms に、区切りの間を 60 文字分に
    probe.la_arm(rate_hz=2e5, pretrigger=64, fall=1 << 5)
    for b in frame:
        while not t.read32(USART1_SR) & (1 << 7):
            pass
        t.write32(USART1_DR, b)
    t.flush()
    probe.la_collect(timeout_s=3)
    out = probe.la_decode("modbus", {"uart": {"channel": 5, "baud": 19_200, "parity": "even"}, "gap_chars": 60})
    (req,) = out["annotations"]
    assert req["kind"] == "request" and req["fields"]["crc_ok"], req
    assert (req["fields"]["slave"], req["fields"]["start"], req["fields"]["count"]) == (0x11, 0x6B, 3)


def test_real_capture_round_trips_through_sigrok(probe, tmp_path):
    first = probe.la_decode("uart", {"channel": 5, "baud": 19_200, "parity": "even"})
    info = probe.la_export(str(tmp_path / "modbus.sr"), names={"5": "usart1_tx"})
    assert info["samples"] == first["capture"]["length"]
    loaded = probe.la_load(str(tmp_path / "modbus.sr"))
    assert loaded["names"]["5"] == "usart1_tx"
    again = probe.la_decode("uart", {"channel": 5, "baud": 19_200, "parity": "even"})
    assert [a["value"] for a in again["annotations"]] == [a["value"] for a in first["annotations"]]
