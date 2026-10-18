"""記録の Perfetto への書き出し（fprobe.export.perfetto。Chrome JSON のトレースイベント）。"""

import json

import pytest

from fprobe.export.perfetto import write_trace
from fprobe.la import Capture, Transitions
from fprobe.recording import Recorder, Recording

HZ = 100_000_000                       # 1 タイムベース = 10 ns
ORIGIN = 1_000_000


def _wave(bits: str) -> Transitions:
    return Transitions.from_capture(Capture([int(b) for b in bits], 0, 0, 1e-8, 1))


@pytest.fixture
def recording(tmp_path):
    rec = Recorder(tmp_path / "r", HZ, meta={"name": "試し"})
    o = ORIGIN
    rec.add_many([
        ("clock", "point", o, 0, 0, {"host_s": 1.0, "round_trip_s": 0.001}),
        ("rtos", "run", o + 100, 0x20000100, 0, {"task": "idle", "method": "itm"}, o + 300),
        ("rtos", "switch", o + 300, 0x20000100, 0x20000200, {"from": "idle", "to": "main"}),
        ("rtos", "run", o + 300, 0x20000200, 0, {"task": "main", "method": "itm"}, o + 900),
        ("swo", "exception", o + 400, 15, 0, {"action": "enter", "name": "SysTick"}, None, 50.0),
        ("swo", "exception", o + 420, 16, 0, {"action": "enter", "name": "IRQ0"}, None, 50.0),
        ("swo", "exception", o + 450, 16, 0, {"action": "exit", "name": "IRQ0"}, None, 50.0),
        ("swo", "exception", o + 452, 15, 0, {"action": "return", "name": "SysTick"}, None, 50.0),
        ("swo", "exception", o + 500, 15, 0, {"action": "exit", "name": "SysTick"}, None, 50.0),
        ("swo", "exception", o + 600, 17, 0, {"action": "exit", "name": "IRQ1"}, None, 50.0),   # 入りが無い
        ("testpoint", "call", o + 310, 1, 0, {"function": "main_loop", "self_s": 1e-6, "complete": True}, o + 800),
        ("testpoint", "call", o + 320, 5, 1, {"function": "read_sensor", "self_s": 2e-7, "complete": True}, o + 340),
        ("testpoint", "hit", o + 330, 7, 42, {"kind": "var", "function": "read_sensor", "label": "raw", "line": 12,
                                              "value": 42}),
        ("testpoint", "hit", o + 335, 8, 0, {"kind": "branch", "function": "read_sensor", "label": "if", "line": 13}),
        ("sample", "word", o + 200, 0x20000010, 7),
        ("sample", "word", o + 700, 0x20000010, 9),
        ("aux", "mv", o + 250, 0, 3300),
        ("dap", "ap_read", o + 260, 0xE000EDF0, 0x01030003, {"ack": 1}),
        ("uart", "rx", o + 270, 0, 0x41),
        ("annotation", "mark", o + 280, 0, 0, {"text": "ここで化けた"}, None, 12.0),
        ("annotation", "uart.byte", o + 50, 0x41, 0, {"protocol": "uart", "text": "A", "value": 0x41,
                                                      "error": None, "fields": None}, o + 150),
        ("host", "call", o + 950, 0, 0, {"client": "mcp", "method": "target.halt", "seconds": 0.0000005}, None, 30.0),
        ("target", "state", o + 960, 0, 0, {"state": "halted"}),
    ])
    rec.add_waveform(_wave("0011100"), start_ticks=o + 10)
    rec.close()
    r = Recording.open(tmp_path / "r")
    yield r
    r.close()


def _load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data["traceEvents"]
    threads = {e["tid"]: e["args"]["name"] for e in events if e["ph"] == "M" and e["name"] == "thread_name"}
    return data, events, threads


def _on(events, threads, track, ph=None):
    return [e for e in events if e["ph"] != "M" and threads.get(e.get("tid")) == track and (ph is None or e["ph"] == ph)]


def test_the_trace_is_chrome_json_with_the_timebase_in_other_data(recording, tmp_path):
    out = write_trace(recording, tmp_path / "t.json")
    data, events, _ = _load(tmp_path / "t.json")
    assert data["otherData"]["timebase_hz"] == HZ and data["otherData"]["origin_ticks"] == ORIGIN
    assert data["displayTimeUnit"] == "ns"
    assert out["events"] == sum(1 for e in events if e["ph"] != "M")
    assert any(e["ph"] == "M" and e["name"] == "process_name" for e in events)


def test_tasks_are_slices_and_switches_are_points(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, events, threads = _load(tmp_path / "t.json")
    runs = _on(events, threads, "タスク", "X")
    assert [(e["name"], e["ts"], e["dur"]) for e in runs] == [("idle", 1.0, 2.0), ("main", 3.0, 6.0)]
    assert [e["name"] for e in _on(events, threads, "タスク", "i")] == ["idle → main"]


def test_interrupts_are_nested_slices_from_enter_to_exit(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, events, threads = _load(tmp_path / "t.json")
    slices = sorted((e["name"], e["ts"], e["dur"]) for e in _on(events, threads, "割り込み", "X"))
    assert slices == [("IRQ0", 4.2, pytest.approx(0.3)), ("SysTick", 4.0, pytest.approx(1.0))]
    lone = _on(events, threads, "割り込み", "i")
    assert [e["name"] for e in lone] == ["IRQ1 exit"]                  # 入りの無い出は点
    assert all(e["args"]["uncertainty_us"] == pytest.approx(0.5) for e in _on(events, threads, "割り込み"))


def test_testpoint_functions_are_slices_and_variables_are_counters(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, events, threads = _load(tmp_path / "t.json")
    calls = _on(events, threads, "関数（テストポイント）", "X")
    assert [(e["name"], e["ts"], e["dur"]) for e in calls] == [("main_loop", 3.1, 4.9), ("read_sensor", 3.2, 0.2)]
    counters = [e for e in events if e["ph"] == "C" and e["name"] == "read_sensor: raw"]
    assert [c["args"]["value"] for c in counters] == [42]
    assert [e["name"] for e in _on(events, threads, "テストポイント", "i")] == ["read_sensor: if"]


def test_sampled_words_and_voltages_are_counters(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json", names={0x20000010: "counter"})
    _, events, _ = _load(tmp_path / "t.json")
    assert [(e["ts"], e["args"]["value"]) for e in events if e["ph"] == "C" and e["name"] == "counter"] == \
        [(2.0, 7), (7.0, 9)]
    assert [e["args"]["value"] for e in events if e["ph"] == "C" and e["name"] == "電圧 ch0 (mV)"] == [3300]


def test_unnamed_words_are_named_by_address(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, events, _ = _load(tmp_path / "t.json")
    assert any(e["ph"] == "C" and e["name"] == "0x20000010" for e in events)


def test_dap_uart_and_marks_are_points(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, events, threads = _load(tmp_path / "t.json")
    dap = _on(events, threads, "DAP", "i")
    assert dap[0]["name"] == "ap_read 0xE000EDF0" and dap[0]["args"]["value"] == "0x01030003"
    assert _on(events, threads, "UART rx", "i")[0]["name"] == "0x41 'A'"
    mark = [e for e in events if e["ph"] == "i" and e["name"] == "ここで化けた"]
    assert mark and mark[0]["s"] == "g"


def test_decoded_annotations_and_host_calls_are_slices(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, events, threads = _load(tmp_path / "t.json")
    decoded = _on(events, threads, "解読 uart.byte", "X")
    assert [(e["name"], e["ts"], e["dur"]) for e in decoded] == [("A", 0.5, 1.0)]
    calls = _on(events, threads, "操作 mcp", "X")
    assert calls[0]["name"] == "target.halt" and calls[0]["ts"] + calls[0]["dur"] == pytest.approx(9.5)
    assert calls[0]["dur"] == pytest.approx(0.5)
    assert [e["name"] for e in _on(events, threads, "target", "i")] == ["state: halted"]


def test_clock_points_are_left_out(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, _, threads = _load(tmp_path / "t.json")
    assert "clock" not in threads.values()


def test_the_waveform_is_a_counter_per_channel_at_its_changes(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json")
    _, events, _ = _load(tmp_path / "t.json")
    assert [(e["ts"], e["args"]["value"]) for e in events if e["ph"] == "C" and e["name"] == "LA ch0"] == \
        [(0.1, 0), (0.12, 1), (0.15, 0)]
    write_trace(recording, tmp_path / "n.json", waveform=False)
    _, events, _ = _load(tmp_path / "n.json")
    assert not any(e["ph"] == "C" and e["name"] == "LA ch0" for e in events)


def test_the_range_and_streams_limit_what_is_written(recording, tmp_path):
    out = write_trace(recording, tmp_path / "t.json", t0=ORIGIN + 250, t1=ORIGIN + 290, streams=["aux", "uart"],
                      waveform=False)
    _, events, _ = _load(tmp_path / "t.json")
    names = sorted(e["name"] for e in events if e["ph"] != "M")
    assert names == ["0x41 'A'", "電圧 ch0 (mV)"] and out["events"] == 2


def test_a_slice_that_crosses_the_range_start_is_kept(recording, tmp_path):
    write_trace(recording, tmp_path / "t.json", t0=ORIGIN + 500, streams=["rtos"], waveform=False)
    _, events, threads = _load(tmp_path / "t.json")
    assert [e["name"] for e in _on(events, threads, "タスク", "X")] == ["main"]
