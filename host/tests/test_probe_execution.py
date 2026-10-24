"""Probe の実行の制御の道具（fprobe.probe_execution）: debug.step・target.vector_catch・target.reset の種類・
debug.call。機器は使わない（偽の Cortex-M と偽の行表）。"""

import pytest
from pyocd.core.target import Target
from test_probe_observe import connect, probe  # noqa: F401（probe は fixture）
from test_stepping import FUNCTIONS, PROGRAM, ROWS

from cpu_fake import DEMCR, VC_HARDERR, VTOR, FakeCpu, FakeLines

CFSR = 0xE000ED28


def setup(probe, pc=0x400) -> FakeCpu:
    cpu = FakeCpu({**PROGRAM, 0x20: ("spin",)}, pc, handlers={15: 0x500, 3: 0x700})
    cpu.write32(VTOR, 0)
    cpu.write32(4, 0x21)                                        # リセットの入口 = 0x20（関数の呼び出しの踏み台）
    connect(probe, cpu)
    probe._symbols = FakeLines(FUNCTIONS, ROWS)
    return cpu


def test_step_returns_the_place_like_run_until_halt(probe):
    setup(probe)
    out = probe.step_line("into")
    assert out["reason"] == "step" and out["function"] == "main" and out["line"] == 11 and out["file"] == "demo.c"
    out = probe.step_line("until", location="demo.c:17")
    assert (out["reason"], out["line"]) == ("step", 17)
    out = probe.step_line("out")
    assert (out["reason"], out["function"]) == ("step", "start")


def test_a_fault_while_stepping_carries_the_fault_summary(probe):
    cpu = setup(probe, pc=0x802)
    cpu.write32(CFSR, 1 << 17)                                  # INVSTATE
    cpu.write32(DEMCR, VC_HARDERR)
    out = probe.step_line("over")
    assert out["reason"] == "vector_catch" and out["fault"]["causes"] == ["INVSTATE"]


def test_vector_catch_is_set_and_read_back(probe):
    cpu = setup(probe)
    out = probe.vector_catch(["hard_fault", "core_reset"])
    assert out["enabled"] == ["core_reset", "hard_fault"] and out["unsupported"] == []
    assert cpu.vector_catch == Target.VectorCatch.HARD_FAULT | Target.VectorCatch.CORE_RESET
    assert probe.vector_catch()["enabled"] == ["core_reset", "hard_fault"]
    assert probe.vector_catch_get() == probe.vector_catch()                # 読むだけ（API では状態を変えない読み）
    assert len(probe.vector_catch(["all"])["enabled"]) == 9
    assert probe.vector_catch([])["enabled"] == []
    with pytest.raises(ValueError):
        probe.vector_catch(["hardfault"])


def test_vector_catch_reports_what_the_core_does_not_have(probe):
    cpu = setup(probe)
    cpu.get_vector_catch = lambda: cpu.vector_catch & (Target.VectorCatch.HARD_FAULT | Target.VectorCatch.CORE_RESET)
    out = probe.vector_catch(["hard_fault", "bus_fault"])                  # ARMv6-M の形
    assert out["enabled"] == ["hard_fault"] and out["unsupported"] == ["bus_fault"]


def test_reset_kinds_choose_the_pyocd_reset_type(probe):
    cpu = setup(probe)
    assert probe.target_reset(halt=True, kind="hardware")["kind"] == "hardware"
    probe.target_reset(kind="system")
    probe.target_reset()
    assert cpu.resets == [("reset_and_halt", Target.ResetType.HARDWARE), ("reset", Target.ResetType.SYSRESETREQ),
                          ("reset", None)]
    cpu.supported_reset_types.remove(Target.ResetType.VECTRESET)          # ARMv6-M
    with pytest.raises(ValueError):
        probe.target_reset(kind="core")
    with pytest.raises(ValueError):
        probe.target_reset(kind="warm")


def test_call_runs_a_function_and_restores_the_core(probe):
    cpu = setup(probe, pc=0x104)
    cpu.program.update({0x200: ("do", lambda c: c.regs.update(r0=c.regs["r0"] * 3)), 0x202: ("ret",)})
    before = dict(cpu.regs)
    out = probe.call_function("0x200", [14])
    assert out["returned"] and out["return"]["r0"] == 42 and out["restored"]          # 型が分からない: r0 のまま
    assert cpu.regs == before and not cpu.breakpoints
    out = probe.call_function("square", [14])                              # 名前（DWARF の無い偽の ELF: 型は分からない）
    assert out["return"]["r0"] == 42
