"""試験用の方形波（CSR la_testgen。取り込みのクロックが速い構成 fprobe_pynq_cap と、垂れ流しの fprobe_pynq_deep にある）の操作:
LogicAnalyzer.testgen / testgen_off（偽のデバイス。ハードウェア不要）。設定の順序（周期・high を先に、有効化を最後）と、
範囲外の値・ケイパビリティの無い構成の断りを確かめる。判定は構成の名前でなくケイパビリティ la_testgen。"""

import pytest

from fprobe import generated as g
from fprobe.la import LogicAnalyzer
from caps_fakes import FakeDeviceBase, caps_for


class Dev(FakeDeviceBase):
    """cap_ratio = 3: 内蔵バッファを 300 MHz で取り込む構成（fprobe_pynq_cap）。
    cap_ratio = 1 かつ mem_bytes > 0: 深いバッファ（fprobe_pynq_deep。刻むクロックは sys）。"""

    def __init__(self, *, cap_ratio=3, la_testgen=True, deep=False):
        self.writes = []
        self.caps = caps_for(la_channels=16, la_depth=0 if deep else 8192, la_sample_hz=100_000_000 * cap_ratio,
                             sys_clock_hz=100_000_000, mem_bytes=(32 << 20) if deep else 0, la_testgen=la_testgen)

    def read32(self, address):
        return 1 << 20                                 # DeepAnalyzer の生成時に読むリングの拍数だけ

    def write32(self, address, value):
        self.writes.append((address, value))


def test_testgen_sets_period_and_high_then_enables_and_returns_the_frequency():
    dev = Dev()
    freq = LogicAnalyzer(dev).testgen(period=3, high=1)
    assert dev.writes == [(g.LA_TESTGEN_PERIOD, 3), (g.LA_TESTGEN_HIGH, 1), (g.LA_TESTGEN_CONTROL, 1)]
    assert freq == 100_000_000                                  # 300 MHz / 3


def test_testgen_in_the_deep_design_is_clocked_by_sys():
    # 取り込みの比は 1（sys と同じ）。方形波を刻むクロックは sys（100 MHz）なので、周波数 = sys / period
    dev = Dev(cap_ratio=1, deep=True)
    freq = LogicAnalyzer(dev).testgen(period=4, high=2)
    assert dev.writes == [(g.LA_TESTGEN_PERIOD, 4), (g.LA_TESTGEN_HIGH, 2), (g.LA_TESTGEN_CONTROL, 1)]
    assert freq == 25_000_000
    LogicAnalyzer(dev).testgen_off()
    assert dev.writes[-1] == (g.LA_TESTGEN_CONTROL, 0)


def test_testgen_edge_bit_and_off():
    dev = Dev()
    la = LogicAnalyzer(dev)
    la.testgen(period=2, high=1, edge=1)
    assert dev.writes[-1] == (g.LA_TESTGEN_CONTROL, 1 | 1 << g.LA_TESTGEN_CONTROL_EDGE[0])
    la.testgen_off()
    assert dev.writes[-1] == (g.LA_TESTGEN_CONTROL, 0)         # 駆動をやめる（Hi-Z）


@pytest.mark.parametrize("period, high", [(1, 1), (0, 0), (4096, 1), (65536, 1), (5, 0), (5, 5), (5, 6)])
def test_testgen_refuses_values_the_hardware_would_silently_round(period, high):
    dev = Dev()
    with pytest.raises(ValueError):
        LogicAnalyzer(dev).testgen(period=period, high=high)
    assert dev.writes == []                                    # 何も書かない


def test_testgen_refuses_a_bad_edge():
    with pytest.raises(ValueError, match="edge"):
        LogicAnalyzer(Dev()).testgen(period=3, high=1, edge=2)


@pytest.mark.parametrize("dev", [Dev(cap_ratio=1, la_testgen=False), Dev(cap_ratio=3, la_testgen=False),
                                 Dev(cap_ratio=1, la_testgen=False, deep=True)], ids=["sys", "fast_clock", "deep"])
def test_testgen_is_refused_without_the_capability_whatever_the_clock_ratio(dev):
    # 判定はケイパビリティ。取り込みの比が 3 でも、ケイパビリティが無ければ断る（黙って番地に書かない）
    with pytest.raises(RuntimeError, match="la_testgen"):
        LogicAnalyzer(dev).testgen(period=3, high=1)
    with pytest.raises(RuntimeError, match="la_testgen"):
        LogicAnalyzer(dev).testgen_off()
    assert dev.writes == []
