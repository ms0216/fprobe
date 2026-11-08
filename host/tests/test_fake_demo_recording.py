"""fake_demo のピンの垂れ流しの記録（fake_demo_recording.RecordingDemo）: recording.status の la_stream が、AI（API）が始めた流れでも
人の画面が読むのと同じ欄（形式の事実）で出る。断りの文は本物（DeepAnalyzer._check_rle_format）と同じ。"""

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


def test_a_token_stream_appears_in_recording_status_with_the_format_facts(demo):
    assert "la_stream" not in demo.recording_status()
    demo.recording_start(name="t")
    started = demo.recording_la_stream_start(rle=True, rle_format="tokens", tick_us=1000)
    assert started["rle_format"] == "tokens"
    la = demo.recording_status()["la_stream"]
    assert la["running"] and la["rle_format"] == "tokens"
    assert {"wire_bytes", "pad_bytes", "changes", "bytes_per_change"} <= la.keys()
    stopped = demo.recording_la_stream_stop()
    assert not stopped["running"]
    assert "la_stream" not in demo.recording_status()


def test_records_report_their_format_and_do_not_count_changes_like_the_real_raw_beat_path(demo):
    demo.recording_start(name="r")
    demo.recording_la_stream_start(rle=True, rle_format="records")
    la = demo.recording_status()["la_stream"]
    assert la["rle_format"] == "records" and la["changes"] is None and la["bytes_per_change"] is None and la["pad_bytes"] == 0


def test_stopping_the_recording_stops_the_pin_stream(demo):
    demo.recording_start(name="s")
    demo.recording_la_stream_start(rle=True)
    demo.recording_stop()
    assert "la_stream" not in demo.recording_status()


def test_refusals_use_the_real_sentences(demo):
    demo.recording_start(name="x")
    with pytest.raises(ValueError, match="rle = True のときだけ"):
        demo.recording_la_stream_start(rle=False, rle_format="tokens", tick_us=1000, rate_hz=25e6)
    with pytest.raises(ValueError, match="詰める"):
        demo.recording_la_stream_start(rle=True, rle_format="tokens", tick_us=1000, pack_channels=8)
    demo.tokens_capable = False
    assert demo.probe_info()["capabilities"]["la_rle_tokens"] is False
    with pytest.raises(RuntimeError, match="la_rle_tokens"):
        demo.recording_la_stream_start(rle=True, rle_format="tokens", tick_us=1000)
    demo.recording_la_stream_start(rle=True, rle_format="records")           # records は出せる
