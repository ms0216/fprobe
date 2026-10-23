"""fake_demo の Event Log・Recordings・Logic Analyzer のデモの値（fake_demo_search.SearchDemo）。"""

import time

import pytest

import fake_demo
from fake_demo import DemoProbe


@pytest.fixture
def demo():
    probe = DemoProbe()
    fake_demo.start(probe)
    yield probe
    if probe._elf._recorder is not None:
        probe.recording_stop()


def test_the_capture_has_a_uart_line_that_the_real_decoder_reads(demo):
    out = demo.la_decode("uart", {"channel": 2, "baud": 1_000_000})          # ボーレートは渡す（推定しない）
    text = bytes(a["value"] for a in out["annotations"] if a["value"] is not None)
    assert text.startswith(b"fprobe: hello\r\n")
    assert out["errors"] == 1                                     # 枠の誤りが 1 か所
    assert {d["name"] for d in demo.la_decoders()["decoders"]} >= {"uart", "spi", "i2c"}


def test_the_capture_is_measured_by_the_real_measure(demo):
    m = demo.la_measure()
    assert m["channels"][0]["frequency_hz"] == pytest.approx(1.04e6, rel=0.01)    # ch0 = 1 MHz の時計
    assert 2 in m["channels"]                                   # UART の線も変化がある


def test_the_demo_recording_is_searched_measured_and_marked_by_the_real_probe(demo):
    marks = demo.recording_find({"stream": "annotation", "kind": "mark"})
    assert [e["body"]["text"] for e in marks["events"]][0] == "boot complete"
    stats = demo.timing_measure({"stream": "rtos", "kind": "switch"}, {"stream": "rtos", "kind": "switch"})
    assert stats["count"] > 100 and stats["histogram"]
    assert demo.recording_mark(text="ここを見る", t=1_000_000) == {"t": 1_000_000}
    found = demo.recording_find({"stream": "annotation", "body": {"text": "ここを見る"}})["events"]
    assert [e["t"] for e in found] == [1_000_000]
    assert demo.recording_waveform_measure(0, 2_000_000)["channels"]
    assert demo.recording_segments()["segmented"] is False


def test_recordings_are_listed_started_stopped_and_opened_inside_the_demo_folder(demo, tmp_path):
    assert [r["name"] for r in demo.recording_list()["recordings"]] == ["demo"]   # 利用者の本物の記録は見せない
    started = demo.recording_start(name="試し")
    demo.record_host_event("host", "call", {"client": "ui", "role": "human", "method": "target.halt", "seq": 7})
    demo.la_capture()
    demo.recording_mark(text="いま")
    time.sleep(0.25)
    status = demo.recording_status()
    assert status["recording"] and status["events"] >= 3
    calls = demo.recording_query("SELECT json_extract(body, '$.seq') FROM events WHERE stream = 'host'")
    assert calls["rows"] == [[7]]                                 # 画面の Activity の行 → 記録の時刻
    stopped = demo.recording_stop()
    assert stopped["recording"] == started["recording"] and not demo.recording_status()["recording"]
    names = [r["name"] for r in demo.recording_list()["recordings"]]
    assert started["recording"] in names and "demo" in names
    opened = demo.recording_open("demo")
    assert opened["span"] == [0, 20_000_000] or tuple(opened["span"]) == (0, 20_000_000)
    out = tmp_path / "demo.csv"
    demo.recording_export("csv", str(out))
    assert "boot complete" in out.read_text(encoding="utf-8")


def test_cross_trigger_settings_are_remembered_and_the_sequence_advances(demo, monkeypatch):
    assert demo.xtrig_route(to_la=["sequence"], to_inject=[]) == {"to_la": ["sequence"], "to_inject": []}
    demo.xtrig_set_match(0x20000010)
    assert demo.xtrig_status()["match"]["count"] > 0
    out = demo.xtrig_set_sequence([{"when": ["pattern_a"]}, {"when": ["pattern_b"], "within_us": 10}],
                                  pattern_a={"rise": 8}, pattern_b={"fall": 32})
    assert out == {"stages": 2, "armed": True, "stage": 0, "fired": False}
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 10)
    status = demo.xtrig_sequence_status()
    assert status["fired"] and status["stage"] == 1 and status["time"] > 0
    with pytest.raises(KeyError):
        demo.xtrig_set_sequence([{"when": ["nope"]}])
