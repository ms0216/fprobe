"""Probe のターゲットが出す値とイベントの道具（fprobe.probe_user、U16）: ITM のポート（SWO のバイトのデバイスの時刻）と
RTT のチャンネル（ホストの時刻 → 時計の対応付け）を型で解釈し、名前付きイベント（freertos_demo.elf の文字列）を集計する。
記録中は stream "user" に書き、timing_measure がそのまま使える。機器は使わない。"""

import struct
from types import SimpleNamespace

import pytest
from probe_fakes import DictMemory, FakeDevice, FakeTarget, use_fake_ring
from rtos_kernel import shared

from fprobe import collector, probe_window
from fprobe.mcp_server import Probe
from fprobe.profile import dwt
from fprobe.usertrace import EVENT_PORT, KINDS, VALUE_PORT


class FakeSwo:
    def __init__(self):
        self.started: list[int] = []

    def swo_stop(self):
        pass

    def swo_start(self, baud):
        self.started.append(baud)

    def swo_read(self):
        return b""


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    use_fake_ring(monkeypatch, probe_window, collector)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._symbols = shared()[1]                                       # freertos_demo.elf（fprobe_user.h の名前の文字列）
    p._debug = SimpleNamespace(target=FakeTarget(DictMemory({dwt.ITM_TER: 1})), probe=FakeSwo(), close=lambda: None)
    yield p
    if p._recorder is not None:
        p.recording_stop()


def name_address(text: str) -> int:
    """freertos_demo.elf の中の名前の文字列の番地（fprobe_user.h の FPROBE_USER_NAME。4 バイトに揃う）。"""
    for seg in shared()[1].segments:
        i = seg.data.find(text.encode() + b"\x00")
        while i >= 0:
            if (seg.address + i) % 4 == 0:
                return seg.address + i
            i = seg.data.find(text.encode() + b"\x00", i + 1)
    raise KeyError(text)


def stimulus(port: int, word: int, size: int = 4) -> bytes:
    return bytes([port << 3 | {1: 1, 2: 2, 4: 3}[size]]) + word.to_bytes(size, "little")


def f32(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def test_itm_values_and_named_events_with_device_times_go_to_the_recording(probe):
    dev, target = probe.session.device, probe.target
    probe.recording_start(name="user")
    started = probe.user_trace_start(channels=[{"name": "fpu_value", "source": "itm", "index": 1, "type": "f32"}],
                                     swo_baud=2_000_000, core_clock_hz=16e6)
    assert probe._debug.probe.started == [2_000_000]
    ter = [v for a, v in target.write_log if a == dwt.ITM_TER][-1]
    assert ter == 1 | 1 << 1 | 1 << EVENT_PORT | 1 << VALUE_PORT                # 元のポート 0 は残す
    assert started["itm_ports"] == [1, EVENT_PORT, VALUE_PORT]
    t0, step, count = started["t0"], name_address("fpu_step"), name_address("i:sensor_count")
    for k in range(3):
        base = t0 + 10_000 * (k + 1)
        dev.swo(base, stimulus(EVENT_PORT, step | KINDS.index("start")))
        dev.swo(base + 1000 + 500 * k, stimulus(EVENT_PORT, step | KINDS.index("stop")))
        dev.swo(base + 3000, stimulus(1, f32(1.5 * k)))
    dev.swo(t0 + 50_000, stimulus(EVENT_PORT, count | 3) + stimulus(VALUE_PORT, 42))
    dev.advance(100_000)
    got = probe.user_trace_get(kinds=["value"])
    assert [(r["name"], r["value"]) for r in got["records"]] == [("fpu_value", 0.0), ("fpu_value", 1.5),
                                                                  ("fpu_value", 3.0), ("sensor_count", 42)]
    stats = probe.user_trace_stats()
    assert stats["values"]["fpu_value"]["max"] == 3.0 and stats["values"]["sensor_count"]["last"] == 42
    step_stats = stats["intervals"]["fpu_step"]
    assert step_stats["count"] == 3 and step_stats["min"] == 1000 and step_stats["max"] == 2000
    stopped = probe.user_trace_stop()
    assert stopped["records"] == 10 and stopped["recorded"]
    assert [v for a, v in target.write_log if a == dwt.ITM_TER][-1] == 1      # 足したポートを戻した
    measured = probe.timing_measure({"stream": "user", "kind": "start", "body": {"name": "fpu_step"}},
                                    {"stream": "user", "kind": "stop", "body": {"name": "fpu_step"}})
    assert measured["count"] == 3 and measured["max"] == 2000
    rows = probe.recording_events(streams=["user"], kinds=["interval"])["events"]
    assert [(e["t"] - t0, e["t_end"] - e["t"]) for e in rows] == [(10_000, 1000), (20_000, 1500), (30_000, 2000)]


def test_rtt_channels_are_decoded_with_host_times_mapped_to_device_ticks(probe):
    probe._rtt = object()                                          # rtt_start 済み（受信は _received で渡す）
    started = probe.user_trace_start(channels=[{"name": "adc", "source": "rtt", "index": 1, "type": "u16"}],
                                     events=False, event_rtt_channel=2)
    assert started["itm_ports"] == [] and started["rtt_channels"] == [1, 2]
    probe._received(1, b"\x10\x00\x20\x00", 0.0)
    probe._received(2, struct.pack("<I", name_address("done") | KINDS.index("mark")), 0.0)
    probe._received(0, b"text", 0.0)                              # 使わないチャンネル
    got = probe.user_trace_get()
    assert [(r["kind"], r["name"], r.get("value")) for r in got["records"]] == [
        ("value", "adc", 16), ("value", "adc", 32), ("mark", "done", None)]
    assert all("uncertainty" in r for r in got["records"])
    probe.user_trace_stop()
    assert probe._rtt_listeners == []


def test_user_trace_errors(probe):
    with pytest.raises(ValueError, match="何も"):
        probe.user_trace_start(events=False)
    with pytest.raises(RuntimeError, match="rtt_start"):
        probe.user_trace_start(channels=[{"source": "rtt", "index": 1, "type": "i32"}], events=False)
    with pytest.raises(ValueError, match="30"):
        probe.user_trace_start(channels=[{"source": "itm", "index": 30, "type": "i32"}])
    with pytest.raises(ValueError, match="24"):
        probe.user_trace_start(channels=[{"source": "itm", "index": 24, "type": "i32"}])
    with pytest.raises(RuntimeError, match="user_trace_start"):
        probe.user_trace_get()
    probe.user_trace_start()
    with pytest.raises(RuntimeError, match="user_trace_stop"):
        probe.user_trace_start()
    probe.user_trace_stop()
    assert probe.user_trace_get()["running"] is False
