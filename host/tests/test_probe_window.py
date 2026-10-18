"""Probe の共通の部品（fprobe.probe_window）: DAP サンプラの持ち主と、時刻付きの出来事の窓（イベント記録・記録）。
機器は使わない（偽のデバイスとリング）。"""

from types import SimpleNamespace

import pytest
from probe_fakes import FakeDevice, use_fake_ring

from fprobe import collector, probe_window
from fprobe.mcp_server import Probe

ADDRESS = 0x2000_0010


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    use_fake_ring(monkeypatch, probe_window, collector)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    if p._recorder is not None:
        p.recording_stop()


def test_the_sampler_has_one_holder_at_a_time(probe):
    probe._claim_sampler("rtos")
    probe._claim_sampler("rtos")                                # 同じ持ち主は何度でも
    with pytest.raises(RuntimeError, match="rtos_timeline_stop"):
        probe._claim_sampler("profile")
    probe._release_sampler("profile")                           # 持ち主でなければ何もしない
    assert probe._sampler_user == "rtos"
    probe._release_sampler("rtos")
    probe._claim_sampler("profile")


def test_a_window_reads_the_event_log_from_its_start_to_its_close(probe):
    dev = probe.session.device
    with probe._lock:
        window = probe._open_window()
    assert not window.recorded and dev.ring_running
    t0 = window.t0
    dev.sample(t0 + 10, ADDRESS, 1)
    dev.sample(t0 + 20, ADDRESS, 0xDEAD, ack=0b100)             # 読めなかった語（FAULT）は使わない
    dev.swo(t0 + 30, b"\x01\x02")
    dev.sample(t0 + 40, ADDRESS, 2)
    assert probe._window_events(window, "sample") == [(t0 + 10, ADDRESS, 1), (t0 + 40, ADDRESS, 2)]
    assert [b for _, _, b in probe._window_events(window, "swo")] == [1, 2]
    with probe._lock:
        probe._close_window(window)
    dev.sample(dev.ticks + 10, ADDRESS, 3)                      # 閉じた後の出来事は入らない
    assert [v for _, _, v in probe._window_events(window, "sample")] == [1, 2]
    info = probe._window_info(window, 1e8)
    assert info["source"] == "event_log" and info["lost_events"] == 0 and info["duration_s"] > 0


def test_a_window_does_not_restart_a_running_event_log(probe):
    dev = probe.session.device
    dev.ring_running = True
    dev.sample(dev.ticks, ADDRESS, 7)                           # ほかの道具が読んでいる途中のリング
    with probe._lock:
        window = probe._open_window()
    assert len(dev.ring) == 1 and window.seen == 1


def test_events_that_fell_off_the_ring_are_counted(probe):
    dev = probe.session.device
    dev.ring_capacity = 4
    with probe._lock:
        window = probe._open_window()
    for k in range(10):
        dev.sample(window.t0 + 10 * (k + 1), ADDRESS, k)
    got = probe._window_events(window, "sample")
    assert [v for _, _, v in got] == [6, 7, 8, 9] and window.lost == 6
    assert any("読めなかった" in n for n in probe._window_info(window, 1e8)["notes"])


def test_a_window_opened_while_recording_reads_the_recording(probe):
    dev = probe.session.device
    probe.recording_start(name="window")
    with probe._lock:
        window = probe._open_window()
    assert window.recorded
    dev.sample(window.t0 + 10, ADDRESS, 5)
    dev.sample(window.t0 + 20, ADDRESS, 6, ack=0b010)           # WAIT: 記録には ACK 付きで残るが、窓では使わない
    dev.swo(window.t0 + 30, b"\xAA")
    assert probe._window_events(window, "sample") == [(window.t0 + 10, ADDRESS, 5)]
    assert probe._window_events(window, "swo") == [(window.t0 + 30, 0, 0xAA)]
    wait = probe.recording_find({"stream": "sample", "b": 6})["events"]
    assert wait[0]["body"] == {"ack": 0b010}
    assert probe._window_info(window, 1e8)["source"] == "recording"
