"""Primer 20K + Nucleo: ロジアナの深いバッファ（DDR3、M8 第 2 段）。pyOCD の SWD 通信を 100 MS/s で捕捉し、
USB の拡張ストリームで読み戻す。RAW と変化点圧縮（RLE）。"""

import time

import pytest

pytest.importorskip("pyocd")
from tests.hil.program import program  # noqa: E402
from fprobe.la import DeepAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402
from fprobe_gw.boards.primer20k import PRIMER20K  # noqa: E402
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL  # noqa: E402

pytestmark = pytest.mark.hil

PINS = PRIMER20K.debug_connector
PROBE_DRIVES_SWDIO = 8


@pytest.fixture(scope="module")
def session():
    program("fprobe")
    with Session.open(STRINGS[STR_SERIAL - 1]) as s:
        yield s


def swd_traffic(session):
    with session.debug(target_override="stm32f401xe", frequency=4_000_000, connect_mode="attach") as debug:
        for _ in range(20):
            debug.target.read32(0x20004000)


@pytest.mark.parametrize("rle", [False, True])
def test_captures_swd_traffic_deeply(session, rle):
    la = DeepAnalyzer(session.device)
    period = la.start(rle=rle, fall=1 << PROBE_DRIVES_SWDIO, post_beats=20_000)
    assert period == pytest.approx(1 / PRIMER20K.sync_pll.fout_hz)
    swd_traffic(session)
    la.wait(5)
    if rle:
        # 変化点のまま読む（毎サンプルに展開すると長い記録はホストのメモリに収まらない）
        tr = la.read_transitions(before_beats=2_000)
        assert tr.level_at(PROBE_DRIVES_SWDIO, tr.trigger - 1) == 1 and tr.level_at(PROBE_DRIVES_SWDIO, tr.trigger) == 0
        assert tr.length > 100 * 22_001 * 8                   # RAW の 100 倍以上の時間
        assert len(tr.edges(PINS["swclk"])) > 2000           # 20 回の読み出しの SWD 通信がすべて入る
        return
    cap = la.read(before_beats=2_000)
    drives = cap.channel(PROBE_DRIVES_SWDIO)
    assert drives[cap.trigger - 1] == 1 and drives[cap.trigger] == 0
    swclk = cap.channel(PINS["swclk"])
    assert len(cap.samples) == 22_001 * 8
    assert sum(1 for a, b in zip(swclk, swclk[1:]) if a and not b) >= 40


def test_reading_speed(session):
    la = DeepAnalyzer(session.device)
    la.start(post_beats=1 << 20)                          # 条件なし = すぐトリガ。後ろに 16 MB
    la.wait(5)
    t0 = time.monotonic()
    cap = la.read(before_beats=0)
    seconds = time.monotonic() - t0
    rate = (1 << 20) * 16 / seconds
    print(f"deep buffer read: {rate / 1e6:.1f} MB/s (with decoding)")
    assert len(cap.samples) == ((1 << 20) + 1) * 8 and rate > 10e6
