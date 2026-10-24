"""fprobe.recording: 共通の時間軸の記録（時計の対応付け・書き手・読み手）。"""

import csv
import json
import time
from array import array

import pytest

from fprobe.la import Transitions
from fprobe.recording import ClockMap, Recorder, Recording

HZ = 100_000_000                                   # タイムベース 100 MHz


# --- 時計の対応付け --------------------------------------------------------------

def test_one_point_uses_the_nominal_rate_and_says_the_rate_is_not_known():
    # 1 点では速さが分からない: 公称の速さで写し、不確かさには点からの経過時間をそのまま入れる（速さの誤りの上限を
    # 仮定しない）。点の上では往復の半分だけ
    m = ClockMap(HZ)
    m.add(host_s=10.0, ticks=5_000, round_trip_s=0.0002)
    t, unc = m.to_ticks(10.5)
    assert t == 5_000 + HZ // 2 and unc == pytest.approx(HZ * 0.0001 + HZ * 0.5)
    assert m.to_ticks(10.0)[1] == pytest.approx(HZ * 0.0001)


def test_offset_and_drift_are_fitted_with_the_slope_uncertainty():
    m = ClockMap(HZ)
    for i in range(10):                             # デバイスの時計がホストより 100 ppm 速い
        m.add(host_s=100.0 + i, ticks=round(7_000 + i * HZ * 1.0001), round_trip_s=0.0004)
    t, unc = m.to_ticks(105.5)
    u = HZ * 0.0002                                  # 各点の誤りの上限（往復の半分）
    slope = u * 25 / 82.5                            # 傾きの誤りの上限 = u Σ|x − x̄| / Σ(x − x̄)²（x̄ = 104.5）
    assert abs(t - (7_000 + 5.5 * HZ * 1.0001)) < 2 and unc == pytest.approx(u + slope * 1.0)


def test_close_points_are_fitted_without_a_threshold_and_the_slope_uncertainty_grows_with_distance():
    # 以前は点の幅が 1 ms 未満なら公称の速さ（境界の値）だった。幅が狭いことは傾きの不確かさに出す
    m = ClockMap(HZ)
    m.add(host_s=1.0, ticks=1_000, round_trip_s=0.0001)
    m.add(host_s=1.0002, ticks=1_000 + round(HZ * 0.0002), round_trip_s=0.0001)
    near = m.to_ticks(1.0001)[1]
    far = m.to_ticks(2.0)[1]
    assert near < far and far > HZ * 0.1                # 0.2 ms の幅から 1 秒先へ伸ばすと大きく外れうる


def test_without_points_host_times_cannot_be_placed():
    with pytest.raises(RuntimeError):
        ClockMap(HZ).to_ticks(1.0)


# --- 書き手と読み手 ----------------------------------------------------------------

@pytest.fixture
def rec(tmp_path):
    r = Recorder(tmp_path / "r1", timebase_hz=HZ, meta={"device": {"link": "usb"}})
    yield r
    r.close()


def test_events_are_read_back_in_time_order_by_range_and_stream(rec):
    rec.add("dap", "ap_read", 300, a=0x2000_0000, b=7)
    rec.add("uart", "rx", 100, a=0x41)
    rec.add("host", "call", 200, body={"client": "claude", "method": "target.halt"}, uncertainty=50)
    rec.flush()
    r = Recording.open(rec.directory)
    assert [(e.t, e.stream) for e in r.events()] == [(100, "uart"), (200, "host"), (300, "dap")]
    assert [e.kind for e in r.events(t0=150, t1=400, streams=["dap", "host"])] == ["call", "ap_read"]
    host = r.events(streams=["host"])[0]
    assert host.body["method"] == "target.halt" and host.uncertainty == 50
    assert r.meta["device"] == {"link": "usb"} and r.meta["timebase_hz"] == HZ


def test_a_recording_can_be_read_while_it_is_being_written(rec):
    rec.add("uart", "rx", 1, a=1)
    rec.flush()
    r = Recording.open(rec.directory)
    rec.add("uart", "rx", 2, a=2)
    rec.flush()
    assert [e.t for e in r.events()] == [1, 2]


def test_events_survive_a_crash_after_flush(tmp_path):
    r = Recorder(tmp_path / "r", timebase_hz=HZ)
    r.add("uart", "rx", 1, a=1)
    r.flush()
    r._abandon()                                   # 閉じずに落ちたことにする
    assert [e.t for e in Recording.open(tmp_path / "r").events()] == [1]


def waveform(values, starts, length, period_s=1e-8, channels=2):
    return Transitions(array("H", values), array("Q", starts), length, 0, 0, period_s, channels)


def test_waveforms_are_placed_on_the_timebase_and_viewed_across_chunks(rec):
    # 10 ns/サンプル = 1 タイムベース/サンプル。2 つのかたまりが時刻 1000 と 2000 から
    rec.add_waveform(waveform([0, 1], [0, 50], 100), start_ticks=1000)
    rec.add_waveform(waveform([1, 0], [0, 50], 100), start_ticks=2000)
    rec.flush()
    r = Recording.open(rec.directory)
    assert r.waveform_view(1000, 1100, 2)[0] == "01"
    assert r.waveform_view(2000, 2100, 2)[0] == "10"
    gap = r.waveform_view(1100, 2000, 3)[0]
    assert gap == "---"                           # かたまりの無い時間は「無い」（-）
    assert r.span() == (1000, 2100)


def test_find_and_intervals_give_lists_and_statistics(rec):
    for i in range(5):                            # 開始 → 終了が 10, 20, 30, 40, 50 ティック
        rec.add("rtt", "line", i * 1000, body={"text": f"start {i}"})
        rec.add("rtt", "line", i * 1000 + (i + 1) * 10, body={"text": f"end {i}"})
    rec.add("dap", "ap_write", 50, a=0x4000_0000, b=1)
    rec.flush()
    r = Recording.open(rec.directory)
    found = r.find({"stream": "rtt", "body": {"text": {"regex": "^end"}}})
    assert [e.t for e in found] == [10, 1020, 2030, 3040, 4050]
    assert [e.t for e in r.find({"stream": "dap", "a": {"ge": 0x4000_0000}})] == [50]
    stats = r.intervals({"stream": "rtt", "body": {"text": {"regex": "^start"}}},
                        {"stream": "rtt", "body": {"text": {"regex": "^end"}}})
    assert [d for _, d in stats["intervals"]] == [10, 20, 30, 40, 50]
    assert (stats["min"], stats["max"], stats["mean"], stats["count"]) == (10, 50, 30, 5)


def test_density_counts_events_per_pixel(rec):
    for t in [0, 1, 2, 50, 99]:
        rec.add("dap", "ap_read", t)
    rec.flush()
    assert Recording.open(rec.directory).density(0, 100, 4, streams=["dap"]) == [3, 0, 1, 1]


def test_export_writes_vcd_csv_and_json(rec, tmp_path):
    rec.add_waveform(waveform([0, 3], [0, 10], 20), start_ticks=0)
    rec.add("uart", "rx", 5, a=0x41)
    rec.flush()
    r = Recording.open(rec.directory)
    vcd = tmp_path / "w.vcd"
    r.export_vcd(vcd)
    text = vcd.read_text()
    assert "$timescale 1 ns" in text and "#100\n" in text      # 時刻は ns（タイムベースは 10 の冪とは限らない）
    out = tmp_path / "e.csv"
    r.export_csv(out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0]["stream"] == "uart" and rows[0]["a"] == "65"
    js = tmp_path / "e.json"
    r.export_json(js)
    assert json.loads(js.read_text(encoding="utf-8"))[0]["kind"] == "rx"


def test_a_million_events_are_written_and_summarized_quickly(rec):
    n = 1_000_000
    t0 = time.perf_counter()
    rec.add_many(("dap", "ap_read", i * 10, i, 0) for i in range(n))
    rec.flush()
    write_s = time.perf_counter() - t0
    r = Recording.open(rec.directory)
    t0 = time.perf_counter()
    d = r.density(0, n * 10, 2000, streams=["dap"])
    assert time.perf_counter() - t0 < 0.5 and sum(d) == n
    assert write_s < 10


def test_iter_events_can_include_intervals_that_began_before_the_range(tmp_path):
    rec = Recorder(tmp_path / "r", 1e8)
    rec.add_many([("rtos", "run", 10, 0, 0, None, 50), ("rtos", "run", 50, 0, 0, None, 90), ("uart", "rx", 20)])
    rec.close()
    r = Recording.open(tmp_path / "r")
    try:
        assert [e.t for e in r.iter_events(t0=30)] == [50]
        assert [e.t for e in r.iter_events(t0=30, overlapping=True)] == [10, 50]
        assert [e.t for e in r.iter_events(t0=50, overlapping=True)] == [50]       # t0 ちょうどに終わるものは含めない
    finally:
        r.close()


def test_distinct_values_of_a_column(tmp_path):
    rec = Recorder(tmp_path / "r", 1e8)
    rec.add_many([("sample", "word", 1, 0x20), ("sample", "word", 2, 0x10), ("sample", "word", 3, 0x20),
                  ("uart", "rx", 4)])
    rec.close()
    r = Recording.open(tmp_path / "r")
    try:
        assert r.distinct("a", streams=["sample"]) == [0x10, 0x20]
        assert r.distinct("stream") == ["sample", "uart"]
        with pytest.raises(ValueError):
            r.distinct("body")
    finally:
        r.close()


def test_the_waveform_cache_is_bounded(tmp_path, monkeypatch):
    # 記録のかたまりの変化点の表は使った順に覚え、上限を超えたら古いものから捨てる（捨てても同じ結果）
    from array import array

    from fprobe import recording as recording_mod
    from fprobe.la import Transitions
    rec = Recorder(tmp_path / "r", timebase_hz=1e6)
    for k in range(6):
        values = array("H", [i & 1 for i in range(200)])
        starts = array("Q", range(200))
        rec.add_waveform(Transitions(values, starts, 200, 0, 0, 1e-6, 1), start_ticks=k * 1000)
    rec.close()
    view = Recording.open(tmp_path / "r")
    expected = view.waveform_view(0, 6000, 60)
    monkeypatch.setattr(recording_mod, "WAVE_CACHE_BYTES", 2 * 199 * 4)          # かたまり 2 つ分
    small = Recording.open(tmp_path / "r")
    assert small.waveform_view(0, 6000, 60) == expected
    assert small.waveform_view(0, 6000, 60) == expected
    assert len(small._waves) <= 2


def test_span_and_distinct_use_the_indexes_and_follow_new_events(tmp_path):
    # 範囲は区間の終わり（t_end）も含める。stream の一覧は記録が増えたら読み直す（増えていなければ前の答え）
    rec = Recorder(tmp_path / "r", timebase_hz=1e6)
    rec.add("dap", "ap_read", 100)
    rec.add("rtos", "run", 50, t_end=900)
    rec.flush()
    view = Recording.open(tmp_path / "r")
    assert view.span() == (50, 900) and view.distinct("stream") == ["dap", "rtos"]
    rec.add("uart", "rx", 1200)
    rec.flush()
    assert view.span() == (50, 1200) and view.distinct("stream") == ["dap", "rtos", "uart"]
    plans = " ".join(str(r) for r in view._db.execute(
        "EXPLAIN QUERY PLAN SELECT a, t FROM events WHERE stream = 'fpga' AND kind = 'group' AND t < 5 "
        "ORDER BY t DESC LIMIT 1"))
    assert "events_stream_kind_t" in plans
    rec.close()


def test_meta_is_replaced_whole_so_a_failed_write_keeps_the_old_one(tmp_path, monkeypatch):
    # 書く途中で失敗しても meta.json が半分にならない（一時ファイルに書いてから置き換える。R17）
    from pathlib import Path
    rec = Recorder(tmp_path / "r", timebase_hz=1e6)
    rec.close()
    view = Recording(tmp_path / "r")
    before = (tmp_path / "r" / "meta.json").read_text(encoding="utf-8")
    real = Path.write_text

    def half(self, data, *a, **kw):
        real(self, data[: len(data) // 2], *a, **kw)
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", half)
    with pytest.raises(OSError):
        view.update_meta(note="x" * 1000)
    monkeypatch.undo()
    assert (tmp_path / "r" / "meta.json").read_text(encoding="utf-8") == before
    assert json.loads(before)
