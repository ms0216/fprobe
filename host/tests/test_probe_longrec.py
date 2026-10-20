"""Probe の長期の記録の道具（U23）: 分割した記録・分割の一覧・全体の縮図・ロジアナの流し続け・道具の排他。偽のデバイスで。"""

import random
import time
from array import array
from types import SimpleNamespace

import pytest

from fprobe.la import Transitions
from fprobe.mcp_server import Probe
from test_la_stream import raw_source
from test_recording_stream import TimedStreamDevice

HZ = 100_000_000


class FakeDevice:
    def __init__(self):
        self.ticks = 1_000_000

    def time(self):
        self.ticks += 1000
        return self.ticks

    def read32(self, address):
        return 0                                     # イベント記録の容量 0 = 無い構成

    def capabilities(self):
        return SimpleNamespace(timebase_hz=HZ, board="fake", design="fprobe")


class StreamDevice(TimedStreamDevice):
    def capabilities(self):
        return SimpleNamespace(mem_bytes=128 << 20, la_channels=16, la_sample_hz=HZ, timebase_hz=HZ, board="fake",
                               design="fprobe")

    def read32(self, address):
        from fprobe import generated as g
        if address in (g.LA_DEEP_RING_BEATS, g.LA_DEEP_STATUS, g.LA_DEEP_STREAM_BEATS, g.MEM_STATUS):
            return super().read32(address)
        return 0


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    if p._recorder is not None:
        p.recording_stop()


def test_a_plain_recording_is_not_segmented(probe):
    probe.recording_start(name="plain")
    probe.recording_mark(text="a")
    assert probe.recording_segments()["segmented"] is False
    ov = probe.recording_overview(width=4)
    assert sum(ov["total"]) >= 1 and "annotation" in ov["streams"]
    assert probe.recording_status()["segmented"] is False


def test_a_closed_segmented_recording_does_not_take_marks_afterwards(probe):
    name = probe.recording_start(name="long", segment_s=60)["recording"]
    probe.recording_stop()
    probe.recording_open(name)
    with pytest.raises(RuntimeError, match="長期の記録"):
        probe.recording_mark(text="後から", t=10)


def test_a_segmented_recording_rotates_and_reports_segments(probe):
    probe.recording_start(name="long", segment_s=0.05, max_bytes=10 ** 9)
    probe.recording_mark(text="一つ目")
    time.sleep(0.4)                                  # 集める側の flush で分割が回る
    probe.recording_mark(text="二つ目")
    probe._recorder.flush()
    seg = probe.recording_segments()
    assert seg["segmented"] and seg["policy"]["segment_s"] == 0.05
    assert len(seg["segments"]) >= 2 and seg["segments"][-1]["state"] == "open"
    marks = probe.recording_find({"stream": "annotation"})
    assert [e["body"]["text"] for e in marks["events"]] == ["一つ目", "二つ目"]
    ov = probe.recording_overview(width=8)
    assert sum(ov["streams"]["annotation"]) == 2
    span = probe.recording_status()["span"]
    assert sum(probe.recording_density(t0=span[0], t1=span[1] + 1, width=3)["counts"]) == probe.recording_status()["events"]
    out = probe.recording_query(sql="SELECT COUNT(*) FROM events WHERE stream = 'annotation'", t0=span[0], t1=span[1] + 1)
    assert out["rows"][0][0] == 2 and out["segments"]
    probe.recording_stop()
    probe.recording_open(probe.recording_list()["recordings"][0]["name"])
    assert probe.recording_segments()["segments"][-1]["state"] == "sealed"


def test_bad_policy_is_refused(probe):
    with pytest.raises(ValueError):
        probe.recording_start(decimate_after_s=10, decimate_streams=[])


def test_la_stream_into_the_recording_excludes_other_la_tools(probe, monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)
    rng = random.Random(5)
    dev = StreamDevice(raw_source(rng, 3000), rng=rng, trigger=True)
    probe._session = SimpleNamespace(device=dev)
    probe.recording_start(name="la", segment_s=3600)
    status = probe.recording_la_stream_start(chunk_s=5e-5, rle=False)
    assert status["running"] and status["anchor"]["method"] == "trigger"
    with pytest.raises(RuntimeError, match="流している"):
        probe.la_arm()
    with pytest.raises(RuntimeError, match="流している"):
        probe.events_read()
    assert probe._collector._paused.is_set() and probe.recording_status()["la_stream"]["running"]
    deadline = time.monotonic() + 5
    while dev.written < 3000 and time.monotonic() < deadline:
        time.sleep(0.005)
    stopped = probe.recording_la_stream_stop()
    assert not stopped["running"] and stopped["samples"] == dev.written * 8 and stopped["blocks"] >= 1
    assert not probe._collector._paused.is_set()
    levels = probe.recording_overview(width=10)["waveform"]
    assert len(levels) == 16
    kinds = [e["kind"] for e in probe.recording_events(streams=["la"])["events"]]
    assert kinds == ["stream_start", "stream_stop"]


def test_stopping_the_recording_stops_the_stream(probe, monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    rng = random.Random(6)
    dev = StreamDevice(raw_source(rng, 500), rng=rng, trigger=False)
    probe._session = SimpleNamespace(device=dev)
    probe.recording_start(name="la2")
    probe.recording_la_stream_start(chunk_s=1.0)
    probe.recording_stop()
    assert probe._la_record is None
    with pytest.raises(RuntimeError):
        probe.recording_la_stream_stop()


def test_la_stream_needs_a_recording(probe):
    with pytest.raises(RuntimeError, match="記録していない"):
        probe.recording_la_stream_start()


def test_capture_before_stream_is_refused(probe):
    probe.recording_start(name="c")
    probe._la = object()                             # 浅いロジアナを仕掛けている
    with pytest.raises(RuntimeError, match="仕掛けている"):
        probe.recording_la_stream_start()
    probe._la = None
    tr = Transitions(array("H", [0, 1]), array("Q", [0, 30]), 100, 30, 2_000_000, 1e-8, 1)
    probe._keep_capture(tr, "la.capture")
