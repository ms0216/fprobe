"""時刻の事実（段階 S0。docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.5・§5.3）。機器は使わない。

- ロジアナの ch ごとの時刻: 内部の ch はフィルタを通らない分、ピンの時間軸から回路の申告（LA_CH_TIMING）だけずれる。
  フィルタをかけた ch は設定の cycles − 1 周期さらに遅れる。補正はホストが ch ごとにする（数字はホストに持たない）。
- 連続記録のサンプル 0: 回路が記録しないので、往復とトリガから確かに言える範囲だけを返す（経験則の余裕を持たない）。
- ポストされた読み: 読んだ瞬間の範囲（頼んだ転送の前に終わった転送 〜 値を返した転送）を付ける。
"""

import dataclasses
import random
import threading
import time
from array import array

import pytest

from fprobe import generated as g
from fprobe.caps import LaChannel
from fprobe.collector import add_events
from caps_fakes import FakeDeviceBase, caps_for
from fprobe.events import KIND_HOST, KIND_SAMPLE, KIND_UART_RX, DapEvent, resolve
from fprobe.la import LogicAnalyzer, Transitions, Waveform
from fprobe.recording import Recorder, Recording
from fprobe.timing import la_channel_skew, la_channel_timing, posted_read_window, stream_start_window

HZ = 100_000_000
LAYOUT = (tuple(LaChannel(i, "pin", i) for i in range(8)) + (LaChannel(8, "internal", 0), LaChannel(9, "internal", 1))
          + tuple(LaChannel(10 + i, "none", 0) for i in range(6)))
# 回路の申告の形（gateware の timing.la_channel_timing と同じ: ピン 0、内部 3（フィルタの段）、つながっていない ch は不明）
DECLARED = tuple([(0, 0, 1)] * 8 + [(3, 3, 0)] * 2 + [(None, None, None)] * 6)


def caps(**kw):
    base = dict(la_channels=16, la_depth=64, la_sample_hz=HZ, la_layout=LAYOUT,
                la_delay_internal=6, la_delay_deep=7, la_phase_span=1, la_channel_timing=DECLARED, la_filter=True)
    base.update(kw)
    return caps_for(**base)


# --- ch ごとの時刻 ---------------------------------------------------------------------------------------------

def test_internal_channels_are_skewed_by_the_declared_ticks_and_pins_are_the_axis():
    rows = la_channel_timing(caps(), deep=False)
    assert [r["skew_ticks"] for r in rows[:10]] == [0] * 8 + [3, 3]
    assert [r["span_ticks"] for r in rows[:10]] == [1] * 8 + [0, 0]
    assert all(r["skew_ticks"] is None for r in rows[10:])                  # 何もつながっていない
    assert rows[8]["from"] == ["LA_CH_TIMING"]


def test_old_devices_without_the_declaration_do_not_guess_the_internal_channels():
    rows = la_channel_timing(caps(la_channel_timing=()), deep=True)
    assert [r["skew_ticks"] for r in rows[:8]] == [0] * 8                    # ピンは LA_DELAY の申告の時間軸そのもの
    assert rows[8]["skew_ticks"] is None and rows[9]["skew_ticks"] is None  # 申告が無い: 分からない（0 で埋めない）


def test_a_filtered_channel_is_earlier_by_its_configured_delay():
    front = {"filter": {"channels": [2], "min_width_ns": 50.0, "delay_ns": 40.0, "delay_cycles": 4}}
    skew = la_channel_skew(caps(), deep=False, front_info=front)
    assert skew[2] == -4 and skew[1] == 0 and skew[8] == 3
    # 取り込みのクロックが sys の 3 倍: 周期は取り込みのクロックで数えるので、刻みでは 1/3
    fast = la_channel_skew(caps(la_sample_hz=3 * HZ), deep=False, front_info=front)
    assert fast[2] == pytest.approx(-4 / 3)


class LaDevice(FakeDeviceBase):
    def __init__(self, c, cap_info=0):
        self.caps, self.cap_info = c, cap_info

    def read(self, address, count=1, *, timeout_retries=0):
        if address == g.LA_START_INDEX:
            return [0, 5, 1_000_000, 0]
        return [0] * count

    def read32(self, address):
        return self.cap_info if address == g.LA_CAP_INFO else 0

    def write32(self, address, value):
        pass

    def write(self, address, words):
        pass


def test_a_capture_carries_the_per_channel_skew_of_the_device():
    la = LogicAnalyzer(LaDevice(caps()))
    la.start(filter_channels=0b100, filter_ns=50.0)
    cap = la.read(1e-8)
    assert cap.channel_skew[8] == 3 and cap.channel_skew[0] == 0 and cap.channel_skew[2] == -4
    assert Transitions.from_capture(cap).channel_skew == cap.channel_skew


def test_the_capture_domain_time_is_not_corrected_twice():
    # 取り込みのクロックが sys の整数倍の構成: ピンが変わった取り込みの周期 n は la_cap_info の latency で決まる（n の後に変わった）。
    # そこへさらに LA_DELAY（sys の経路の段数）を引くと二重になる
    c = caps(la_sample_hz=3 * HZ)
    info = 3 << g.LA_CAP_INFO_RATIO[0] | 2 << g.LA_CAP_INFO_TRIGGER_PHASE[0] | 2 << g.LA_CAP_INFO_LATENCY[0]
    cap = LogicAnalyzer(LaDevice(c, info)).read(1 / 300e6)
    assert cap.trigger_cap_cycles == 1_000_000 * 3 + 2 - 2
    assert cap.trigger_time == (1_000_000 * 3 + 2 - 2) // 3


# --- 波形の表: ch のずれを置き場所に反映する ------------------------------------------------------------------------

def test_the_waveform_places_each_channel_by_its_skew():
    # ch0（ピン）と ch1（内部、+3 刻み）が同じサンプル 10 で変わった: 内部の ch は 3 刻み後に起きた
    tr = Transitions(array("H", [0, 3]), array("Q", [0, 10]), 40, 0, 0, 1e-8, 2, channel_skew=(0, 3))
    wave = Waveform(tr, ticks_per_sample=1.0)
    assert list(wave.edges_between(0, 0, 40)) == [10]
    assert list(wave.edges_between(1, 0, 40)) == [13]
    assert wave.level_at(1, 12) == 0 and wave.level_at(1, 13) == 1
    assert wave.count_between(1, 12, 14) == 1 and wave.count_between(1, 9, 12) == 0
    assert [v[10:15] for v in wave.view(0, 40, 40)] == ["11111", "00011"]
    assert list(Waveform(tr).edges_between(1, 0, 40)) == [10]            # 刻みの長さを渡さなければずらさない（古い呼び手）


def test_the_recording_keeps_the_skew_and_its_views_use_it(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    tr = Transitions(array("H", [0, 3]), array("Q", [0, 10]), 40, 0, 0, 1e-8, 2, channel_skew=(0, 3))
    rec.add_waveform(tr, start_ticks=1000)
    rec.flush()
    r = Recording.open(rec.directory)
    (_, _, back), = r.waveforms()
    assert back.channel_skew == (0, 3)
    assert [v[10:15] for v in r.waveform_view(1000, 1040, 40)] == ["11111", "00011"]
    vcd = tmp_path / "w.vcd"
    r.export_vcd(vcd)
    text = vcd.read_text()
    assert "#10100\n1!" in text and "#10130\n1\"" in text                  # ns: ch0 は 1010 刻み、ch1 は 1013 刻み


def test_a_recording_without_the_skew_reads_as_before(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.add_waveform(Transitions(array("H", [0, 3]), array("Q", [0, 10]), 40, 0, 0, 1e-8, 2), start_ticks=0)
    rec.flush()
    (_, _, back), = Recording.open(rec.directory).waveforms()
    assert back.channel_skew == ()


# --- 連続記録のサンプル 0 -------------------------------------------------------------------------------------------

def test_the_stream_start_is_the_window_of_the_round_trip_on_the_pin_axis():
    w = stream_start_window(before=10_000, after=40_000, trigger_raw=None, delay=7, span=1)
    assert (w["t_lo"], w["t_hi"]) == (10_000 - 7, 40_000 - 7)              # 時刻 − 段数（S1b で位相の幅を引かなくなった）
    assert w["basis"]["bounds"] == {"t_lo": "before", "t_hi": "after"}


def test_the_trigger_bounds_the_stream_start_from_above_without_a_slack():
    # 条件なしのトリガは最初のサンプル以後に成立する: サンプル 0 はトリガより後ではない（何サンプル前かは決めない）
    w = stream_start_window(before=10_000, after=40_000, trigger_raw=10_400, delay=7, span=1)
    assert (w["t_lo"], w["t_hi"]) == (10_000 - 7, 10_400 - 7)
    assert w["basis"]["bounds"]["t_hi"] == "trigger"
    stale = stream_start_window(before=10_000, after=40_000, trigger_raw=5, delay=7, span=1)   # 前の流れの値
    assert stale["t_hi"] == 40_000 - 7


def test_the_recording_stream_records_the_window_and_places_the_waveform_inside_it(tmp_path):
    from fprobe.recording_long import Policy, SegmentedRecorder
    from fprobe.recording_stream import LaRecordingStream
    from test_la_stream import raw_source
    from test_recording_stream import TimedStreamDevice

    rng = random.Random(9)
    dev = TimedStreamDevice(raw_source(rng, 2000), rng=rng, trigger=True)
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=3600))
    s = LaRecordingStream(dev, rec, rle=False, chunk_s=5e-5, lock=threading.RLock())
    anchor = s.start()
    deadline = time.monotonic() + 5
    while dev.written * 16 < 2000 and time.monotonic() < deadline:
        time.sleep(0.005)
    status = s.stop()
    rec.close()
    assert anchor.t_lo <= anchor.ticks <= anchor.t_hi
    assert status["anchor"]["t_lo"] == anchor.t_lo and status["anchor"]["t_hi"] == anchor.t_hi
    r = Recording.open(tmp_path / "r")
    start = [e for e in r.events(streams=["la"]) if e.kind == "stream_start"][0]
    assert (start.body["t_lo"], start.body["t_hi"]) == (anchor.t_lo, anchor.t_hi)
    assert start.uncertainty == (anchor.t_hi - anchor.t_lo) / 2           # 置いた時刻 ± これで範囲をちょうど覆う


# --- ポストされた読み ------------------------------------------------------------------------------------------------

def host(t, *, ap, read, reg, address=0, data=0, ack=1, kind=KIND_HOST):
    return DapEvent(t, kind, ap, read, reg, ack, False, address, data)


def test_a_posted_read_carries_the_window_from_the_previous_transfer_to_the_returning_one():
    events = [host(100, ap=True, read=False, reg=0x4, address=0x4, data=0x2000_0000),   # TAR
              DapEvent(150, KIND_UART_RX, False, False, 0, 0, False, 0, 0x41),           # 線の外の出来事は数えない
              host(200, ap=True, read=True, reg=0xC, address=0x2000_0000),               # 読みを頼む
              host(300, ap=False, read=True, reg=0xC, data=0x55)]                        # RDBUFF で返る
    read = [a for a in resolve(events) if a.kind == "ap_read"][0]
    assert (read.time, read.requested, read.time_lo) == (300, 200, 100)
    assert posted_read_window(read.requested, read.time, read.time_lo)["t_lo"] == 100


def test_the_previous_transfer_may_be_the_samplers_and_a_first_read_has_no_lower_bound():
    events = [DapEvent(90, KIND_SAMPLE, True, True, 0xC, 1, False, 0x2000_0100, 7),
              host(200, ap=True, read=True, reg=0xC, address=0x2000_0000),
              host(300, ap=True, read=True, reg=0xC, address=0x2000_0004, data=1),
              host(400, ap=False, read=True, reg=0xC, data=2)]
    reads = [a for a in resolve(events) if a.kind == "ap_read"]
    assert [(a.requested, a.time_lo, a.time) for a in reads] == [(200, 90, 300), (300, 200, 400)]
    first = [a for a in resolve(events[1:]) if a.kind == "ap_read"][0]
    assert first.time_lo is None
    assert "previous_transfer" in posted_read_window(first.requested, first.time, None)["unknown"]


def test_the_recorded_posted_read_has_the_window_in_its_body(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    ref = 1 << 40
    carry = add_events(rec, [host(100, ap=True, read=False, reg=0x4, address=0x4, data=0x2000_0000),
                             host(200, ap=True, read=True, reg=0xC, address=0x2000_0000)], ref)
    add_events(rec, [host(300, ap=False, read=True, reg=0xC, data=0x55)], ref, carry=carry)
    rec.flush()
    (read,) = [e for e in Recording.open(rec.directory).events() if e.kind == "ap_read"]
    mask = (1 << 48) - 1
    assert read.t & mask == 300
    assert read.body["requested"] & mask == 200 and read.body["t_lo"] & mask == 100
