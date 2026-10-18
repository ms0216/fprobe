"""Probe の記録の道具（recording.*）: 偽のデバイスで、始める・捕捉を取り込む・読む・検索・印・書き出し。"""

import time
from array import array
from types import SimpleNamespace

import pytest

from fprobe.la import Transitions
from fprobe.mcp_server import Probe

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


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    if p._recorder is not None:
        p.recording_stop()


def test_a_recording_takes_captures_and_marks_and_can_be_read_and_searched(probe, tmp_path):
    started = probe.recording_start(name="試し")
    assert started["recording"].endswith("試し") and probe.recording_status()["recording"]
    tr = Transitions(array("H", [0, 1]), array("Q", [0, 30]), 100, 30, 2_000_000, 1e-8, 1)
    probe._keep_capture(tr, "la.capture")                        # 捕捉は記録中なら記録にも入る
    probe.recording_mark(text="ここで化けた")
    time.sleep(0.05)
    status = probe.recording_status()
    assert status["events"] >= 2 and status["span"][0] <= 2_000_000 - 30
    marks = probe.recording_find({"stream": "annotation", "body": {"text": {"regex": "化け"}}})
    assert len(marks["events"]) == 1 and marks["events"][0]["uncertainty"] is not None
    view = probe.recording_waveform(t0=2_000_000 - 30, t1=2_000_000 + 70, width=2)
    assert view["levels"][0] in ("0x", "x1", "01")
    stopped = probe.recording_stop()
    assert not probe.recording_status()["recording"] and stopped["recording"] == started["recording"]


def test_saved_recordings_are_listed_opened_and_exported(probe, tmp_path):
    first = probe.recording_start(name="a")["recording"]
    probe.recording_mark(text="一つ目")
    probe.recording_stop()
    names = [r["name"] for r in probe.recording_list()["recordings"]]
    assert any(n.endswith("a") for n in names)
    probe.recording_open(first)
    assert [e["body"]["text"] for e in probe.recording_events(streams=["annotation"])["events"]] == ["一つ目"]
    out = tmp_path / "x.csv"
    probe.recording_export(format="csv", path=str(out))
    assert "一つ目" in out.read_text(encoding="utf-8")


def test_reading_without_a_recording_is_an_error(probe):
    with pytest.raises(RuntimeError):
        probe.recording_events()


def test_a_mark_can_be_placed_at_a_given_time_while_recording(probe):
    probe.recording_start(name="t")
    out = probe.recording_mark(text="ここから遅い", t=1_234_567)
    assert out == {"t": 1_234_567}
    found = probe.recording_find({"stream": "annotation", "kind": "mark"})["events"]
    assert [(e["t"], e["body"]["text"]) for e in found] == [(1_234_567, "ここから遅い")]


def test_a_mark_can_be_added_to_an_opened_recording_at_a_given_time(probe):
    name = probe.recording_start(name="saved")["recording"]
    probe.recording_stop()
    probe.recording_open(name)
    assert probe.recording_mark(text="後から見て気づいた", t=42) == {"t": 42}
    probe.recording_open(name)                                    # 開き直しても残る（ファイルに書いた）
    found = probe.recording_find({"stream": "annotation", "kind": "mark"})["events"]
    assert [(e["t"], e["body"]["text"]) for e in found] == [(42, "後から見て気づいた")]


def test_a_mark_on_an_opened_recording_needs_a_time(probe):
    name = probe.recording_start(name="saved")["recording"]
    probe.recording_stop()
    probe.recording_open(name)
    with pytest.raises(RuntimeError, match="t（"):
        probe.recording_mark(text="いま")
