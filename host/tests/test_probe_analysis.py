"""Probe の解析の道具（AnalysisTools）: recording.query・la.measure・recording.waveform_measure。偽のデバイスで。"""

import json
from array import array
from types import SimpleNamespace

import pytest

from fprobe.la import Transitions
from fprobe.mcp_server import Probe
from fprobe.recording_sql import QueryError

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


def test_recording_query_reads_the_recording_being_recorded(probe):
    probe.recording_start(name="q")
    probe.recording_mark(text="一つ目")
    probe.recording_mark(text="二つ目")
    out = probe.recording_query(sql="SELECT json_extract(body, '$.text') AS text FROM events "
                                    "WHERE stream = 'annotation' ORDER BY t")
    assert out["columns"] == ["text"] and out["rows"] == [["一つ目"], ["二つ目"]]
    assert out["recording"] == probe.recording_status()["name"]


def test_recording_query_refuses_writes(probe):
    probe.recording_start(name="q")
    with pytest.raises(QueryError):
        probe.recording_query(sql="DELETE FROM events")


def test_recording_query_without_a_recording_is_an_error(probe):
    with pytest.raises(RuntimeError):
        probe.recording_query(sql="SELECT 1")


def _clock_capture(trigger_time: int = 2_000_000) -> Transitions:
    """周期 10 サンプル（100 ns）・high 3 サンプルのクロック（チャンネル 0）を 10 周期。"""
    values, starts = array("H", [0]), array("Q", [0])
    for k in range(10):
        values.extend([1, 0])
        starts.extend([1 + 10 * k, 4 + 10 * k])
    return Transitions(values, starts, 101, 0, trigger_time, 1e-8, 2)


def test_la_measure_measures_the_last_capture(probe):
    probe._keep_capture(_clock_capture(), "la.capture")
    out = probe.la_measure(relations=[{"type": "delay", "from": 0, "from_edge": "rise", "to": 0, "to_edge": "fall"}])
    ch = out["channels"][0]
    assert ch["frequency_hz"] == pytest.approx(10e6) and ch["duty"] == pytest.approx(0.3)
    assert out["relations"][0]["mean_ns"] == pytest.approx(30)
    assert out["capture"]["id"] == probe._capture_info["id"] and out["at_unit"] == "sample"
    assert probe.la_measure(start=50)["channels"][0]["rises"] == 5


def test_la_measure_without_a_capture_or_with_an_old_id_is_an_error(probe):
    with pytest.raises(RuntimeError):
        probe.la_measure()
    probe._keep_capture(_clock_capture(), "la.capture")
    with pytest.raises(KeyError):
        probe.la_measure(capture_id=probe._capture_info["id"] - 1)


def test_recording_waveform_measure_measures_the_recorded_captures(probe):
    probe.recording_start(name="m")
    probe._keep_capture(_clock_capture(), "la.capture")
    out = probe.recording_waveform_measure()
    assert out["channels"][0]["period"]["count"] == 9 and out["at_unit"] == "timebase"
    assert out["channels"][0]["period"]["min_at"] == 2_000_000 + 1      # サンプル 0 がトリガ時刻（trigger = 0）
    assert probe.recording_waveform_measure(t0=2_000_000 + 50)["channels"][0]["rises"] == 5


def test_recording_export_writes_a_perfetto_trace_with_watched_names(probe, tmp_path):
    from fprobe.probe_watch import _Watched
    probe.recording_start(name="p")
    probe._keep_capture(_clock_capture(), "la.capture")
    probe._recorder.add_many([("sample", "word", 2_000_010, 0x20000010, 5), ("sample", "word", 2_000_020, 0x20000014, 6),
                              ("sample", "word", 2_000_030, 0xE000101C, 0x08000100)])
    probe._watched = [_Watched("speed", 0x20000010, 8)]
    out = probe.recording_export(format="perfetto", path=str(tmp_path / "t.json"), streams=["sample", "waveform"])
    assert out["path"] == str(tmp_path / "t.json") and out["counters"] > 0
    data = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in data["traceEvents"] if e["ph"] == "C"}
    assert {"speed", "speed+4", "PC（DWT_PCSR）", "LA ch0"} <= names


def test_sampled_words_are_named_by_the_elf_symbols(probe, tmp_path):
    probe.recording_start(name="s")
    probe._recorder.add_many([("sample", "word", 10, 0x20000104, 5), ("sample", "word", 20, 0x30000000, 6)])
    probe._recorder.flush()
    variable = SimpleNamespace(name="g_table")
    probe._symbols = SimpleNamespace(symbol_at=lambda a: (variable, a - 0x20000100) if 0x20000100 <= a < 0x20000110
                                     else None)
    names = probe._counter_names()
    assert names[0x20000104] == "g_table+4" and 0x30000000 not in names


def test_timeline_rows_of_sampled_words_are_named_by_expression_or_symbol(probe, tmp_path):
    probe.recording_start(name="t")
    probe._recorder.add_many([("sample", "word", 10, 0x20000104, 5), ("sample", "word", 20, 0x30000000, 6),
                              ("sample", "word", 30, 0x20000200, 7)])
    probe._recorder.flush()
    variable = SimpleNamespace(name="g_table")
    probe._symbols = SimpleNamespace(symbol_at=lambda a: (variable, a - 0x20000100) if 0x20000100 <= a < 0x20000110
                                     else None)
    probe._watched = [SimpleNamespace(name="sensor.value", words=range(0x20000200, 0x20000204, 4))]
    tile = probe.timeline_tiles(source="recording", t0=0, t1=100, streams=["sample"])
    lane = next(lane for lane in tile.result["lanes"] if lane["stream"] == "sample")
    titles = {r["id"]: r["title"] for r in lane["rows"]}
    assert titles["sample.word:0x20000104"] == "g_table+4"
    assert titles["sample.word:0x20000200"] == "sensor.value"                     # 観測している式の名前
    assert titles["sample.word:0x30000000"] == "0x30000000"                       # 分からない番地は番地のまま


def test_recording_export_limits_csv_by_range_and_streams(probe, tmp_path):
    probe.recording_start(name="c")
    probe._recorder.add_many([("uart", "rx", 100, 0, 0x41), ("uart", "rx", 300, 0, 0x42), ("dap", "ap_read", 200)])
    probe.recording_export(format="csv", path=str(tmp_path / "x.csv"), t0=150, streams=["uart"])
    rows = (tmp_path / "x.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2 and rows[1].startswith("300,")
    with pytest.raises(ValueError):
        probe.recording_export(format="vcd", path=str(tmp_path / "x.vcd"), streams=["uart"])


def test_recording_status_tells_whether_memory_is_still_read(probe):
    # 記録中に watch が止まると MCU のメモリは記録に入らない（黙って欠ける）: 状態で分かるように
    probe.recording_start(name="w")
    assert probe.recording_status()["watch_running"] is False
    probe._watch_running = True
    assert probe.recording_status()["watch_running"] is True
