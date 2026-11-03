"""ロジアナの時刻（trigger_time）を、デバイスが申告する入り口の遅れ（ケイパビリティ LA_DELAY）で直す。機器は使わない。

FPGA はトリガのサンプルに、ピンの変化より数刻み後の時刻を付ける（同期化と符号器までの段）。読み出しはそれを引いて、
ピンの変化の時刻にする（DAP のイベント・FPGA の観測と同じ時間軸に並べるため）。"""

from fprobe import generated as g
from fprobe.la import DeepAnalyzer, LogicAnalyzer
from caps_fakes import FakeDeviceBase, caps_for

RAW_TIME = 1_000_000


class FakeDevice(FakeDeviceBase):
    def __init__(self, **caps):
        self.caps = caps_for(la_channels=16, la_sample_hz=100_000_000, sys_clock_hz=100_000_000, **caps)
        self.regs = {g.LA_DEEP_RING_BEATS: 1024, g.LA_DEEP_POST_BEATS: 4, g.LA_DEEP_STATUS: 0}

    def read(self, address, count=1, *, timeout_retries=0):
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

    def read_stream(self, size, timeout_ms=0):
        return bytes(size)


def test_internal_capture_time_is_the_pin_time():
    la = LogicAnalyzer(FakeDevice(la_depth=64, la_delay_internal=6, la_phase_span=1))
    # ピンの変化の下限（段数だけ引く。変化はそこから位相の幅 1 刻み未満。時刻の正しさ S1b: それまでは位相の幅も引いていて 1 刻み前）
    assert la.read(1e-8).trigger_time == RAW_TIME - 6


def test_deep_capture_time_is_the_pin_time():
    la = DeepAnalyzer(FakeDevice(mem_bytes=1 << 20, la_delay_deep=7, la_phase_span=1))
    assert la.read(before_beats=2, after_beats=2).trigger_time == RAW_TIME - 7
    assert la.read_transitions(before_beats=2, after_beats=2).trigger_time == RAW_TIME - 7


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


def test_capture_clock_that_is_a_multiple_of_sys_gives_the_pin_time_in_capture_cycles():
    # 取り込み 300 MHz、sys 100 MHz（比 3）。ピンが変わった取り込みの周期 = タイムベース × 3 + 位相 − 固定の遅れ
    dev = FakeDevice(la_depth=64)
    dev.caps = caps_for(la_channels=16, la_sample_hz=300_000_000, sys_clock_hz=100_000_000, la_depth=64)
    dev.regs[g.LA_CAP_INFO] = 3 << g.LA_CAP_INFO_RATIO[0] | 2 << g.LA_CAP_INFO_TRIGGER_PHASE[0] | 5 << g.LA_CAP_INFO_LATENCY[0]
    cap = LogicAnalyzer(dev).read(1 / 300e6)
    assert cap.trigger_cap_cycles == RAW_TIME * 3 + 2 - 5
    assert cap.trigger_time == (RAW_TIME * 3 + 2 - 5) // 3
    # 比が合わないデバイスは使わない
    import pytest
    dev.regs[g.LA_CAP_INFO] = 2 << g.LA_CAP_INFO_RATIO[0]
    with pytest.raises(RuntimeError, match="比が合わない"):
        LogicAnalyzer(dev).read(1 / 300e6)


def test_filter_and_pulse_widths_are_counted_in_capture_cycles():
    from fprobe.la import Front, Pulse
    caps = caps_for(la_channels=16, la_sample_hz=300_000_000, sys_clock_hz=100_000_000, la_depth=64,
                    la_filter=True, la_pulse=True)
    front = Front.of(caps, 0b1, 100.0, Pulse(channel=0, min_ns=30.0, max_ns=60.0))
    assert front.words[0] >> g.LA_FILTER_CYCLES[0] & 0xFF == 30           # 100 ns × 300 MHz
    assert front.words[2:] == (9, 18)                                     # 30 ns・60 ns
