"""Primer 20K: 汎用の SPI マスタ（gpio のビットバング + CONTROL の EXCHANGE）とプラグイン spiflash（U42。仕様
docs/superpowers/specs/2026-10-19-u42-spi-flash-design.md §7）。

書き換えてよい 3.3 V の SPI NOR Flash（単体の基板。例: W25Q32 の DIP / SOP 変換基板）をつないだときだけ走らせる:
FPROBE_SPIFLASH=1。Flash の最後の 64 KB を消して書く（ほかは触らない）。

配線（**Primer の USB をつないでから** Flash の基板をつなぐ。Flash の VCC は Primer の 3.3 V から取る。ほかの電源から
給電しない = 電源の無い Flash の入力に 3.3 V をかけない）:
- J12-5（pmod1[0]、ch0、TCK）→ CLK、J12-6（pmod1[1]、ch1、TDI）→ DI（IO0）、J12-11（pmod1[6]、ch6、TDO）← DO（IO1）、
  J12-8（pmod1[3]、ch3、TMS）→ /CS、J12 の 1〜4 番の 3.3 V → VCC・/WP・/HOLD、GND → GND（シルクで確かめる）。
- 線は短く（10 cm 程度）。化けたら stretch を上げる。
"""

import os
import random

import pytest

from fprobe.mcp_server import Probe
from fprobe.plugins import ProbeCaller
from fprobe.plugins.spiflash import SpiFlash
from tests.hil.program import SERIAL, program

pytestmark = [pytest.mark.hil,
              pytest.mark.skipif(os.environ.get("FPROBE_SPIFLASH") != "1",
                                 reason="書き換えてよい SPI Flash をつないだときだけ（FPROBE_SPIFLASH=1）")]


@pytest.fixture(scope="module")
def flash():
    program("fprobe")
    probe = Probe(SERIAL)
    try:
        with SpiFlash(ProbeCaller(probe)) as f:
            yield f
    finally:
        probe.close()


def test_identifies_the_chip(flash):
    ident = flash.jedec_id()
    assert ident.manufacturer not in (0x00, 0xFF)
    geo = flash.geometry
    print(f"JEDEC {ident}、{geo.size // 1024} KiB、ページ {geo.page_size}、消去 {geo.erase_types}（{geo.source}）")
    assert geo.size >= 64 * 1024


def test_write_read_verify_the_last_block(flash):
    geo = flash.geometry
    flash.unprotect()
    start = geo.size - 65536
    data = bytes(random.Random(42).getrandbits(8) for _ in range(65536))
    result = flash.write(start, data)
    print(result)
    assert result["verified"]
    assert flash.read(start + 1000, 3000) == data[1000:4000]
    import time
    t = time.perf_counter()
    flash.read(start, 65536)
    print(f"読み出し {65536 / 1024 / (time.perf_counter() - t):.1f} KiB/s")
