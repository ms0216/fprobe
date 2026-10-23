"""ロジアナの時刻（trigger_time）を、デバイスが申告する入り口の遅れ（ケイパビリティ LA_DELAY）で直す。機器は使わない。

FPGA はトリガのサンプルに、ピンの変化より数刻み後の時刻を付ける（同期化と符号器までの段）。読み出しはそれを引いて、
ピンの変化の時刻にする（DAP のイベント・FPGA の観測と同じ時間軸に並べるため）。"""

from fprobe import generated as g
from fprobe.caps import Capabilities
from fprobe.la import DeepAnalyzer, LogicAnalyzer

RAW_TIME = 1_000_000


class FakeDevice:
    def __init__(self, **caps):
        self.caps = Capabilities(la_channels=16, la_sample_hz=100_000_000, sys_clock_hz=100_000_000, **caps)
        self.regs = {g.LA_DEEP_RING_BEATS: 1024, g.LA_DEEP_POST_BEATS: 4, g.LA_DEEP_STATUS: 0}

    def capabilities(self):
        return self.caps

    def read(self, address, count=1):
        if address == g.LA_START_INDEX:                       # start, trigger, time_lo, time_hi
            return [0, 5, RAW_TIME, 0]
        if address == g.LA_MEM_BASE:
            return [0] * count
        if address == g.LA_DEEP_TRIGGER_BEAT:                 # trigger_beat, written, time_lo, time_hi
            return [100, 200, RAW_TIME, 0]
        return [0] * count

    def read32(self, address):
        return self.regs.get(address, 0)

    def write32(self, address, value):
        pass

    def write(self, address, words):
        pass

    def read_stream(self, n, timeout_ms=0):
        return bytes(n)


def test_internal_capture_time_is_the_pin_time():
    la = LogicAnalyzer(FakeDevice(la_depth=64, la_delay_internal=6.5))
    assert la.read(1e-8).trigger_time == RAW_TIME - 7          # 半刻みは切り上げ（±0.5 刻み）


def test_deep_capture_time_is_the_pin_time():
    la = DeepAnalyzer(FakeDevice(mem_bytes=1 << 20, la_delay_deep=7.5))
    assert la.read(before_beats=2, after_beats=2).trigger_time == RAW_TIME - 8
    assert la.read_transitions(before_beats=2, after_beats=2).trigger_time == RAW_TIME - 8


def test_old_devices_without_the_declaration_are_not_corrected():
    assert LogicAnalyzer(FakeDevice(la_depth=64)).read(1e-8).trigger_time == RAW_TIME


def test_a_deep_capture_that_triggered_but_is_not_full_says_so():
    # RLE は信号の変化が少ないとトリガ後の拍が埋まるのに時間がかかる。「トリガが成立しない」と誤らせない
    import pytest
    dev = FakeDevice(mem_bytes=1 << 20)
    la = DeepAnalyzer(dev)
    with pytest.raises(TimeoutError, match="トリガが成立しない"):
        la.wait(timeout_s=0.01)
    dev.regs[g.LA_DEEP_STATUS] = 1 << g.LA_DEEP_STATUS_TRIGGERED[0]
    with pytest.raises(TimeoutError, match="トリガは成立した.*埋まらない"):
        la.wait(timeout_s=0.01)
