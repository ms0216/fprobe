"""テストポイントの流しながらの解析（fprobe.testpoint.live）: 時刻の写し・時刻付きの SWO のバイト・上限付きの
呼び出しの一覧・記録への書き出し・JSON の要約。機器は使わない。"""

import json
import threading

import pytest

from fprobe.itm import Instrumentation, Sync
from fprobe.recording import Recorder, Recording
from fprobe.testpoint import synth
from fprobe.testpoint.live import (LiveAnalysis, TimeMap, TimedSwo, calls_dict, coverage_dict, report_dict)
from fprobe.testpoint.probes import Probe, ProbeTable
from fprobe.testpoint.trace import Hit, Loss


def P(probe_id, fn, kind, **kw):
    return Probe(probe_id, "a.c", probe_id * 10, fn, kind, **kw)


TABLE = ProbeTable.of([
    P(1, "main", "entry"), P(2, "main", "exit"),
    P(3, "f", "entry"), P(4, "f", "exit"),
    P(7, "f", "then", decision=7, label="if"), P(8, "f", "else", decision=7, label="else", implicit=True),
    P(9, "f", "var", variable="state", label="state"),
    Probe(20, "b.c", 5, "other", "entry"), Probe(21, "b.c", 9, "other", "exit"),
])


def calls_of(n, start=0, step=100):
    """f の呼び出しを n 回（then の枝と変数の印つき）。"""
    out = []
    for k in range(n):
        t = start + k * step
        out += [Hit(t, 3), Hit(t + 10, 7), Hit(t + 20, 9, k), Hit(t + 50, 4)]
    return out


# --- 時刻の写し ---------------------------------------------------------------------------------

def test_time_map_scales_and_offsets_into_the_timebase():
    m = TimeMap(hz=50e6, timebase_hz=100e6, offset=1000, uncertainty=3.0)
    assert m.ticks(0) == 1000 and m.ticks(5) == 1010
    assert TimeMap(hz=100e6, timebase_hz=100e6).ticks(123) == 123


# --- 時刻付きの SWO のバイト ---------------------------------------------------------------------

def test_timed_swo_gives_each_packet_the_time_of_its_first_byte_across_feeds():
    raw = synth.itm_bytes([(0, 5, None), (0, 6, 0xAB)], port=1, timestamps=False)
    events = [(1000 + 10 * i, b) for i, b in enumerate(raw)]
    swo = TimedSwo()
    out = swo.feed(events[:8]) + swo.feed(events[8:])             # パケットの途中で分ける
    stim = [(t, p.value, p.size) for t, p in out if isinstance(p, Instrumentation)]
    sync_len = 6
    assert stim == [(1000 + 10 * sync_len, 5, 2), (1000 + 10 * (sync_len + 3), 0xAB, 4),
                    (1000 + 10 * (sync_len + 8), 6, 2)]
    assert isinstance(out[0][1], Sync)
    assert len(swo._times) < 4                                     # 解読し終えたバイトの時刻は捨てる


# --- 流しながらの解析 ---------------------------------------------------------------------------

def test_live_analysis_snapshots_while_running_and_finishes_once():
    live = LiveAnalysis(TABLE, hz=1e6, open_s=0.0001)
    live.feed([Hit(0, 1)] + calls_of(3, start=100))
    snap = live.report()
    assert snap.functions["f"].calls == 3 and not snap.anomalies and snap.open_frames == [("main", 0)]
    final = live.report(final=True)
    assert [a.type for a in final.anomalies] == ["no_exit"]
    assert live.report() is final and live.finished
    with pytest.raises(RuntimeError):
        live.feed([Hit(1000, 2)])                                 # 終えた後は受けない


def test_recent_calls_are_kept_up_to_the_limit():
    live = LiveAnalysis(TABLE, hz=1e6, calls_limit=5)
    live.feed(calls_of(8))
    calls, dropped = live.calls()
    assert len(calls) == 5 and dropped == 3 and calls[0].t0 == 300   # 古いものから捨てる
    assert live.report().functions["f"].calls == 8                    # 統計は全部


def test_feeding_from_threads_is_safe():
    live = LiveAnalysis(TABLE, hz=1e6)
    def feeder(base):
        for k in range(50):
            live.feed([Hit(base + k, 20), Hit(base + k, 21)])
    threads = [threading.Thread(target=feeder, args=(i * 1000,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert live.report().functions["other"].calls == 200


def test_recording_needs_an_anchor_and_writes_mapped_times(tmp_path):
    rec = Recorder(tmp_path / "rec", timebase_hz=100e6)
    live = LiveAnalysis(TABLE, hz=1e6, recorder=rec)
    with pytest.raises(RuntimeError, match="anchor"):
        live.feed([Hit(0, 3)])
    live.anchor(TimeMap(hz=1e6, timebase_hz=100e6, offset=5000, uncertainty=7.0))
    live.anchor(TimeMap(hz=1e6, timebase_hz=100e6, offset=1))       # 2 回目は無視（最初の写しのまま）
    live.feed(calls_of(2))
    live.report(final=True)
    rec.close()
    rd = Recording.open(tmp_path / "rec")
    calls = rd.find({"stream": "testpoint", "kind": "call"})
    assert [(e.t, e.t_end, e.uncertainty) for e in calls] == [(5000, 10000, 7.0), (15000, 20000, 7.0)]
    assert rd.meta["testpoint"]["coverage"]["c0"] == pytest.approx(3 / 7)
    rd.close()


# --- JSON の要約 --------------------------------------------------------------------------------

def test_report_dict_is_json_with_relative_seconds_and_timebase_ticks():
    live = LiveAnalysis(TABLE, hz=1e6)
    live.anchor(TimeMap(hz=1e6, timebase_hz=100e6, offset=10_000))
    live.feed([Hit(1000, 1)] + calls_of(4, start=2000) + [Hit(9000, 4), Hit(9500, 99), Loss(9600, "x")])
    d = report_dict(live.report(), live.time_map, top=1, anomalies=2, buckets=10, tree_depth=1)
    json.dumps(d, ensure_ascii=False)
    assert [f["name"] for f in d["functions"]] == ["main"] and d["functions_total"] == 2
    assert d["anomaly_counts"] == {"exit_without_entry": 1, "unknown_id": 1, "loss": 1}
    assert len(d["anomalies"]) == 2 and d["anomalies_total"] == 3
    first = d["anomalies"][0]
    assert first["type"] == "exit_without_entry" and first["t_s"] == pytest.approx(8000e-6)
    assert first["t"] == 10_000 + 9000 * 100
    assert d["coverage"]["c0"] == [4, 7] and "uncovered" not in d["coverage"]
    assert d["variables"][0]["variable"] == "state" and d["variables"][0]["last"] == 3
    assert d["tree"]["children"][0]["function"] == "main" and d["tree"]["children"][0]["children"] == []
    assert d["tree"]["children"][0]["children_omitted"] == 1


def test_calls_dict_filters_by_time_and_function_and_renders_text():
    live = LiveAnalysis(TABLE, hz=1e6)
    live.feed([Hit(0, 1)] + calls_of(5, start=100) + [Hit(1000, 2)])
    calls, dropped = live.calls()
    d = calls_dict(calls, dropped, live.report(), None, t0_s=250e-6, t1_s=500e-6, function="f", limit=10)
    assert [c["t_s"] for c in d["calls"]] == pytest.approx([300e-6, 400e-6])
    assert all(c["function"] == "f" and c["depth"] == 1 and "t" not in c for c in d["calls"])
    assert d["total"] == 2 and "f" in d["text"]
    d = calls_dict(calls, dropped, live.report(), None, limit=3)
    assert len(d["calls"]) == 3 and d["total"] == 6 and d["truncated"]


def test_coverage_dict_filters_the_uncovered_list():
    live = LiveAnalysis(TABLE, hz=1e6)
    live.feed(calls_of(1))
    d = coverage_dict(live.report().coverage, file="a.c", function=None, limit=10)
    assert d["c0"] == [3, 7] and d["c1"] == [1, 2]
    assert {(u["function"], u["kind"]) for u in d["uncovered"]} == {("main", "entry"), ("main", "exit"),
                                                                   ("f", "else")}
    d = coverage_dict(live.report().coverage, file=None, function="other", limit=10)
    assert [u["kind"] for u in d["uncovered"]] == ["entry", "exit"] and d["decisions"] == []
    d = coverage_dict(live.report().coverage, file=None, function=None, limit=1)
    assert len(d["uncovered"]) == 1 and d["uncovered_total"] == 5
