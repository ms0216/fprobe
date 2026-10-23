"""通信の解読（la.decode）の実機試験: Nucleo F401RE の USART1（PA9 = TX → ロジアナ ch5）と、SWD の線（ch0 = SWCLK、
ch3 = SWDIO、ch8 = 駆動の向き）。Nucleo にはファームを入れず、コアを止めたまま USART1 をレジスタで動かす。"""

import time

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
RCC_AHB1ENR, RCC_APB2ENR = 0x40023830, 0x40023844
GPIOA_MODER, GPIOA_AFRH = 0x40020000, 0x40020024
USART1_SR, USART1_DR, USART1_BRR, USART1_CR1 = 0x40011000, 0x40011004, 0x40011008, 0x4001100C


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.target_reset(halt=True)
    yield p
    p.target_reset()
    p.close()


def test_uart_bytes_and_baud_are_decoded(probe):
    t = probe.target
    t.write32(RCC_AHB1ENR, t.read32(RCC_AHB1ENR) | 1)
    t.write32(RCC_APB2ENR, t.read32(RCC_APB2ENR) | (1 << 4))
    t.write32(GPIOA_MODER, (t.read32(GPIOA_MODER) & ~(0xF << 18)) | (2 << 18) | (2 << 20))     # PA9 / PA10 を AF
    t.write32(GPIOA_AFRH, (t.read32(GPIOA_AFRH) & ~0xFF0) | (7 << 4) | (7 << 8))               # AF7 = USART1
    t.write32(USART1_CR1, 0)
    t.write32(USART1_BRR, round(16e6 / 115_200))
    t.write32(USART1_CR1, (1 << 13) | (1 << 3) | (1 << 2))
    time.sleep(0.01)
    probe.la_arm(rate_hz=1e6, pretrigger=64, fall=1 << 5)          # 約 8 ms（SWD で 1 バイトずつ送るので間が空く）
    for b in b"Hello":
        while not t.read32(USART1_SR) & (1 << 7):                                              # TXE
            pass
        t.write32(USART1_DR, b)
    t.flush()
    probe.la_collect(timeout_s=3)
    d = probe.la_decode("uart", {"channel": 5, "baud": 115_200})               # ボーは推定しない（必須）
    assert d["errors"] == 0, d["params"]
    data = bytes(a["value"] for a in d["annotations"] if a["kind"] == "data")
    assert data.startswith(b"Hello"[:len(data)]) and len(data) >= 3, data                       # 窓に入った分


def test_swd_transfers_match_the_host_read(probe):
    probe.la_arm(rate_hz=100e6, pretrigger=64, fall=1 << 0)
    probe.read_memory(0x20000000, 16)
    probe.la_collect(timeout_s=3)
    d = probe.la_decode("swd", {"swclk": 0, "swdio": 3, "drive": 8}, limit=100)
    transfers = [a["text"] for a in d["annotations"] if a["kind"] == "transfer" and not a["text"].endswith("WAIT")]   # WAIT はやり直し
    # 16 バイトの読み: TAR を書き、DRW を 4 回（ポスト: 値は 1 つ後で返る）、最後に RDBUFF
    assert transfers[0].startswith("AP0 W TAR 0x20000000 OK"), transfers
    assert [x.split()[2] for x in transfers[1:5]] == ["DRW"] * 4 and "RDBUFF" in transfers[5], transfers
