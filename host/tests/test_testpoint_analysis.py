"""テストポイント: 解析（遷移・時間・2 点間・カバレッジ・長時間の要約・異常）と記録への書き出し。"""

import json

import pytest
from testpoint_helpers import DATA, needs_zig, parse_log, run_instrumented

from fprobe.decode.synth import Wave
from fprobe.recording import Recorder, Recording
from fprobe.testpoint import synth
from fprobe.testpoint.analysis import Analyzer, analyze, intervals, render_calls
from fprobe.testpoint.decode import decode_gpio_serial
from fprobe.testpoint.instrument import Options, WatchVar
from fprobe.testpoint.probes import Probe, ProbeTable
from fprobe.testpoint.record import record_testpoints
from fprobe.testpoint.trace import Hit, Loss


def P(probe_id, fn, kind, **kw):
    return Probe(probe_id, "a.c", probe_id * 10, fn, kind, **kw)


TABLE = ProbeTable.of([
    P(1, "main", "entry"), P(2, "main", "exit"),
    P(3, "f", "entry"), P(4, "f", "exit"),
    P(5, "g", "entry"), P(6, "g", "exit"),
    P(7, "f", "then", decision=7, label="if"), P(8, "f", "else", decision=7, label="else", implicit=True),
    P(9, "g", "var", variable="state", label="state"),
    P(10, "h", "entry"), P(11, "h", "exit", implicit=True, label="end"),
])


def hits(*pairs):
    return [Hit(t, i, v) for t, i, *rest in pairs for v in (rest[0] if rest else None,)]


def test_nested_calls_times_and_tree():
    items = hits((0, 1), (10, 3), (12, 7), (20, 5), (25, 9, 42), (30, 6), (50, 4), (60, 3), (70, 4), (100, 2))
    r = analyze(items, TABLE, hz=1e6)
    calls = sorted(r.calls, key=lambda c: c.t0)
    assert [(c.function, c.t0, c.t1, c.depth, c.self_ticks) for c in calls] == [
        ("main", 0, 100, 0, 50), ("f", 10, 50, 1, 30), ("g", 20, 30, 2, 10), ("f", 60, 70, 1, 10)]
    f = r.functions["f"]
    assert (f.calls, f.inclusive.total, f.inclusive.min, f.inclusive.max, f.exclusive.total) == (2, 50, 10, 40, 40)
    assert f.inclusive.mean == 25 and f.inclusive.stdev == 15
    assert r.tree.children["main"].children["f"].children["g"].count == 1
    assert r.tree.children["main"].children["f"].inclusive == 50
    assert r.variables[9].last == 42 and r.variables[9].values == [(25, 42)]
    assert not r.anomalies and r.open_frames == []


def test_recursion_counts_outermost_only():
    items = hits((0, 3), (10, 3), (20, 3), (30, 4), (40, 4), (50, 4))
    r = analyze(items, TABLE, hz=1.0)
    f = r.functions["f"]
    assert f.calls == 3 and f.total_inclusive == 50 and f.inclusive.total == 50 + 30 + 10
    assert [c.depth for c in sorted(r.calls, key=lambda c: c.t0)] == [0, 1, 2]


def test_missing_exit_is_recovered_and_flagged():
    items = hits((0, 1), (10, 3), (20, 5), (30, 2))          # f・g の出口が無いまま main の出口
    r = analyze(items, TABLE, hz=1.0)
    types = [(a.type, a.function) for a in r.anomalies]
    assert types == [("missing_exit", "g"), ("missing_exit", "f")]
    incomplete = [c for c in r.calls if not c.complete]
    assert {c.function for c in incomplete} == {"f", "g"}
    assert r.functions["f"].inclusive.count == 0 and r.functions["f"].calls == 1
    assert r.functions["main"].inclusive.total == 30


def test_exit_without_entry_and_unknown_id():
    r = analyze(hits((5, 4), (6, 999)), TABLE, hz=1.0)
    assert [a.type for a in r.anomalies] == ["exit_without_entry", "unknown_id"]


def test_loss_closes_open_frames():
    items = hits((0, 1), (10, 3)) + [Loss(15, "x")] + hits((20, 3), (30, 4))
    r = analyze(items, TABLE, hz=1.0)
    assert [a.type for a in r.anomalies] == ["loss"]
    assert sorted((c.function, c.complete) for c in r.calls) == [("f", False), ("f", True), ("main", False)]
    assert r.losses == 1


def test_coverage():
    items = hits((0, 1), (1, 3), (2, 7), (3, 4), (4, 2))
    cov = analyze(items, TABLE, hz=1.0).coverage
    assert (cov.functions_hit, cov.functions_total) == (2, 4)
    # C0: 足した枝（8・11）と var を除く 1,2,3,4,5,6,7,10 のうち 1,2,3,4,7
    assert (cov.c0_hit, cov.c0_total) == (5, 8)
    assert (cov.c1_hit, cov.c1_total) == (1, 2)
    assert cov.decisions[0]["outcomes"][1] == {"id": 8, "label": "else", "kind": "else", "implicit": True,
                                               "count": 0}
    assert [p.id for p in cov.uncovered] == [5, 6, 8, 10]


def test_long_call_flag():
    items = []
    t = 0
    for k in range(40):
        items += hits((t, 5), (t + 100 + (k % 3) - 1, 6))
        t += 1000
    items += hits((t, 5), (t + 300, 6))
    r = analyze(items, TABLE, hz=1e6)
    longs = [a for a in r.anomalies if a.type == "long_call"]
    assert len(longs) == 1 and longs[0].t == t and longs[0].function == "g"


def test_buckets_silence_and_no_exit():
    items = hits((0, 1), (100, 3), (150, 4), (2500, 5), (2600, 6))
    an = Analyzer(TABLE, hz=1000.0, bucket_s=1.0, silence_s=1.0, open_s=2.0)
    an.feed(items)
    r = an.finish(t_end=3000)
    assert [(b.index, b.hits, b.calls) for b in r.buckets] == [(0, 3, 2), (2, 2, 1)]
    assert r.buckets[0].functions == {"main": 1, "f": 1}
    assert [a.type for a in r.anomalies] == ["silence", "no_exit"]
    assert r.open_frames == [("main", 0)]
    assert r.buckets[0].anomalies == 2                 # silence（t=150）・no_exit（t=0）


def test_a_snapshot_does_not_mark_open_calls():
    # 取っている最中の途中の要約（final=False）は no_exit を付けない（何度呼んでも異常が増えない）
    an = Analyzer(TABLE, hz=1000.0, open_s=2.0)
    an.feed(hits((0, 1), (100, 3), (150, 4)))
    for _ in range(2):
        r = an.finish(t_end=3000, final=False)
        assert not r.anomalies and r.open_frames == [("main", 0)]
    assert [a.type for a in an.finish(t_end=3000).anomalies] == ["no_exit"]


def test_intervals():
    items = hits((0, 3), (5, 3), (9, 6), (20, 3), (24, 6), (30, 6)) + [Loss(31, "x")] + hits((32, 3), (35, 6))
    res = intervals(items[:-2] + [Hit(32, 3)] + [Loss(33, "y")] + [Hit(35, 6)], 3, 6, hz=1.0)
    assert res.pairs == [(5, 4), (20, 4)]
    assert res.stats.mean == 4 and res.stats.count == 2


def test_render_calls_and_json():
    r = analyze(hits((0, 1), (10, 3), (20, 4), (30, 2)), TABLE, hz=1e6)
    text = render_calls(r.calls, r.hz)
    lines = text.splitlines()
    assert lines[0].split()[2] == "main" and lines[1].startswith("      10.000 us    f")
    d = r.to_dict()
    json.dumps(d, ensure_ascii=False)
    assert d["functions"][0]["name"] == "main" and d["coverage"]["c1"] == [0, 2]


def test_function_names_disambiguate_static_functions():
    table = ProbeTable.of([Probe(1, "a.c", 1, "init", "entry"), Probe(2, "b.c", 1, "init", "entry"),
                           Probe(3, "a.c", 2, "init", "exit"), Probe(4, "b.c", 2, "init", "exit")])
    r = analyze(hits((0, 1), (1, 2), (2, 4), (3, 3)), table, hz=1.0)
    assert set(r.functions) == {"a.c:init", "b.c:init"} and not r.anomalies


def test_record_to_recorder(tmp_path):
    rec = Recorder(tmp_path / "rec", timebase_hz=100e6)
    items = hits((0, 1), (10, 3), (12, 7), (20, 5), (25, 9, 42), (30, 6), (50, 4), (100, 2), (101, 4))
    report = record_testpoints(rec, items, TABLE, hz=1e6, offset_ticks=1000)
    rec.close()
    rd = Recording.open(tmp_path / "rec")
    events = rd.events(streams=["testpoint"])
    kinds = sorted({e.kind for e in events})
    assert kinds == ["anomaly", "call", "hit"]
    main = [e for e in events if e.kind == "call" and e.body["function"] == "main"][0]
    assert (main.t, main.t_end, main.a, main.b) == (1000, 1000 + 100 * 100, 1, 0)
    var = [e for e in events if e.kind == "hit" and e.a == 9][0]
    assert var.b == 42 and var.body["value"] == 42 and var.t == 1000 + 25 * 100
    assert rd.find({"stream": "testpoint", "kind": "call", "body": {"function": "g"}})[0].t == 1000 + 20 * 100
    assert report.anomalies[0].type == "exit_without_entry"
    rd.close()


def test_recorder_sink_carries_the_time_uncertainty(tmp_path):
    from fprobe.testpoint.record import RecorderSink
    rec = Recorder(tmp_path / "rec", timebase_hz=100e6)
    sink = RecorderSink(rec, hz=1e6, offset_ticks=500, uncertainty=2000.0)
    an = Analyzer(TABLE, hz=1e6, **sink.callbacks())
    an.feed(hits((0, 1), (10, 9, 7), (20, 2)))
    rec.close()
    rd = Recording.open(tmp_path / "rec")
    events = rd.events(streams=["testpoint"])
    assert events and all(e.uncertainty == 2000.0 for e in events)
    rd.close()


# --- 一続き: 挿入 → 実行 → 2 線の波形 → 解読 → 解析 ---

@needs_zig
def test_pipeline_from_source_to_report(tmp_path):
    result, out = run_instrumented(tmp_path, [DATA / "run.c"], Options(variables=(WatchVar("depth"),)))
    log = parse_log(out)
    rate = 100e6
    records = [(1e-6 + k * 10e-6, i, v) for k, (i, v) in enumerate(log)]       # 10 µs ごとに 1 つ
    wave = Wave(8, rate, idle={4: 1, 5: 1}, jitter=3e-9, seed=1)
    synth.gpio_serial(wave, records, clock_channel=4, data_channel=5)
    dec = decode_gpio_serial(wave.build(end_s=records[-1][0] + 20e-6), 4, 5)
    assert [(h.id, h.value) for h in dec.hits] == log
    report = analyze(dec.items, result.table, dec.hz)
    assert not report.anomalies
    fact = report.functions["fact"]
    assert fact.calls == 3 and fact.inclusive.count == 3
    depths = sorted(c.depth for c in report.calls if c.function == "fact")
    assert depths == [0, 1, 2]
    assert report.functions["classify"].calls == 3
    cov = report.coverage
    assert cov.functions_hit == cov.functions_total == 5
    classify = [d for d in cov.decisions if d["function"] == "classify"][0]
    assert [o["count"] for o in classify["outcomes"]] == [1, 1, 1, 1, 1]       # case 0・1・2・3・default
    find_if = [d for d in cov.decisions if d["function"] == "find" and d["outcomes"][0]["kind"] == "then"][0]
    assert [o["count"] for o in find_if["outcomes"]] == [1, 4]                   # 7 で見つかる 1 回、外れ 4 回
    assert cov.c0 == cov.c1 == 1.0 and report.variables          # run_main は全部の枝を通す
    assert "fact" in render_calls(report.calls, report.hz)


@pytest.mark.parametrize("bad", [1.0, 3.0])
def test_long_call_needs_both_sigma_and_ratio(bad):
    """ばらつきが 0 でも、平均の long_ratio 倍を超えなければ印を付けない。"""
    items = []
    for k in range(40):
        items += hits((k * 1000, 5), (k * 1000 + 100, 6))
    items += hits((50_000, 5), (50_000 + int(100 * (1 + bad / 10)), 6))
    r = analyze(items, TABLE, hz=1.0)
    assert [a.type for a in r.anomalies] == (["long_call"] if bad > 2 else [])
