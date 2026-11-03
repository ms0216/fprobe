"""FPGA の観測のレジスタ・組・記録の開始と停止の道具の応答（tests/api_result_check.py が仕様の result で照合する）。

ほかの試験は観測の回路の読み書きの道具を Probe から呼ばないので、ここで呼ぶ。観測の回路（fprobe.fpga_obs.FpgaObs）は
偽物に置き換える（Probe の道具が返す形だけを見る。回路との受け答えは test_fpga_obs*.py）。
"""

from types import SimpleNamespace

import pytest
from probe_fakes import FakeDevice

from fprobe.mcp_server import Probe


class StubObs:
    mode = "stream"

    def __init__(self, dev):
        self.dev = dev
        self.calls = []

    def register_read(self, addr):
        self.calls.append(("read", addr))
        return 0x1234

    def register_write(self, addr, value):
        self.calls.append(("write", addr, value))

    def select(self, group):
        self.calls.append(("select", group))

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    monkeypatch.setattr("fprobe.probe_fpga_obs.FpgaObs", StubObs)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    return p


def test_register_select_start_stop_return_their_documented_shapes(probe):
    assert probe.fpga_obs_register_read(addr=2) == {"value": 0x1234}
    assert probe.fpga_obs_register_write(addr=15, value=1) == {}
    assert probe.fpga_obs_select(group=1) == {}
    assert probe.fpga_obs_start() == {}
    assert probe.fpga_obs_stop() == {}
    assert probe._fpga_obs.calls == [("read", 2), ("write", 15, 1), ("select", 1), ("start",), ("stop",)]
