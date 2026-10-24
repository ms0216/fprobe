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
from fprobe.symbols import Symbol, Symbols

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


def test_halt_risk_needs_an_elf():
    with pytest.raises(RuntimeError, match="ELF"):
        Probe("unused").halt_risk()


def test_halt_risk_of_the_demo_elf():
    p = Probe("unused")
    p._symbols = Symbols(DEMO)
    r = p.halt_risk()
    assert r["matched"] is False and r["matches"] == [] and r["elf"].endswith("sram_demo.elf")


def test_halt_risk_has_no_default_names():
    # 既定の名前の表を持たない: 無線・RTOS らしい名前が ELF にあっても、使う側が渡さなければ何も合わない
    p = Probe("unused")
    p._symbols = SimpleNamespace(path=Path("app.elf"), names=lambda: [
        Symbol("RADIO_IRQHandler", 0x100, 4, "function"), Symbol("_kernel", 0x2000, 4, "object")])
    r = p.halt_risk()
    assert r["matched"] is False and r["names"] == [] and r["patterns"] == []


def test_halt_risk_reports_the_names_given_by_the_caller():
    p = Probe("unused")
    p._symbols = SimpleNamespace(path=Path("app.elf"), names=lambda: [
        Symbol("RADIO_IRQHandler", 0x100, 4, "function"), Symbol("_kernel", 0x2000, 4, "object")])
    assert p.halt_risk_set(names=["_kernel"], patterns=["_IRQHandler$"]) ==         {"names": ["_kernel"], "patterns": ["_IRQHandler$"]}
    r = p.halt_risk()
    assert r["matched"] and [(m["query"], m["by"]) for m in r["matches"]] == [("_kernel", "name"),
                                                                              ("_IRQHandler$", "pattern")]
    assert r["names"] == ["_kernel"] and r["patterns"] == ["_IRQHandler$"]
    with pytest.raises(ValueError):
        p.halt_risk_set(patterns=["("])
    assert p.halt_risk_set() == {"names": [], "patterns": []}


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
    assert not memory.u32(DEMCR) >> 24 & 1                                # 条件が無ければ PC を読まない（TRCENA に触らない）


def test_staying_in_the_default_handler_is_not_reported_without_names(running):
    # 「故障の処理」の名前の表を本体に持たない: 渡されなければ default_handler の中にいても fatal を付けない
    p, memory, target, at = running
    assert "fatal" not in at("default_handler", 0) and "fatal" not in at("default_handler", 2)


def test_staying_in_a_given_function_is_reported(running):
    p, memory, target, at = running
    with pytest.raises(ValueError, match="pc_samples"):                     # 読む数の既定は無い（総点検: 暗黙の既定）
        p.liveness_set(functions=["default_handler"])
    assert p.liveness_set(functions=["default_handler"], pc_samples=4) == {"functions": ["default_handler"], "patterns": [],
                                                                           "pc_samples": 4, "tick_variable": None}
    assert at("default_handler", 0)["fatal"]["kind"] == "pc_in"           # 待たずに事実として（経験則の秒数を置かない）
    status = at("default_handler", 2)
    assert status["state"] == "RUNNING"
    assert status["fatal"]["kind"] == "pc_in" and status["fatal"]["function"] == "default_handler"
    assert status["fatal"]["since"] == 1000.0
    target.state = Target.State.HALTED                                   # 止めたら見張りは忘れる
    target.registers["pc"] = p._symbols.symbol("tick").address
    assert "fatal" not in p.target_status()


def test_the_number_of_pc_samples_is_the_callers_and_is_in_the_result(running, monkeypatch):
    p, memory, target, at = running
    reads = []
    original = target.read_memory
    monkeypatch.setattr(target, "read_memory", lambda a, *k: (reads.append(a), original(a, *k))[1])
    assert p.liveness_set(patterns=["^default_handler$"], pc_samples=7)["pc_samples"] == 7
    assert p.liveness_get() == {"functions": [], "patterns": ["^default_handler$"], "pc_samples": 7,
                                "tick_variable": None}
    reads.clear()
    fatal = at("default_handler", 0)["fatal"]
    assert reads.count(DWT_PCSR) == 7
    assert fatal["evidence"][0] == {"clue": "pc_in", "function": "default_handler", "valid": 7, "pc_samples": 7}
    with pytest.raises(ValueError):
        p.liveness_set(functions=["default_handler"], pc_samples=0)


def test_the_tick_variable_is_the_one_the_caller_names(running):
    # ティックの変数を名前の慣習（xTickCount・uwTick 等）で探さない（点検 2 回目 H6）: 使う側が tick_variable で渡す
    p, memory, target, at = running
    assert p.liveness_set(pc_samples=2)["tick_variable"] is None
    assert "fatal" not in at("tick", 0) and "fatal" not in at("tick", 1)        # 名前が無ければティックを読まない
    settings = p.liveness_set(pc_samples=2, tick_variable="counter")
    assert settings == {"functions": [], "patterns": [], "pc_samples": 2, "tick_variable": "counter"}
    assert p.liveness_get()["tick_variable"] == "counter"
    assert "fatal" not in at("tick", 0)
    fatal = at("tick", 1)["fatal"]                                              # counter が変わっていない
    assert fatal["kind"] == "tick_stopped" and {"clue": "tick_stopped", "tick": 0} in fatal["evidence"]
    with pytest.raises(ValueError, match="pc_samples"):
        p.liveness_set(tick_variable="counter")
    with pytest.raises(KeyError, match="no_such"):
        p.liveness_set(pc_samples=2, tick_variable="no_such")


def test_lockup_is_reported_at_once(running):
    p, memory, target, at = running
    memory.write_memory(probe_safety.DHCSR, (1 << 19).to_bytes(4, "little"))
    assert p.target_status()["fatal"]["kind"] == "lockup"


def test_halt_risk_conditions_can_be_read_without_an_elf():
    # 条件はデーモンが持つ事実。ELF が無くても読める（画面の欄が ELF を待たずに今の条件を描ける）
    p = Probe("unused")
    assert p.halt_risk_get() == {"names": [], "patterns": []}
    p.halt_risk_set(names=["RADIO_IRQHandler"], patterns=["^ll_"])
    assert p.halt_risk_get() == {"names": ["RADIO_IRQHandler"], "patterns": ["^ll_"]}
