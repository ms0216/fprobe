"""Primer 20K + Nucleo で、内蔵ロジックアナライザが pyOCD の SWD 通信を取り込めることを確かめる（M7）。

デバッガとロジアナの一体化の確認: 同じプロセスの pyOCD（共有セッション）が Nucleo を読む間の
SWCLK / SWDIO と内部信号（プローブが SWDIO を駆動中、DAP 処理中）を、SWCLK の立ち下がりでトリガして取る。
"""

from pathlib import Path

import pytest

from tests.hil.program import program  # noqa: E402
from fprobe import Device
from fprobe.la import LogicAnalyzer, write_vcd
from fprobe.session import Session
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL
from fprobe_gw.boards.primer20k import PRIMER20K

pytestmark = pytest.mark.hil

PINS = PRIMER20K.debug_connector
PROBE_DRIVES_SWDIO, DAP_BUSY = 8, 9                       # 内部信号のチャンネル（fprobe_core）
OUT = Path(__file__).resolve().parents[3] / "build" / "la_swd.vcd"


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


def test_capabilities_announce_the_analyzer(programmed):
    with Device.open() as device:
        caps = device.capabilities()
    assert caps.la_channels == 16 and caps.la_sample_hz > 100e6
    assert caps.la_depth == 8192 or caps.mem_bytes            # 内蔵バッファか、外部メモリの深いバッファ


def test_captures_swd_traffic_of_a_debugger_session(programmed):
    # 1 つの USB ハンドルでロジアナ（拡張インターフェース）と pyOCD（CMSIS-DAP）を同時に使う
    with Session.open(STRINGS[STR_SERIAL - 1]) as session:
        la = LogicAnalyzer(session.device)
        # トリガ: プローブが SWDIO を離した瞬間（読み出しのターンアラウンド。ターゲットが ACK・データを返す）
        period = la.start(rate_hz=25e6, pretrigger=1024, fall=1 << PROBE_DRIVES_SWDIO)
        with session.debug(target_override="stm32f401xe", frequency=1_000_000) as debug:
            assert debug.target.read32(0xE000ED00) == 0x410FC241
            la.wait()
            cap = la.read(period)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    write_vcd(cap, OUT, {v: k for k, v in PINS.items()})
    swclk = cap.channel(PINS["swclk"])
    falls = sum(1 for a, b in zip(swclk, swclk[1:]) if a and not b)
    assert falls >= 40                                    # SWD のトランザクション（約 46 クロック）が 1 つ以上入っている
    drives = cap.channel(PROBE_DRIVES_SWDIO)
    assert drives[cap.trigger - 1] == 1 and drives[cap.trigger] == 0
    assert any(cap.channel(DAP_BUSY))                     # DAP コマンドの処理中


def test_unconditional_start_keeps_the_pretrigger_window(programmed):
    # 条件なし（即成立）でも、開始からプリトリガ分を記録してからトリガする（内蔵・深いバッファ共通の意味）
    with Device.open() as device:
        la = LogicAnalyzer(device)
        period = la.start(rate_hz=25e6, pretrigger=1024)
        la.wait()
        cap = la.read(period)
    assert cap.trigger == 1024 and len(cap.samples) == la.depth
