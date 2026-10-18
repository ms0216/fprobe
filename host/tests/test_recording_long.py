"""fprobe.recording_long: 長期の記録（分割・索引・縮図の事前計算・間引き・容量の上限・部分読み込み）。

同じ出来事・波形を普通の記録（Recorder）と長期の記録（SegmentedRecorder）に入れ、読みが一致することを確かめる。
"""

import json
import random
from array import array

import pytest

from fprobe.la import Transitions
from fprobe.recording import Recorder, Recording
from fprobe.recording_long import Policy, SegmentedRecorder, SegmentedRecording
from fprobe.recording_sql import QueryError, query

HZ = 1_000_000                                     # タイムベース 1 MHz（段 0 の幅 = 1024）
STREAMS = ("dap", "uart", "sample", "annotation")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _wave(rng: random.Random, length: int, channels: int = 4) -> Transitions:
    values, starts, v, i = array("H"), array("Q"), rng.randrange(16), 0
    while i < length:
        values.append(v)
        starts.append(i)
        i += rng.randrange(1, 400)
        v ^= 1 << rng.randrange(channels)
    return Transitions(values, starts, length, 0, 0, 1 / HZ, channels)


def _fill(recorders, clock, seed=0, hours=5, per_hour=400):
    """1 時間ごとに出来事と波形を入れる（遅れて届く出来事も混ぜる）。"""
    rng = random.Random(seed)
    for h in range(hours):
        base = h * 3600 * HZ
        for _ in range(per_hour):
            t = base + rng.randrange(0, 3600 * HZ)
            stream = rng.choice(STREAMS)
            for r in recorders:
                r.add(stream, "k", t, a=t % 7)
        late = base - rng.randrange(1, 10 * HZ) if h else base           # 前の分割の時刻の出来事が後から届く
        for r in recorders:
            r.add("dap", "late", max(0, late))
        for k in range(3):
            tr = _wave(rng, 20_000)
            start = base + k * 1000 * HZ
            for r in recorders:
                r.add_waveform(tr, start)
        clock.now += 3600
        for r in recorders:
            r.flush()


@pytest.fixture
def pair(tmp_path):
    clock = Clock()
    plain = Recorder(tmp_path / "plain", HZ)
    long = SegmentedRecorder(tmp_path / "long", HZ, policy=Policy(segment_s=3600), clock=clock)
    _fill([plain, long], clock)
    plain.close()
    long.close()
    yield Recording.open(tmp_path / "plain"), Recording.open(tmp_path / "long"), clock


def test_open_dispatches_to_the_segmented_reader(pair):
    _, long, _ = pair
    assert isinstance(long, SegmentedRecording)
    assert len([s for s in long.segments() if s["state"] == "sealed"]) >= 5


def test_events_count_span_and_find_match_the_plain_recording(pair):
    plain, long, _ = pair
    assert long.count() == plain.count()
    assert long.span() == plain.span()
    assert [(e.t, e.stream) for e in long.events()] == [(e.t, e.stream) for e in plain.events()]
    t0, t1 = 3000 * HZ, 9000 * HZ
    assert long.events(t0, t1, ["dap"], limit=50) == plain.events(t0, t1, ["dap"], limit=50)
    q = {"stream": "dap", "kind": "late"}
    assert long.find(q) == plain.find(q)
    assert long.distinct("stream") == plain.distinct("stream")


@pytest.mark.parametrize("seed", range(6))
def test_density_is_exact(pair, seed):
    plain, long, _ = pair
    rng = random.Random(seed)
    span = plain.span()
    t0 = rng.randrange(span[0], span[1])
    t1 = min(span[1] + 1, t0 + rng.randrange(1, span[1] - span[0]))
    width = rng.randrange(1, 300)
    streams = rng.choice([None, ["dap"], ["uart", "sample"]])
    assert long.density(t0, t1, width, streams) == plain.density(t0, t1, width, streams)


@pytest.mark.parametrize("seed", range(6))
def test_waveform_view_matches_the_plain_recording(pair, seed):
    plain, long, _ = pair
    rng = random.Random(100 + seed)
    span = plain.span()
    t0 = rng.randrange(span[0], span[1])
    t1 = min(span[1] + 1, t0 + rng.randrange(1000, span[1] - span[0]))
    width = rng.randrange(1, 400)
    assert long.waveform_view(t0, t1, width) == plain.waveform_view(t0, t1, width)


def test_waveform_chunks_and_vcd_export(pair, tmp_path):
    plain, long, _ = pair
    a = [(t, tps, tr.length) for t, tps, tr in plain.waveforms()]
    b = [(t, tps, tr.length) for t, tps, tr in long.waveforms()]
    assert a == b
    plain.export_vcd(tmp_path / "a.vcd")
    long.export_vcd(tmp_path / "b.vcd")
    assert (tmp_path / "a.vcd").read_text() == (tmp_path / "b.vcd").read_text()


def test_the_open_segment_is_read_while_recording(tmp_path):
    clock = Clock()
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=10), clock=clock)
    rec.add("uart", "rx", 5)
    rec.flush()
    view = Recording.open(tmp_path / "r")
    assert view.count() == 1 and view.density(0, 100, 2) == [1, 0]
    clock.now += 11
    rec.add("uart", "rx", 50)
    rec.flush()                                                     # 分割を閉じて次を開く
    rec.add("uart", "rx", 70)
    rec.flush()
    assert [e.t for e in view.events()] == [5, 50, 70]
    assert view.density(0, 100, 2) == [1, 2]
    assert [s["state"] for s in view.segments()] == ["sealed", "open"]
    rec.close()


def test_host_events_use_one_clock_across_segments(tmp_path):
    clock = Clock()
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=10), clock=clock)
    rec.clock.add(100.0, 1000, 0.0)
    t1 = rec.add_host("annotation", "mark", 100.5)
    clock.now += 20
    rec.flush()
    t2 = rec.add_host("annotation", "mark", 101.0)
    rec.close()
    assert (t1, t2) == (1000 + HZ // 2, 1000 + HZ)


def test_decimation_drops_bulky_streams_but_keeps_the_minimap(tmp_path):
    clock = Clock()
    plain = Recorder(tmp_path / "plain", HZ)
    long = SegmentedRecorder(tmp_path / "long", HZ, clock=clock,
                             policy=Policy(segment_s=3600, decimate_after_s=2 * 3600,
                                           decimate_streams=("sample", "waveform")))
    _fill([plain, long], clock)
    plain.close()
    long.close()
    p, r = Recording.open(tmp_path / "plain"), Recording.open(tmp_path / "long")
    old = [s for s in r.segments() if s["dropped"]]
    assert old and all(s["dropped"] == ["sample", "waveform"] for s in old)
    first = old[0]
    assert not r.events(first["t0"], first["t1"] + 1, ["sample"])          # 間引いた
    assert r.events(first["t0"], first["t1"] + 1, ["uart"]) == p.events(first["t0"], first["t1"] + 1, ["uart"])
    span = p.span()
    assert r.density(span[0], span[1] + 1, 7) == p.density(span[0], span[1] + 1, 7)   # 縮図は残る
    cov = r.coverage(first["t0"], first["t1"])
    assert not cov["complete"] and cov["gaps"][0]["dropped"] == ["sample", "waveform"]
    view = r.waveform_view(first["t0"], first["t1"], 5)                    # 波形は要約から（画素が大きければ同じ）
    assert all(len(row) == 5 for row in view)
    assert not list(r.waveforms(first["t0"], first["t1"]))


def test_capacity_limit_deletes_the_oldest_segments(tmp_path):
    clock = Clock()
    rec = SegmentedRecorder(tmp_path / "r", HZ, clock=clock, policy=Policy(segment_s=3600, max_bytes=1))
    _fill([rec], clock, hours=4)
    rec.close()
    r = Recording.open(tmp_path / "r")
    states = [s["state"] for s in r.segments()]
    assert states.count("deleted") >= 3 and states[-1] == "sealed"
    assert not (tmp_path / "r" / r.segments()[0]["path"]).exists()
    span = (0, 4 * 3600 * HZ)
    cov = r.coverage(*span)
    assert any(g["state"] == "deleted" for g in cov["gaps"])
    total = sum(r.overview(64)["total"])
    assert total == 4 * 401                                               # 消した分割も粗い縮図に残る


def test_overview_reports_streams_per_pixel(pair):
    plain, long, _ = pair
    ov = long.overview(50)
    span = plain.span()
    assert ov["span"] == list(span)
    assert sum(ov["total"]) == plain.count()
    for stream in ("dap", "uart"):
        assert sum(ov["streams"][stream]) == len(plain.events(streams=[stream]))
    assert len(ov["waveform"]) == 4 and all(len(row) == 50 for row in ov["waveform"])


def test_sql_over_segments(pair):
    plain, long, _ = pair
    with pytest.raises(QueryError):
        query(long.directory, "SELECT COUNT(*) FROM events", t0=0, t1=10 ** 12, max_segments=2)
    out = query(long.directory, "SELECT COUNT(*) FROM events WHERE stream = 'dap'", t0=0, t1=10 ** 12)
    assert out["rows"][0][0] == len(plain.events(streams=["dap"])) and not out["partial"]
    recent = query(long.directory, "SELECT COUNT(*) FROM events", max_segments=2)
    assert recent["partial"] and len(recent["segments"]) == 2
    with pytest.raises(QueryError):
        query(long.directory, "DELETE FROM events")


def test_policy_is_saved_and_validated(tmp_path):
    with pytest.raises(ValueError):
        Policy(segment_s=0)
    with pytest.raises(ValueError):
        Policy(decimate_after_s=10, decimate_streams=())                  # 何を間引くかが無い
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=60, max_bytes=10 ** 9))
    rec.close()
    meta = json.loads((tmp_path / "r" / "meta.json").read_text(encoding="utf-8"))
    assert meta["layout"] == "segmented" and meta["policy"]["max_bytes"] == 10 ** 9
