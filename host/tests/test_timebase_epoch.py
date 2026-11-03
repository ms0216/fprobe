"""タイムベースの時代の切れ目（docs/superpowers/specs/2026-11-01-time-accuracy-design.md §14.1、N8）: タイムベースが 0 に
戻ったら（ビットストリームの書き込み・電源・クロックのロック外れ）、事実として記録し、前後の時代を 1 本の軸に混ぜない。"""

from types import SimpleNamespace

import pytest

from fprobe.collector import Collector, sample_clock
from fprobe.recording import ClockMap, Recorder, Recording

HZ = 1e6


def test_a_recording_ends_at_a_timebase_restart_and_keeps_out_later_rows(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    assert rec.observe_time(1_000) and rec.observe_time(5_000)
    rec.add("annotation", "x", 4_000)
    assert not rec.observe_time(20)                                      # 0 に戻った
    rec.add("annotation", "after", 30)                                   # 新しい時代の行は入れない
    rec.add_many([("annotation", "after", 40)])
    rec.close()
    r = Recording.open(tmp_path / "r")
    rows = [(e.stream, e.kind, e.t) for e in r.events()]
    assert ("annotation", "after", 30) not in rows and ("annotation", "x", 4_000) in rows
    (fact,) = [e for e in r.events() if e.stream == "timebase"]
    assert (fact.kind, fact.t, fact.body["ticks_after"]) == ("restart", 5_000, 20)
    assert r.meta["ended"]["reason"] == "timebase_restart" and r.meta["rejected_rows"] == 2
    r.close()


def test_the_clock_map_starts_over_at_a_new_epoch():
    m = ClockMap(HZ)
    m.add(10.0, 10_000_000, 0.0)
    m.add(11.0, 11_000_000, 0.0)
    m.add(12.0, 5, 0.0)                                                  # 0 に戻った: 前の点と 1 本の直線にしない
    assert m.restarts == 1
    t, _ = m.to_ticks(12.5)
    assert t == 500_005                                                  # 新しい時代の 1 点と公称の速さ（幅 0.5 s）


class _Ring:
    def __init__(self):
        self.written = 0
        self.capacity = 16
        self.overwritten = 0
        self.events = []

    def counts(self):
        return self.written, 0

    def read(self, last=None, end=None):
        return self.events[:end][-last:] if last else []


def test_the_collector_counts_an_event_log_restart_and_reads_from_the_start(tmp_path):
    from fprobe.events import KIND_SWO, DapEvent
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    dev = SimpleNamespace(time=lambda: 1_000_000)
    c = Collector.__new__(Collector)
    c.rec, c.device, c.events, c._seen, c._carry, c.lost, c.restarts = rec, dev, _Ring(), 10, None, 0, 0
    c.obs = SimpleNamespace(events_lost=lambda: None, feed=lambda e: [])
    ring = c.events
    ring.events = [DapEvent(100 + i, KIND_SWO, True, True, 0, 0, False, 0, i) for i in range(3)]
    ring.written = 3                                                      # 始め直された（読んだ数 10 より少ない）
    c._read_new()
    rec.close()
    r = Recording.open(tmp_path / "r")
    assert c.restarts == 1 and c._seen == 3
    assert [e.b for e in r.events(streams=["swo"])] == [0, 1, 2]
    (note,) = [e for e in r.events() if e.kind == "events_restarted"]
    assert note.body == {"written": 3, "seen": 10}
    r.close()


def test_sample_clock_feeds_the_restart_check(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    ticks = iter([1_000, 50])
    dev = SimpleNamespace(time=lambda: next(ticks))
    assert sample_clock(dev, rec) is True
    assert sample_clock(dev, rec) is False                               # 0 に戻った: 時計の点も入れない
    rec.close()
    r = Recording.open(tmp_path / "r")
    assert [e.t for e in r.events(streams=["clock"])] == [1_000] and r.meta["ended"]["reason"] == "timebase_restart"
    r.close()


def test_ending_for_a_reason_is_kept_in_the_meta(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.end("design_switch", before="fprobe", after="fprobe_pynq_fast")
    rec.end("link_lost")                                                 # 最初の理由のまま
    rec.close()
    r = Recording.open(tmp_path / "r")
    assert r.meta["ended"] == {"reason": "design_switch", "before": "fprobe", "after": "fprobe_pynq_fast"}
    r.close()
    with pytest.raises(Exception):
        Recorder(tmp_path / "r2", timebase_hz=HZ).end("")
