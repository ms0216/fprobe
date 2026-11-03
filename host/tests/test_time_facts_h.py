"""時刻の事実（段 H。docs/superpowers/specs/2026-11-01-time-accuracy-design.md §9.2・§9.3）。機器は使わない。

段 H はホストだけ: 仮定・経験則で時刻を作っている所を、測った事実（デバイスの時刻・範囲）にするか、「分からない」と返すか、
使う側が明示する引数にする。PC の時計で付けた時刻には PC の時刻である印を付ける。黙って欠ける・0 にする所は欠けとして返す。
"""

from array import array

from fprobe.correlate import correlate
from fprobe.la import Capture, Transitions, VcdWriter, write_vcd
from fprobe.la_measure import measure, measure_recording
from fprobe.recording import Recorder, Recording

HZ = 100_000_000


# --- B6: ずれの分からない ch を 0 として黙って置かない ------------------------------------------------------------

def _tr(skew=(0, None), period=1e-8, length=40):
    # ch0・ch1 がサンプル 10 で上がり、20 で下がる
    return Transitions(array("H", [0, 3, 0]), array("Q", [0, 10, 20]), length, 0, 0, period, 2, channel_skew=skew)


def test_the_transitions_name_the_channels_whose_skew_is_unknown():
    assert _tr().skew_unknown() == [1]
    assert _tr(skew=()).skew_unknown() == []                                # 申告なし（ファイル・合成）: どの ch もずらさない
    cap = Capture([0, 3], 0, 0, 1e-8, 2, channel_skew=(None, 0))
    assert cap.skew_unknown() == [0]


def test_a_measurement_says_which_channels_were_placed_without_their_unknown_skew():
    out = measure(_tr(), relations=[{"type": "delay", "from": 0, "to": 1}], timebase_hz=HZ)
    assert out["skew_unknown"] == [1]
    assert measure(_tr(skew=(0, 3)), timebase_hz=HZ)["skew_unknown"] == []


def test_a_recording_measurement_collects_the_unknown_channels_of_its_chunks(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.add_waveform(_tr(), start_ticks=0)
    rec.add_waveform(_tr(skew=(None, 0)), start_ticks=1000)
    rec.flush()
    assert measure_recording(Recording.open(rec.directory))["skew_unknown"] == [0, 1]


def test_the_la_summary_lists_the_channels_whose_skew_is_unknown():
    from fprobe.probe_la import _channel_summary, _skew_unknown
    cap = Capture([0, 3, 3, 0], 0, 0, 1e-8, 2, channel_skew=(0, None))
    assert _channel_summary(cap, HZ)["skew_unknown"] == [1]
    assert _skew_unknown(_tr(), HZ) == [1]                                  # la_deep_collect の skew_unknown
    assert _skew_unknown(_tr(), None) == []                                 # ずれを足していない（timebase_hz が無い）


def test_the_vcd_says_which_channels_were_not_shifted(tmp_path):
    path = tmp_path / "w.vcd"
    write_vcd(_tr(), path, timebase_hz=HZ)
    assert "$comment skew unknown (placed unshifted): ch1 $end" in path.read_text()


def test_the_correlation_reports_a_pin_whose_skew_is_unknown(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.add_waveform(_tr(length=400), start_ticks=0)
    rec.add("annotation", "mark", 5)
    rec.flush()
    r = Recording.open(rec.directory)
    out = correlate(r, "ch1 rose", ["ch0 rose"], window_s=1e-6)
    unknown = [g for g in out["gaps"] if g["reason"] == "pin_skew_unknown"]
    assert unknown and unknown[0]["variable"] == "ch1" and (unknown[0]["t0"], unknown[0]["t1"]) == (0, 400)
    assert out["causes"][0]["judged"] == 1                                  # 欠け（値の無い区間）ではない: 結果は外さない


def test_the_perfetto_capture_slice_names_the_unknown_channels(tmp_path):
    import json

    from fprobe.export.perfetto import write_trace
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.add_waveform(_tr(), start_ticks=0)
    rec.flush()
    write_trace(Recording.open(rec.directory), tmp_path / "t.json")
    events = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))["traceEvents"]
    (capture,) = [e for e in events if e.get("name") == "捕捉"]
    assert capture["args"]["skew_unknown"] == [1]


# --- VCD の時刻を黙って丸めない（800 MS/s = 1.25 ns） -----------------------------------------------------------

def test_the_vcd_timescale_is_fine_enough_for_the_sample_period(tmp_path):
    path = tmp_path / "w.vcd"
    write_vcd(_tr(skew=(), period=1.25e-9), path)
    text = path.read_text()
    assert "$timescale 10ps $end" in text and "#1250\n" in text and "#2500\n" in text   # 10 × 1.25 ns・20 × 1.25 ns
    write_vcd(_tr(skew=()), path)
    assert "$timescale 1ns $end" in path.read_text()                       # 10 ns の格子は今まで通り ns


def test_the_recording_vcd_does_not_round_a_fine_sample_period(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.add_waveform(_tr(skew=(), period=1.25e-9), start_ticks=1000)
    rec.flush()
    path = tmp_path / "w.vcd"
    Recording.open(rec.directory).export_vcd(path)
    text = path.read_text()
    assert "$timescale 10 ps $end" in text and "#1001250\n" in text       # 10 µs + 12.5 ns = 10 012.5 ns


def test_the_streaming_vcd_places_each_channel_by_its_skew(tmp_path):
    path = tmp_path / "s.vcd"
    with VcdWriter(path, 2, 1e-8, shifts=(0.0, 3.0)) as vcd:                # ch1 は 3 サンプル後に起きた
        vcd.write(array("H", [0, 3]), array("Q", [0, 10]))
        vcd.write(array("H", [1]), array("Q", [11]))
    text = path.read_text()
    assert text.index("#100\n1!") < text.index("#130\n1\"") < text.index("#140\n0\"")


# --- PC の時計で付けた時刻には PC の時刻である印（A4・§9.2 の対象の出来事） ----------------------------------------

def _recorder_with_clock(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.clock.add(10.0, 1_000_000_000, 1e-4)
    rec.clock.add(11.0, 1_100_000_000, 1e-4)
    return rec


def test_a_host_event_is_marked_as_timed_by_the_pc_clock(tmp_path):
    rec = _recorder_with_clock(tmp_path)
    rec.add_host("annotation", "mark", 10.5, body={"text": "x"})
    rec.add_host("host", "call", 10.5)
    rec.flush()
    events = Recording.open(rec.directory).events()
    assert [e.body["time_source"] for e in events] == ["host", "host"]
    assert events[0].body["text"] == "x"


def test_target_data_the_pc_received_is_marked_as_received_by_the_pc(tmp_path):
    # SWO の ITM パケット・RTT・セミホスティングの行・ヒープ・トレースポイント・ログのブレークポイント（_record_host）は、
    # PC が受けた（読んだ）時刻: 対象の出来事はそれより前
    from types import SimpleNamespace

    from fprobe.probe_observe import ObserveService
    rec = _recorder_with_clock(tmp_path)
    probe = SimpleNamespace(_host=SimpleNamespace(_recorder=rec))
    t = ObserveService._record_host(probe, "swo", "exception", 10.5, a=11, body={"action": "enter"})
    ObserveService._record_host(probe, "swo", "overflow", 10.6)
    rec.flush()
    events = Recording.open(rec.directory).events()
    assert events[0].t == t and [e.body["time_source"] for e in events] == ["host_received", "host_received"]


def test_the_daemon_marks_the_target_state_it_read_as_received(tmp_path):
    from types import SimpleNamespace

    from fprobe.probe_recording import RecordingService
    rec = _recorder_with_clock(tmp_path)
    probe = SimpleNamespace(_recorder=rec)
    RecordingService.record_host_event(probe, "host", "call", {"method": "x"})
    RecordingService.record_host_event(probe, "target", "state", {"state": "HALTED"}, time_source="host_received")
    rec.flush()
    assert [e.body["time_source"] for e in Recording.open(rec.directory).events()] == ["host", "host_received"]


def test_a_host_call_records_its_start_through_the_clock_map_and_perfetto_does_not_estimate_it(tmp_path):
    import json
    from types import SimpleNamespace

    from fprobe.export.perfetto import write_trace
    from fprobe.probe_recording import RecordingService
    rec = _recorder_with_clock(tmp_path)
    RecordingService.record_host_event(SimpleNamespace(_recorder=rec), "host", "call",
                                     {"client": "ai", "method": "target.halt", "seconds": 0.5}, started_host_s=10.25)
    rec.add("host", "call", 1_090_000_000, body={"client": "old", "method": "target.halt", "seconds": 0.5})  # 古い記録
    rec.flush()
    r = Recording.open(rec.directory)
    (new,) = [e for e in r.events() if e.body["client"] == "ai"]
    assert new.body["t_start"] == 1_025_000_000 and new.body["t_start_uncertainty"] is not None
    write_trace(r, tmp_path / "t.json")
    events = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))["traceEvents"]
    calls = {e["name"] + e["ph"] for e in events if e.get("name") == "target.halt"}
    assert calls == {"target.haltX", "target.halti"}                       # 古い記録は区間を推定せず点


def test_a_target_trace_anchored_at_the_read_time_is_an_upper_bound_with_an_unknown_lag(tmp_path):
    # A5: 「最後の出来事 = 読んだ瞬間」と置いて不確かさ 0 にしない
    from fprobe.tracefmt.model import TargetClock, Trace, TraceEvent, write_trace
    clock = TargetClock(1e6, HZ, anchor_ts=500, anchor_t=10_000_000)
    facts = clock.describe()
    assert facts["anchor_bound"] == "upper" and facts["anchors"][0]["uncertainty"] is None
    assert {"anchor_lag", "rate_error", "anchor_time"} <= set(facts["unknown"])
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    trace = Trace("systemview", events=[TraceEvent(400, "task_in", "x", task=1), TraceEvent(500, "task_out", "x", task=1)])
    write_trace(rec, trace, TargetClock(1e6, HZ, 500, 10_000_000, uncertainty=30.0))
    rec.flush()
    events = Recording.open(rec.directory).events()
    assert [e.t for e in events] == [9_990_000, 10_000_000]
    assert all(e.uncertainty is None and e.body["time_bound"] == "upper" and e.body["time_source"] == "target_clock"
               for e in events)


def test_a_one_mark_chain_borrows_the_previous_slope_only_when_asked():
    # B3: 印が 1 つの鎖に前の鎖の傾きを借りるのは仮定（前の鎖と時計の比が同じ）。使う側が明示したときだけ
    from fprobe.obs_decoder import ObsDecoder, _Chain
    for borrow, nominal, basis in ((False, 2.0, "nominal_ratio"), (True, 2.0, "previous_ratio"), (False, None, None)):
        d = ObsDecoder(nominal_ratio=nominal, borrow_ratio=borrow, max_pending=None)
        d._ratio = (3.0, 3.0)                                              # 前の鎖の比の範囲（段階 S3）
        chain = d._chain = _Chain()
        d._marks[1], d._events[1] = (chain, 10), 1000                      # この鎖の印は 1 つだけ
        d._pair(1)
        assert chain.basis == basis
        want = {"nominal_ratio": (2.0, 2.0), "previous_ratio": (3.0, 3.0)}.get(basis)
        assert (chain.segment.slope if chain.segment else None) == want


def test_the_sampled_timeline_resolution_is_the_longest_interval_not_the_median():
    from fprobe.rtos.timeline import timeline_from_samples
    points = [(0, 1), (10, 1), (20, 2), (30, 2), (130, 1)]               # 間隔 10, 10, 10, 100
    assert timeline_from_samples(points, {}, period=10).resolution == 100
    assert timeline_from_samples(points, {}, period=10, gap=50).resolution == 10   # 抜けにした間隔は除く
    assert timeline_from_samples([(0, 1)], {}, period=10).resolution == 10


def test_the_exception_rate_for_the_swo_budget_is_not_an_implicit_default():
    from fprobe.profile.dwt import plan_itm_sampling
    plan = plan_itm_sampling(16e6, 10_000, 2_000_000, exception_trace=True, timestamps=True)
    assert plan.exception_rate_hz is None and any("exception_rate_hz" in w for w in plan.warnings)
    given = plan_itm_sampling(16e6, 10_000, 2_000_000, exception_trace=True, exception_rate_hz=1000, timestamps=True)
    assert given.exception_rate_hz == 1000 and given.bytes_per_s > plan.bytes_per_s


def test_a_counter_overflow_keeps_the_time_of_its_own_packet():
    # §9.2 probe_swo.py:337: 溢れの時刻を、処理した最後のバイトの時刻で代えない
    from fprobe.itm import EventCounter
    from fprobe.swo_trace import CounterTotals
    c = CounterTotals()
    c.feed_timed([(100, EventCounter(0x01)), (250, EventCounter(0x03)), (None, EventCounter(0x01))])
    assert c.events == [(100, "cpi", 256), (250, "cpi", 512), (250, "exc", 256)]
    assert c.overflows["cpi"] == 3                                         # 時刻の無い印も数には入る


def test_a_linux_time_extrapolated_outside_the_pairs_has_no_bound():
    from fprobe.probe_linux import sync_uncertainty
    inside = {"half_rtt_ns": 1000, "outside": False}
    assert sync_uncertainty(inside, HZ, 5.0) == 1000 / 1e9 * HZ + 5.0
    assert sync_uncertainty({**inside, "outside": True}, HZ, 5.0) is None


def test_the_arrival_of_a_log_does_not_assume_8n1_or_the_nominal_poll(tmp_path):
    # §9.2: SWO の不確かさを 10 ビット / baud（8N1 の仮定）、RTT の上限に公称の poll_s を足さない
    from types import SimpleNamespace

    from fprobe.probe_formats import FormatService
    rec = _recorder_with_clock(tmp_path)
    probe = SimpleNamespace(_host=SimpleNamespace(_recorder=rec), poll_s=0.01)
    feed = SimpleNamespace(source="swo", settings={"swo_baud": 2_000_000}, timebase_hz=HZ, host_clock=None)
    assert FormatService._arrival(probe, feed, None, 12345) == (12345, None)
    ticks, unc = FormatService._arrival(probe, feed, 10.5, None)
    assert ticks == 1_050_000_000 and unc == rec.clock.to_ticks(10.5)[1]   # PC の時計の写しの分だけ


def test_two_cyccnt_reads_state_the_one_wrap_assumption():
    from fprobe.profile.cycles import clock_from_two_reads
    est = clock_from_two_reads((0, 10, 100), (HZ, HZ + 10, 100 + 16_000_000), HZ)
    assert any("1 周未満と仮定" in w for w in est.warnings)


def test_the_stop_says_its_elapsed_time_is_the_pc_clock():
    from fprobe.debug import Stop
    from fprobe.probe_debug import DebugService
    from fprobe.symbols import Location
    out = DebugService._stop_dict(Stop("breakpoint", 0x100, Location(0x100), 0.25))
    assert out["elapsed_s"] == 0.25 and out["time_source"] == "host"
