"""イベント記録の読み出しを、順序つきのモデル（無限に長いリスト = 書いた番号 0, 1, 2, ... と、capacity で切った窓）と
突き合わせる。ランダムな書き込み・読み出しの並びで、保存則（書いた数 = 読めた数 + 上書きで消えた数 + 読み飛ばし）と
順序（番号の連続・二重なし・欠けの申告）を確かめる。"""

import random
from types import SimpleNamespace

import pytest

from fprobe import probe_events
from fprobe.collector import Collector
from fprobe.events import EventLog
from fprobe.mcp_server import Probe
from fprobe.recording import Recorder, Recording
from test_events_overwrite import RingDevice

HZ = 100_000_000


def test_cursor_conserves_events_over_random_sequences():
    for seed in range(300):
        rnd = random.Random(seed)
        capacity = rnd.choice([1, 2, 3, 5, 8, 10, 16])
        dev = RingDevice(capacity=capacity, written=rnd.randrange(0, 30), advance=0)
        cursor = EventLog(dev).cursor()
        start = dev.written
        got: list[int] = []
        for _ in range(20):
            dev.written += rnd.randrange(0, 3 * capacity)
            dev.advance = rnd.randrange(0, 2 * capacity)
            events = cursor.take(limit=rnd.choice([1 << 16, 1, 2, capacity, capacity + 3]))
            times = [e.time for e in events]
            assert times == list(range(times[0], times[0] + len(times))) if times else True   # 連続
            assert not got or not times or times[0] > got[-1]                                   # 二重なし・古い順
            got += times
        # 保存則: 読んだ時点までに書かれた数 = 読めた数 + 数えた欠け
        assert len(got) + cursor.lost == cursor.seen - start, seed
        assert all(start <= t < cursor.seen for t in got)


def test_read_with_end_returns_a_contiguous_window_and_counts_the_overwritten():
    for seed in range(300):
        rnd = random.Random(seed)
        capacity = rnd.choice([1, 2, 3, 5, 8, 10])
        written = rnd.randrange(0, 40)
        dev = RingDevice(capacity=capacity, written=written, advance=rnd.randrange(0, 25))
        log = EventLog(dev)
        last = rnd.choice([None, 0, 1, 3, capacity, 50])
        end = rnd.randrange(0, written + 1) if rnd.random() < 0.5 else None
        top = written if end is None else end
        n = min(top, capacity) if last is None else min(last, min(top, capacity))
        events = log.read(last, end=end)
        times = [e.time for e in events]
        assert len(times) + log.overwritten == n, seed
        assert times == list(range(top - len(times), top)), seed            # end 件目で終わる連続した番号


def test_negative_last_reads_nothing():
    dev = RingDevice(capacity=8, written=5, advance=0)
    assert EventLog(dev).read(-1) == []


def test_overwritten_is_reset_by_an_empty_read():
    dev = RingDevice(capacity=8, written=8, advance=5)
    log = EventLog(dev)
    log.read()
    assert log.overwritten == 5
    dev.advance = 0
    assert log.read(0) == [] and log.overwritten == 0


def probe_on(dev):
    p = Probe("unused")
    dev.capabilities = lambda: SimpleNamespace(timebase_hz=HZ)
    p._session = SimpleNamespace(device=dev)
    p._la_idle = lambda: None
    return p


def test_events_read_since_stays_consistent_when_the_writer_overtakes_the_read():
    dev = RingDevice(capacity=8, written=8, advance=5)
    p = probe_on(dev)
    r = p.events_read(since=0, last=8)
    times = [int(round(x["time_us"] * HZ / 1e6)) for x in r["items"]]
    assert times == [5, 6, 7]                                   # 0〜4 は読む間に上書きされた
    assert r["first"] == 5 and r["next"] == 8                    # 返した最初の番号と、続きの番号が実際の中身と合う
    assert r["skipped"] == 5                                     # 読めなかった 0〜4 は読み飛ばしに数える


def test_events_read_since_chain_conserves_events():
    for seed in range(200):
        rnd = random.Random(seed)
        capacity = rnd.choice([2, 3, 5, 8])
        dev = RingDevice(capacity=capacity, written=0, advance=0)
        p = probe_on(dev)
        nxt, got, skipped = 0, [], 0
        for _ in range(15):
            dev.written += rnd.randrange(0, 2 * capacity)
            dev.advance = rnd.randrange(0, capacity)
            r = p.events_read(since=nxt, last=rnd.choice([1, 3, 100]))
            times = [int(round(x["time_us"] * HZ / 1e6)) for x in r["items"]]
            assert times == list(range(r["first"], r["next"])), seed
            assert r["first"] >= nxt
            skipped += r["first"] - nxt
            got += times
            nxt = r["next"]
        # 次に since に渡す番号まで: 読めた数 + 読み飛ばし = 渡した番号
        assert len(got) + skipped == nxt, seed
        assert len(set(got)) == len(got)


def test_collector_counts_events_overwritten_during_a_read(tmp_path):
    dev = RingDevice(capacity=8, written=0, advance=0)
    log = EventLog(dev)
    dev.time = lambda: 1_000_000
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    c = Collector(dev, rec, events=log, poll_s=1000, clock_s=1000)
    dev.written, dev.advance = 8, 5                     # 8 件を読む間に 5 件書かれ、0〜4 が 8〜12 に上書きされる
    c._read_new()
    assert c.lost == 5                                   # 読めなかった分は欠落として数える


def test_window_counts_events_overwritten_during_a_read_as_lost():
    from fprobe.probe_window import EventWindow, WindowTools

    dev = RingDevice(capacity=8, written=0, advance=0)
    dev.time = lambda: 1_000_000
    window = EventWindow(0, False, 0)
    dev.written, dev.advance = 8, 5
    me = SimpleNamespace(session=SimpleNamespace(device=dev))
    WindowTools._ring_events(me, window, "uart")
    assert window.lost == 5
