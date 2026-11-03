"""応答の形の照合（tests/api_result_check.py）のために、ほかの試験が呼ばない道筋の記録・イベント・UART の道具を呼ぶ。

応答の中身の正しさは各道具の試験（test_correlate・test_events_read・test_uart_flow 等）が確かめる。ここは
「その道筋の応答が仕様（host/api/fprobe_api.json の result）の形どおりか」を照合させるだけ。
"""

from types import SimpleNamespace

from fprobe import generated as g
from fprobe import probe_events
from fprobe.mcp_server import Probe
from caps_fakes import FakeDeviceBase, caps_for
from probe_fakes import FakeDevice, use_fake_ring
from test_correlate import make


def _probe_with_recording(tmp_path, monkeypatch) -> Probe:
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    make(tmp_path / "recordings").close()                     # tmp/recordings/r
    p = Probe("unused")
    p.recording_open("r")
    return p


def test_recording_reads_of_an_open_recording(tmp_path, monkeypatch):
    p = _probe_with_recording(tmp_path, monkeypatch)
    p.recording_durations("state")
    p.recording_transactions("mark rose", by="restart", counts=["restart changed and state=BUSY"],
                             effect="errors changed")
    p.recording_transactions("mark rose", by="restart", by_at="end")           # effect なし: by_effect は null
    p.recording_intervals({"stream": "fpga"}, {"stream": "sample"}, limit=3)
    p.recording_intervals({"stream": "nothing"}, {"stream": "sample"})        # 組が無い: 統計の欄なし
    span = p.recording_open("r")["span"]
    p.recording_density(span[0], span[1] + 1, width=10)
    p.recording_waveform(span[0], span[1] + 1, width=10)
    p.recording_overview(width=10)
    p.recording_segments()
    p.recording_export("csv", str(tmp_path / "out.csv"))
    p.recording_export("perfetto", str(tmp_path / "out.json"))
    p.recording_mark("ここ", t=span[0])
    p.recording_status()
    p.recording_trigger_status()                                              # 見張っていない: state off だけ


def test_events_start_and_stop(monkeypatch):
    use_fake_ring(monkeypatch, probe_events)
    dev = FakeDevice()
    dev.caps = caps_for(timebase_hz=100_000_000)
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    p.events_start()
    dev.sample(dev.ticks + 10, 0x20000000, 1)
    p.events_read(raw=True)
    p.events_stop()


class _FlowDevice(FakeDeviceBase):
    """UART のフロー制御の CSR だけの偽の機器（ケイパビリティは本物の型）。"""

    def __init__(self):
        self.regs = {g.UART_FLOW: 0, g.UART_LINES: 0}
        self.caps = caps_for(uart_flow=(4, 1))

    def read32(self, address):
        return self.regs[address]

    def write32(self, address, value):
        self.regs[address] = value


def test_swo_stop():
    p = Probe("unused")
    p._debug = SimpleNamespace(probe=SimpleNamespace(swo_stop=lambda: None))
    assert p.swo_stop() is None


def test_uart_flow_and_lines():
    p = Probe("unused")
    p._session = SimpleNamespace(device=_FlowDevice())
    p.uart_flow(auto=True, rts_channel=4, cts_channel=1)
    p.uart_lines()
