"""linux.* の、ほかの試験が呼ばないメソッドを偽の SSH・エージェント・xsdb・コアで呼ぶ（応答の形を仕様の result で照合する:
tests/api_result_check.py）。中身の働きは test_probe_linux.py 等が確かめる。ここは応答の形を照合に通すため。"""
import json
from pathlib import Path

from fprobe import probe_linux_ring as probe_linux
from fprobe.jtag_chain import ChainDevice, ChainScan
from test_api_linux_halt import rig  # noqa: F401  (fixture)
from test_probe_linux import FakeRingCpu, _connect, _ring_bytes, _ring_probe, _wait, probe  # noqa: F401  (fixture)

XSDB = str(Path(__file__).with_name("linux_fake_xsdb.py"))


def test_agent_deploy_builds_with_the_target_gcc(probe, monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"command -v gcc": "/usr/bin/gcc\ngcc (GCC) 12.2.0\n"}))
    probe.linux_connect(target="pynq", host="h", user="u")
    out = probe.linux_agent_deploy(target="pynq")
    assert out["compiler"] == "gcc (GCC) 12.2.0" and len(out["source_sha256"]) == 64
    assert probe.linux_agent_status(target="pynq")["deployed"] == out


def test_agent_status_before_start_has_the_same_fields_as_after(probe):
    probe.linux_connect(target="pynq", host="h", user="u")
    before = probe.linux_agent_status(target="pynq")
    probe.linux_agent_start(target="pynq")
    assert set(before) == set(probe.linux_agent_status(target="pynq"))


def test_trace_start_and_stop_with_an_ftrace_event(probe, monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"enable": "", "cat /sys/kernel/tracing/trace_clock":
                                                              "local global [mono_raw] boot\n"}))
    _connect(probe)
    probe._linux["pynq"].caps["tracefs"] = "/sys/kernel/tracing"
    probe.linux_ftrace_enable(target="pynq", events=["sched/sched_switch"])
    started = probe.linux_trace_start(target="pynq", sync_interval_s=3600)
    assert started == {"trace_clock": "mono_raw", "trace_pipe": True}
    stopped = probe.linux_trace_stop(target="pynq")
    assert stopped["stopped"] is True and stopped["pairs"] >= 1


def test_ring_watch_then_unwatch(probe):
    _ring_probe(probe, FakeRingCpu(0xA0000000, _ring_bytes([0, 1], head=2)))
    assert probe.linux_ring_watch(target="pynq", interval_s=0.01, address=0xA0000000, access="halt",
                                  max_records=10) == {"watching": True}
    assert _wait(lambda: probe._linux["pynq"].ring_stats["reads"] > 0)
    out = probe.linux_ring_unwatch(target="pynq")
    assert out["watching"] is False and out["ring_stats"]["reads"] > 0


def test_memap_connect_with_xsdb_and_disconnect(probe, monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_XSDB_MEM", "{}")
    probe.linux_connect(target="pynq", host="h", user="u")
    info = probe.linux_memap_connect(target="pynq", xsdb=XSDB, cable_serial="1234-tulA", chain_idcode=0x23727093,
                                     timeout_s=10)
    assert info["tool"] == "xsdb" and info["cable_serial"] == "1234-tulA"
    assert probe.linux_memap_disconnect(target="pynq") == {"memap": None}


class _Dap:
    """linux.memap.connect_jtag が使う生の DAP の偽物（AP 0 が AHB の MEM-AP）。"""

    def start(self, *a, **k):
        pass

    def read_dpidr(self):
        return 0x4BA00477

    def power_up(self):
        return {"debug_power": True}

    def clear_errors(self):
        pass

    def read_ap(self, ap, address):
        return 0x24770011                                       # ARM の AHB-AP（class 8 = MEM-AP）


def test_memap_connect_jtag_returns_the_facts_it_checked(probe, monkeypatch):
    probe.linux_connect(target="pynq", host="h", user="u")
    scan = ChainScan(devices=[ChainDevice(0, 0x4BA00477, 4, "table"), ChainDevice(1, 0x23727093, 6, "table")],
                     total_ir_length=10, ir_capture=None)
    monkeypatch.setattr(probe_linux, "scan_chain", lambda wire: scan)
    monkeypatch.setattr(probe, "_require_disconnected", lambda what: None)
    monkeypatch.setattr(probe, "_ensure_design", lambda protocol: None)
    monkeypatch.setattr(probe, "_jtag_wire", lambda frequency_hz: None)
    monkeypatch.setattr(probe, "_raw", lambda: _Dap())
    info = probe.linux_memap_connect_jtag(target="pynq", chain_idcode=0x4BA00477, ap=0, frequency_hz=1_000_000)
    assert info["via"] == "probe_jtag" and info["device_index"] == 0 and info["ap_idr"] == 0x24770011
    probe.linux_memap_disconnect(target="pynq")


def test_halt_recover_and_rescue(rig):  # noqa: F811
    world, probe, ai, _, opts, _ = rig
    ai.call("linux.halt.open", **opts)
    out = ai.call("linux.halt.recover")
    assert (out["restored"], out["stale"]) == (0, 0)
    world.cores.rescue = lambda: tuple(range(world.cores.n_cores))      # fprobe のバックエンドだけが持つ口
    out = ai.call("linux.halt.rescue")
    assert out["resumed"] == [0, 1, 2, 3] and out["halting_debug"] is False
    ai.call("linux.halt.close")

