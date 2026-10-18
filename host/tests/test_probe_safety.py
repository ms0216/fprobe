"""Probe の安全の道具（fprobe.probe_safety）: 止めると壊れうるかの見分け（ELF だけ）と、走らせたままの
致命的エラーの見張り（target_status の fatal）。機器は使わない。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_fakes import DictMemory, FakeTarget
from pyocd.core.target import Target

from fprobe import probe_safety
from fprobe.mcp_server import Probe
from fprobe.profile.dwt import DEMCR, DWT_PCSR
from fprobe.symbols import Symbols

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


def test_halt_risk_needs_an_elf():
    with pytest.raises(RuntimeError, match="ELF"):
        Probe("unused").halt_risk()


def test_halt_risk_of_the_demo_elf():
    p = Probe("unused")
    p._symbols = Symbols(DEMO)
    r = p.halt_risk()
    assert r["risky"] is False and r["reasons"] == [] and r["elf"].endswith("sram_demo.elf")


def test_halt_risk_reports_radio_and_rtos():
    p = Probe("unused")
    p._symbols = SimpleNamespace(path=Path("app.elf"), names=lambda: [
        SimpleNamespace(name="RADIO_IRQHandler", kind="function"),
        SimpleNamespace(name="_kernel", kind="object")])
    r = p.halt_risk()
    assert r["risky"] and [x["kind"] for x in r["reasons"]] == ["radio", "rtos"]
    assert r["reasons"][1]["rtos"] == "Zephyr"


# --- 致命的エラーの見張り（target_status の fatal） ------------------------------------------------------

@pytest.fixture
def running(monkeypatch):
    """デモの ELF を読み、走っている偽のコアにつないだ Probe と、DWT_PCSR に関数の番地を置いて時刻を進める関数。"""
    clock = [1000.0]
    monkeypatch.setattr(probe_safety.time, "time", lambda: clock[0])
    p = Probe("unused")
    p._symbols = Symbols(DEMO)
    memory = DictMemory()
    target = FakeTarget(memory, halted=False)
    p._debug = SimpleNamespace(target=target, probe=None, close=lambda: None)

    def at(function, seconds):
        memory.write_memory(DWT_PCSR, (p._symbols.symbol(function).address + 2).to_bytes(4, "little"))
        clock[0] += seconds
        return p.target_status()
    return p, memory, target, at


def test_running_normally_has_no_fatal(running):
    p, memory, target, at = running
    assert "fatal" not in at("tick", 0) and "fatal" not in at("reached_milestone", 1) and "fatal" not in at("tick", 1)
    assert memory.u32(DEMCR) >> 24 & 1                                    # PCSR のために TRCENA を立てた


def test_staying_in_the_default_handler_is_reported_as_a_fault_loop(running):
    p, memory, target, at = running
    assert "fatal" not in at("default_handler", 0)
    status = at("default_handler", 2)
    assert status["state"] == "RUNNING"
    assert status["fatal"]["kind"] == "fault_loop" and status["fatal"]["function"] == "default_handler"
    assert status["fatal"]["since"] == 1000.0
    target.state = Target.State.HALTED                                   # 止めたら見張りは忘れる
    target.registers["pc"] = p._symbols.symbol("tick").address
    assert "fatal" not in p.target_status()


def test_lockup_is_reported_at_once(running):
    p, memory, target, at = running
    memory.write_memory(probe_safety.DHCSR, (1 << 19).to_bytes(4, "little"))
    assert p.target_status()["fatal"]["kind"] == "lockup"
