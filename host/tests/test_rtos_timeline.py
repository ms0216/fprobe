"""fprobe.rtos.timeline: タスクの時間軸。方法 A（pxCurrentTCB のサンプル）・方法 B（トレースフック → ITM）と記録への書き込み。"""

import pytest

from fprobe.itm import Instrumentation, Overflow
from fprobe.recording import Recorder, Recording
from fprobe.rtos.timeline import (TaskEvent, decode_trace_word, itm_packets, samples_from_recording,
                                  swo_bytes_from_recording, task_events, timeline_from_events,
                                  timeline_from_samples, write_task_events, write_timeline)

A, B, C, IDLE = 0x2000_5000, 0x2000_5100, 0x2000_5200, 0x2000_5300
NAMES = {A: "ctrl", B: "sensor", C: "worker", IDLE: "IDLE"}
P = 100                                      # サンプリングの周期（タイムベース）


def sampled(values, period=P, t0=1000):
    return [(t0 + i * period, v) for i, v in enumerate(values)]


# --- 方法 A ----------------------------------------------------------------------------------

def test_sampled_runs_and_midpoint_switches():
    tl = timeline_from_samples(sampled([IDLE] * 5 + [A] * 3 + [IDLE] * 2), NAMES)
    assert tl.method == "sampled" and tl.resolution == P and tl.lower_bound
    assert [(r.task, r.t0, r.t1, r.samples) for r in tl.runs] == [
        ("IDLE", 1000, 1450, 5), ("ctrl", 1450, 1750, 3), ("IDLE", 1750, 1900, 2)]
    assert [(s.t, s.previous, s.next, s.uncertainty) for s in tl.switches] == [
        (1450, IDLE, A, 50), (1750, A, IDLE, 50)]
    assert all(r.uncertainty == 50 for r in tl.runs[1:2])
    assert (tl.t0, tl.t1) == (1000, 1900) and tl.gaps == []


def test_sampled_stats():
    tl = timeline_from_samples(sampled([IDLE] * 6 + [A] * 2 + [IDLE] * 6 + [A] * 4 + [B] + [IDLE] * 2), NAMES)
    s = tl.stats()
    assert s["switches"] == 5 and s["lower_bound"] and s["covered"] == 2000 and s["resolution"] == P
    ctrl = s["tasks"]["ctrl"]
    assert ctrl["runs"] == 2 and ctrl["total"] == 600 and ctrl["max_run"] == 400 and ctrl["min_run"] == 200
    assert ctrl["cpu"] == pytest.approx(600 / 2000)
    assert s["tasks"]["sensor"]["runs"] == 1 and s["tasks"]["sensor"]["total"] == 100
    assert sum(t["cpu"] for t in s["tasks"].values()) == pytest.approx(1.0)
    assert s["tasks"]["sensor"]["short_runs"] == 1                     # 1 サンプルだけ: 周期より短いかもしれない


def test_sampled_long_intervals_are_not_gaps_unless_the_caller_gives_a_factor():
    # 「分解能の 2.5 倍を超えたら抜け」と既定で決めない（経験則。CLAUDE.md）。長い間隔はそのまま（同じ値なら 1 つの区間）
    samples = sampled([IDLE] * 3 + [A] * 2) + sampled([A] * 3 + [IDLE] * 2, t0=10_000)
    plain = timeline_from_samples(samples, NAMES)
    assert plain.gaps == [] and [r.task for r in plain.runs] == ["IDLE", "ctrl", "IDLE"]
    # 使う側が明示した間隔（タイムベース）を超えたときだけ抜けにする。中央値を分解能とした倍率では決めない
    with pytest.raises(TypeError):
        timeline_from_samples(samples, NAMES, gap_factor=2.5)
    tl = timeline_from_samples(samples, NAMES, gap=250)
    assert [(g.t0, g.t1) for g in tl.gaps] == [(1400, 10_000)]
    assert [(r.task, r.t0, r.t1) for r in tl.runs] == [
        ("IDLE", 1000, 1250), ("ctrl", 1250, 1400), ("ctrl", 10_000, 10_250), ("IDLE", 10_250, 10_400)]
    assert len(tl.switches) == 2                                       # 抜けをまたぐ切り替えは数えない
    assert tl.stats()["covered"] == 400 + 400


def test_sampled_period_and_unknown_handles():
    tl = timeline_from_samples([(0, 0), (50, 0x2000_9990), (100, 0x2000_9990)], {}, period=50)
    assert tl.resolution == 50 and [r.task for r in tl.runs] == ["(none)", "0x20009990"]
    assert timeline_from_samples([], NAMES).runs == []


def test_sampled_from_unsorted_samples():
    samples = list(reversed(sampled([IDLE, IDLE, A, A])))
    tl = timeline_from_samples(samples, NAMES)
    assert [r.task for r in tl.runs] == ["IDLE", "ctrl"]


# --- 方法 B ----------------------------------------------------------------------------------

def stim31(word):
    return bytes([0xFB]) + word.to_bytes(4, "little")                 # ポート 31、4 バイト


def swo_stream(events, byte_ticks=10, start=0):
    """[(t, word or b"raw")] → 時刻付きのバイト列（パケットのバイトは byte_ticks ずつ後ろへ）。"""
    out = []
    for t, item in events:
        data = stim31(item) if isinstance(item, int) else item
        out += [(t + i * byte_ticks, b) for i, b in enumerate(data)]
    return out


def test_decode_trace_word():
    assert decode_trace_word(A | 0) == ("task_in", A)
    assert decode_trace_word(A | 1) == ("task_out", A)
    assert decode_trace_word(B | 2) == ("create", B)
    assert decode_trace_word(B | 3) == ("delete", B)


def test_itm_packets_carry_header_time():
    stream = swo_stream([(100, A), (500, B | 1)])
    packets = itm_packets(stream)
    assert [(t, p) for t, p in packets] == [(100, Instrumentation(31, A, 4)), (500, Instrumentation(31, B | 1, 4))]


def test_task_events_filter_port_and_mark_overflow():
    stream = swo_stream([(100, A), (200, bytes([0x0B, 0x41, 0, 0, 0])), (300, b"\x70"), (400, A | 1)])
    events = task_events(itm_packets(stream))
    assert events == [TaskEvent(100, "task_in", A), TaskEvent(300, "overflow", 0), TaskEvent(400, "task_out", A)]


def test_itm_timeline_runs_switches_and_stats():
    events = [TaskEvent(t, k, h) for t, k, h in [
        (0, "task_in", IDLE), (1000, "task_out", IDLE), (1010, "task_in", A), (1300, "task_out", A),
        (1310, "task_in", A), (1500, "task_out", A),                    # 同じタスクの out → in: 切り替えではない
        (1510, "task_in", B), (1530, "task_out", B), (1540, "task_in", IDLE), (3000, "task_out", IDLE)]]
    tl = timeline_from_events(events, NAMES, resolution=5)
    assert tl.method == "itm" and not tl.lower_bound and tl.resolution == 5
    assert [(r.task, r.t0, r.t1) for r in tl.runs] == [
        ("IDLE", 0, 1000), ("ctrl", 1010, 1300), ("ctrl", 1310, 1500), ("sensor", 1510, 1530), ("IDLE", 1540, 3000)]
    assert [(s.t, s.previous, s.next) for s in tl.switches] == [(1010, IDLE, A), (1510, A, B), (1540, B, IDLE)]
    s = tl.stats()
    assert s["tasks"]["ctrl"]["runs"] == 2 and s["tasks"]["ctrl"]["total"] == 290 + 190
    assert s["tasks"]["sensor"]["max_run"] == 20 and s["switches"] == 3
    assert s["covered"] == 3000


def test_itm_timeline_overflow_makes_gap():
    events = [TaskEvent(0, "task_in", A), TaskEvent(100, "overflow", 0), TaskEvent(400, "task_in", B),
              TaskEvent(450, "task_in", C)]                              # task_out が無くても次の task_in で閉じる
    tl = timeline_from_events(events, NAMES)
    assert [(r.task, r.t0, r.t1) for r in tl.runs] == [("ctrl", 0, 100), ("sensor", 400, 450)]
    assert [(g.t0, g.t1, g.reason) for g in tl.gaps] == [(100, 400, "overflow")]
    assert [(s.previous, s.next) for s in tl.switches] == [(B, C)]     # あふれの直後は前のタスクが分からない
    assert tl.t1 == 450


def test_itm_end_to_end_from_bytes():
    stream = swo_stream([(0, IDLE), (1000, IDLE | 1), (1010, A), (1300, A | 1), (1310, IDLE)])
    events = task_events(itm_packets(stream))
    tl = timeline_from_events(events, NAMES, resolution=50, t_end=2000)    # 最後の区間は観測の終わりまで
    assert [(r.task, r.t0, r.t1) for r in tl.runs] == [("IDLE", 0, 1000), ("ctrl", 1010, 1300), ("IDLE", 1310, 2000)]
    assert tl.t1 == 2000
    tl = timeline_from_events(events, NAMES)                                # 終わりが分からない: 長さ 0 の区間は捨てる
    assert [r.task for r in tl.runs] == ["IDLE", "ctrl"] and tl.t1 == 1310


# --- 記録 -----------------------------------------------------------------------------------

def test_write_timeline_and_read_back(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=1e6)
    tl = timeline_from_samples(sampled([IDLE] * 3 + [A] * 2), NAMES)
    write_timeline(rec, tl, idle={IDLE})
    rec.close()
    r = Recording(tmp_path / "r")
    runs = r.events(streams=["rtos"], kinds=["run"])
    assert [e.body.get("idle", False) for e in runs] == [True, False]       # 画面は名前でなくこの印で idle を知る
    assert [(e.t, e.t_end, e.a, e.body["task"]) for e in runs] == [(1000, 1250, IDLE, "IDLE"), (1250, 1400, A, "ctrl")]
    assert runs[0].body["method"] == "sampled" and runs[0].body["resolution"] == P and runs[0].body["samples"] == 3
    switch = r.events(streams=["rtos"], kinds=["switch"])
    assert [(e.t, e.a, e.b, e.body, e.uncertainty) for e in switch] == [
        (1250, IDLE, A, {"from": "IDLE", "to": "ctrl"}, 50)]
    r.close()


def test_write_gap_and_task_events(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=1e6)
    events = [TaskEvent(0, "task_in", A), TaskEvent(100, "overflow", 0), TaskEvent(400, "task_in", B),
              TaskEvent(420, "task_out", B), TaskEvent(430, "create", C)]
    write_timeline(rec, timeline_from_events(events, NAMES))
    write_task_events(rec, events, NAMES)
    rec.close()
    r = Recording(tmp_path / "r")
    gap = r.events(streams=["rtos"], kinds=["gap"])
    assert [(e.t, e.t_end, e.body) for e in gap] == [(100, 400, {"reason": "overflow"})]
    raw = r.events(streams=["rtos"], kinds=["task_in", "task_out", "create", "overflow"])
    assert [(e.kind, e.a, (e.body or {}).get("task")) for e in raw] == [
        ("task_in", A, "ctrl"), ("overflow", 0, None), ("task_in", B, "sensor"), ("task_out", B, "sensor"),
        ("create", C, "worker")]
    stats = r.intervals({"stream": "rtos", "kind": "task_in", "a": B}, {"stream": "rtos", "kind": "task_out", "a": B})
    assert stats["count"] == 1 and stats["max"] == 20
    r.close()


def test_inputs_from_recording(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=1e6)
    current = 0x2000_4DC0
    rec.add_many([("sample", "word", 100, current, IDLE), ("sample", "word", 105, 0x2000_0000, 7),
                  ("sample", "word", 200, current, A), ("swo", "byte", 300, 0, 0xFB), ("uart", "rx", 301, 0, 0x41),
                  ("swo", "byte", 310, 0, 0x00)])
    rec.close()
    r = Recording(tmp_path / "r")
    assert samples_from_recording(r, current) == [(100, IDLE), (200, A)]
    assert samples_from_recording(r, current, t0=150) == [(200, A)]
    assert swo_bytes_from_recording(r) == [(300, 0xFB), (310, 0x00)]
    r.close()


def test_overflow_packet_is_seen_by_task_events():
    assert task_events([(5, Overflow())]) == [TaskEvent(5, "overflow", 0)]


def test_trace_word_format_matches_the_firmware_header():
    """freertos_demo/fprobe_trace.h の定数と、ホストの解読（ポート・出来事の番号）が同じ。"""
    import re
    from pathlib import Path

    from fprobe.rtos.timeline import EVENT_KINDS, TRACE_PORT
    header = (Path(__file__).resolve().parents[1] / "firmware" / "freertos_demo" / "fprobe_trace.h").read_text(
        encoding="utf-8")
    defines = {m[1]: int(m[2]) for m in re.finditer(r"#define FPROBE_TRACE_(\w+)\s+(\d+)u", header)}
    assert defines["PORT"] == TRACE_PORT
    assert [defines[k] for k in ("IN", "OUT", "CREATE", "DELETE")] == [0, 1, 2, 3]
    assert EVENT_KINDS == ("task_in", "task_out", "create", "delete")


def test_sampled_resolution_is_the_achieved_interval_when_the_sampler_is_slower():
    """設定した周期（20）より実際の間隔（100）が長い（SWD の速さで 1 巡回が間に合わない）: 間隔を分解能にし、
    すべてを抜けにしない。"""
    samples = [(100 * i, h) for i, h in enumerate([IDLE] * 5 + [A] * 3 + [IDLE] * 2)]
    tl = timeline_from_samples(samples, NAMES, period=20)
    assert tl.resolution == 100 and not tl.gaps and len(tl.switches) == 2
