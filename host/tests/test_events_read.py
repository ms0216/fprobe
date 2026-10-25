"""events.read の続きの読み（since）: 前回の続きから取りこぼしなく読み、上書きで失った数を知らせる。
2026-10-18、last だけで繰り返し読んだ測定が、読みの間に書かれた分を取りこぼして「サンプラが止まった」ように見えた。"""

from types import SimpleNamespace

import pytest

from fprobe import probe_events
from fprobe.mcp_server import Probe
from probe_fakes import FakeDevice, use_fake_ring


def probe_with_ring(monkeypatch, capacity):
    use_fake_ring(monkeypatch, probe_events)
    dev = FakeDevice(ring_capacity=capacity)
    dev.ring_running = True
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    return p, dev


def values(r):
    return [int(x["value"], 16) for x in r["items"]]


def test_reading_since_the_previous_read_misses_nothing(monkeypatch):
    p, dev = probe_with_ring(monkeypatch, capacity=10)
    for i in range(6):
        dev.sample(1000 + i, 0x20000000, i)
    r = p.events_read(since=0, last=4)
    assert values(r) == [0, 1, 2, 3] and (r["first"], r["next"], r["skipped"]) == (0, 4, 0)
    r = p.events_read(since=r["next"])
    assert values(r) == [4, 5] and r["next"] == 6
    assert values(p.events_read(since=6)) == [] and p.events_read(since=6)["next"] == 6


def test_events_overwritten_before_reading_are_counted_as_skipped(monkeypatch):
    p, dev = probe_with_ring(monkeypatch, capacity=10)
    for i in range(20):
        dev.sample(1000 + i, 0x20000000, i)
    r = p.events_read(since=6, last=100)
    assert r["skipped"] == 4 and values(r) == list(range(10, 20)) and (r["first"], r["next"]) == (10, 20)


def test_without_since_it_is_still_the_latest(monkeypatch):
    p, dev = probe_with_ring(monkeypatch, capacity=10)
    for i in range(20):
        dev.sample(1000 + i, 0x20000000, i)
    r = p.events_read(last=3)
    assert values(r) == [17, 18, 19] and (r["first"], r["next"]) == (17, 20) and "skipped" not in r


def test_event_times_keep_increasing_across_the_48_bit_wrap(monkeypatch):
    """イベントの時刻は 48 ビット。折り返しをまたいでも time_us はタイムベース（64 ビット）で単調に増える"""
    p, dev = probe_with_ring(monkeypatch, capacity=10)
    wrap = 1 << 48
    dev.ticks = 3 * wrap + 1000
    dev.sample(wrap - 100, 0x20000000, 0)
    dev.sample(50, 0x20000000, 1)
    dev.ticks = 3 * wrap + 1000
    for raw in (False, True):
        t = [x["time_us"] for x in p.events_read(raw=raw)["items"]]
        assert t[0] < t[1] and t[1] - t[0] == pytest.approx(1.5, abs=0.01)     # 150 刻み = 1.5 µs
