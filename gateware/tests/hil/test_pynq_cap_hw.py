"""PYNQ-Z2 の fprobe_pynq_cap: 内蔵ロジアナが sys の 3 倍（300 MHz）のクロックで取り込むことを、配線なしで実機で確かめる。

準備: `python -m fprobe_gw build fprobe_pynq_cap` で作ったビットストリームを PYNQ に書いておく（このテストは書かない）。
環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行する。ピンは何もつながなくてよい。

確かめること（配線なし）:
  1. ケイパビリティ: la_sample_hz = 3 × sys_clock_hz、la_cap の比 = 3。
  2. selftest（la_cap.control）: ch 9 に sys の周期ごとに反転する内部信号を入れると、同じ値が 3 サンプルずつ続く
     = 取り込みの周期は sys の周期の 1 / 3（クロックの比の実測）。
  3. sys の周波数（タイムベースとホストの時計。ホストの時計の揺れを含む）が 100 MHz に近い。
  よって取り込みの周波数 = 3 × (3 で測った sys の周波数) を、測った値として出す。
"""

import os
import time

import pytest

from fprobe import generated as g  # noqa: E402
from fprobe.la import LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]
SELFTEST_CH = 9                                    # la_pins 8 本の次の内部信号 2 本の 2 本目（sys_bit_b）


@pytest.fixture(scope="module")
def session():
    with Session.open_eth(HOST, link_up_wait_s=8) as s:
        yield s


def test_the_capture_clock_is_three_times_sys(session):
    caps = session.device.capabilities()
    assert caps.design == "fprobe_pynq_cap" and caps.la_depth
    assert caps.la_sample_hz == 3 * caps.sys_clock_hz
    info = session.device.read32(g.LA_CAP_INFO)
    assert info >> g.LA_CAP_INFO_RATIO[0] & 0xF == 3


def test_the_selftest_signal_shows_three_capture_cycles_per_sys_cycle(session):
    dev = session.device
    la = LogicAnalyzer(dev)
    dev.write32(g.LA_CAP_CONTROL, 1)
    try:
        cap = la.capture(rate_hz=None, pretrigger=64)                  # 毎サンプル、条件なし
    finally:
        dev.write32(g.LA_CAP_CONTROL, 0)
    bits = cap.channel(SELFTEST_CH)
    runs, n = [], 1
    for a, b in zip(bits, bits[1:]):
        if a == b:
            n += 1
        else:
            runs.append(n)
            n = 1
    print(f"selftest: {len(bits)} サンプル、連続の長さ {sorted(set(runs[1:]))}（最初の 1 つは途中から）")
    assert len(runs) > 100 and set(runs[1:]) == {3}, sorted(set(runs[1:]))


def test_the_sys_clock_measured_against_the_host_clock(session):
    dev = session.device
    caps = dev.capabilities()

    def now():
        lo, hi = dev.read(g.TIMEBASE_TIME_LO, 2)
        return hi << 32 | lo
    now()
    t0, c0 = time.perf_counter(), now()
    time.sleep(2.0)
    t1, c1 = time.perf_counter(), now()
    sys_hz = (c1 - c0) / (t1 - t0)
    print(f"sys = {sys_hz / 1e6:.4f} MHz（ケイパビリティ {caps.sys_clock_hz / 1e6:.1f} MHz）、"
          f"取り込み = 3 × sys = {3 * sys_hz / 1e6:.3f} MHz（ホストの時計で測った）")
    assert abs(sys_hz / caps.sys_clock_hz - 1) < 0.01
