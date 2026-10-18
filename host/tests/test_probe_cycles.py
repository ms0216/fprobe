"""Probe のサイクルの計測の道具（fprobe.probe_cycles）: cycles_read・cycles_measure（2 つのブレークポイントの間）・
cycles_clock（CPU クロックの実測）。機器は使わない（sram_demo.elf、偽のターゲット・デバイス・イベント記録のリング）。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_fakes import DictMemory, FakeDevice, FakeTarget, use_fake_ring

from fprobe import collector, probe_cycles, probe_window
from fprobe import swo_trace as st
from fprobe.mcp_server import Probe
from fprobe.profile import dwt
from fprobe.symbols import Symbols

SRAM_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
SYNC = bytes(5) + bytes([0x80])
CNT = {name: address for name, (_, address, _) in st.COUNTERS.items()}


class BreakTarget(FakeTarget):
    def __init__(self, memory):
        super().__init__(memory, halted=False)
        self.breakpoints: set[int] = set()

    def set_breakpoint(self, address, kind):
        self.breakpoints.add(address)
        return True

    def remove_breakpoint(self, address):
        self.breakpoints.discard(address)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    use_fake_ring(monkeypatch, probe_window, collector)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._symbols = Symbols(SRAM_DEMO)
    target = BreakTarget(DictMemory({dwt.DWT_CTRL: 0x4000_0000, st.CPUID: 0x410FC241}))
    p._debug = SimpleNamespace(target=target, probe=SimpleNamespace(), close=lambda: None)
    yield p
    p._stop_observing()


def set_counts(target, cyccnt, **counters):
    target.memory.write_memory(st.DWT_CYCCNT, cyccnt.to_bytes(4, "little"))
    for name, value in counters.items():
        target.memory.write_memory(CNT[name], value.to_bytes(4, "little"))


def script(probe, monkeypatch, stops):
    """_run_until_halt の代わり: 止まるたびにカウンタを書き、(pc, reason) を返す。"""
    stops = list(stops)

    def run(timeout_s):
        if not stops:
            return {"reason": "timeout", "pc": 0}
        pc, counts = stops.pop(0)
        swo = counts.pop("swo", None)
        if swo is not None:                                            # 走っている間に SWO に出たパケット
            dev = probe.session.device
            dev.swo(dev.ticks + 10, swo)
            dev.advance(1000)
        set_counts(probe.target, **counts)
        return {"reason": "breakpoint", "pc": pc}
    monkeypatch.setattr(probe, "_run_until_halt", run)


def test_cycles_between_two_breakpoints(probe, monkeypatch):
    start = probe.dbg.breakpoint_address("tick")
    end = probe.dbg.breakpoint_address("reached_milestone")
    probe.breakpoint_set("default_handler")                                    # 前からあるものは残す
    other = probe.dbg.breakpoint_address("default_handler")
    script(probe, monkeypatch, [
        (other, dict(cyccnt=10)),                                      # 始めの前に別の場所で止まった: 飛ばす
        (start, dict(cyccnt=1000, cpi=250, lsu=10, exc=0, sleep=0, fold=0)),
        (end, dict(cyccnt=1500, cpi=4, lsu=30, exc=0, sleep=0, fold=2)),
        (start, dict(cyccnt=0xFFFF_FF00, cpi=0, lsu=0, exc=0, sleep=0, fold=0)),
        (end, dict(cyccnt=0x100, cpi=1, lsu=2, exc=0, sleep=0, fold=0)),
    ])
    out = probe.cycles_measure("tick", "reached_milestone", count=2, core_clock_hz=16e6)
    assert out["intervals"] == [500, 512]
    assert out["summary"]["min"] == 500 and out["summary"]["time_us"]["max"] == pytest.approx(32)
    first = out["counters"][0]
    assert first["cpi"] == 10 and first["lsu"] == 20 and first["fold"] == 2
    assert first["instructions"] == 500 - 10 - 20 + 2
    assert out["counters_exact"] is False and any("256" in n for n in out["notes"])
    assert probe.target.breakpoints == {other}                         # 置いた 2 つは外した
    assert probe.target.memory.u32(dwt.DWT_CTRL) & 1                   # CYCCNT を有効にした
    assert out["start"]["function"] == "tick" and out["end"]["function"] == "reached_milestone"


def test_counters_are_exact_with_swo_overflows(probe, monkeypatch):
    probe._debug.probe = SimpleNamespace(swo_start=lambda baud: None, swo_stop=lambda: None, swo_read=lambda: b"")
    probe.swo_configure(counters=["cpi", "exc", "sleep", "lsu", "fold"], swo_baud=2_000_000, trace_pin=False)
    start = probe.dbg.breakpoint_address("tick")
    end = probe.dbg.breakpoint_address("reached_milestone")
    zero = dict(cpi=0, exc=0, sleep=0, lsu=0, fold=0)
    script(probe, monkeypatch, [
        (start, dict(cyccnt=0, swo=SYNC, **zero)),
        (end, dict(cyccnt=5000, swo=bytes([0x05, 0x01, 0x05, 0x01, 0x05, 0x08]), **{**zero, "cpi": 7, "lsu": 1})),
    ])
    out = probe.cycles_measure("tick", "reached_milestone")
    assert out["counters_exact"] is True
    assert out["counters"][0]["cpi"] == 2 * 256 + 7 and out["counters"][0]["lsu"] == 256 + 1
    probe.swo_unconfigure()


def test_cycles_measure_timeout(probe, monkeypatch):
    start = probe.dbg.breakpoint_address("tick")
    script(probe, monkeypatch, [(start, dict(cyccnt=5))])
    with pytest.raises(RuntimeError, match="終わりの場所"):
        probe.cycles_measure("tick", "reached_milestone", timeout_s=0.1)
    assert probe.target.breakpoints == set()
    end = probe.dbg.breakpoint_address("reached_milestone")
    script(probe, monkeypatch, [(start, dict(cyccnt=5)), (end, dict(cyccnt=25)), (start, dict(cyccnt=30))])
    out = probe.cycles_measure("tick", "reached_milestone", count=3, counters=False, timeout_s=0.1)
    assert out["intervals"] == [20] and "counters" not in out and any("timeout" in n for n in out["notes"])


def test_cycles_read(probe):
    set_counts(probe.target, 123456, cpi=1, exc=2, sleep=3, lsu=4, fold=5)
    out = probe.cycles_read()
    assert out["cyccnt"] == 123456 and out["counters"] == {"cpi": 1, "exc": 2, "sleep": 3, "lsu": 4, "fold": 5}
    assert out["cyccnt_enabled"] is False and out["time"] == probe.session.device.ticks


def test_cycles_clock_with_the_sampler(probe, monkeypatch):
    dev = probe.session.device
    clock = 84_000_000

    def sleep(seconds):                                                # 走っている間にサンプラが CYCCNT を読む
        t0 = dev.ticks
        for k in range(1, 101):
            t = t0 + k * 1_000_000
            dev.sample(t, st.DWT_CYCCNT, (t * clock // 100_000_000) & 0xFFFFFFFF)
    monkeypatch.setattr(probe_cycles.time, "sleep", sleep)
    out = probe.cycles_clock(duration_s=1.0, rate_hz=100)
    assert out["method"] == "sampler" and out["clock_hz"] == pytest.approx(clock, rel=1e-6)
    assert out["samples"] == 100 and probe._sampler_user is None
    assert not dev.sampler_running()


def test_cycles_clock_falls_back_to_two_reads(probe, monkeypatch):
    dev = probe.session.device
    probe._sampler_user = "watch"

    def sleep(seconds):
        dev.advance(100_000_000)
        set_counts(probe.target, 16_000_000 + 1000)
    set_counts(probe.target, 1000)
    monkeypatch.setattr(probe_cycles.time, "sleep", sleep)
    out = probe.cycles_clock(duration_s=1.0)
    assert out["method"] == "two_reads" and out["clock_hz"] == pytest.approx(16e6)
    assert any("watch" in n for n in out["warnings"])


def test_cycles_clock_needs_a_running_core(probe):
    from pyocd.core.target import Target
    probe.target.state = Target.State.HALTED
    with pytest.raises(RuntimeError, match="走って"):
        probe.cycles_clock()
