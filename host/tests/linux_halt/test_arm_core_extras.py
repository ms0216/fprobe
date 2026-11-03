"""ArmDebugCore additions the Linux layer needs: CP15 reads, banked SVC stack pointer, numeric entry reason, per-core
instruction-cache invalidation. Against the fprobe fakes (no instrument)."""
import pytest
from arm_a_fakes import ArmCore, bcm2836
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from jtag_fakes import FakeChain, Tap

from fprobe.arm_a_target import arm_a_part, open_arm_a
from fprobe.arm_debug import CP15

MODE_USR, MODE_SVC = 0x10, 0x13


def connect(core: ArmCore):
    cores = [core] + [ArmCore(mpidr=0x80000F00 | i) for i in range(1, 4)]
    apb, _ = bcm2836(cores, True)
    target = FakeTarget(dpidr=0x4BA00477, swd=False, jtag_dp_index=0, aps={0: FakeMemAp(), 1: apb},
                        jtag_chain=FakeChain([Tap(4, 0x4BA00477)]))
    conn = open_arm_a(FakeDapLink(target), arm_a_part("bcm2836"), 0, 1_000_000, "halt")
    return conn.target.core


def test_read_cp15_returns_the_named_register_and_gives_r0_back():
    c = ArmCore(cp15={"ttbr0": 0x40004000, "ttbr1": 0x40008000, "ttbcr": 2, "contextidr": 0x2A05, "sctlr": 0x10C5387D},
                cpsr=MODE_USR)
    c.regs[0] = 0x1234
    core = connect(c)
    for name, value in c.cp15.items():
        assert core.read_cp15(name) == value
    core.resume()
    assert c.regs[0] == 0x1234                                     # the borrowed register is restored before running


def test_read_cp15_refuses_a_name_it_does_not_know():
    core = connect(ArmCore())
    with pytest.raises(ValueError, match="dfar"):
        core.read_cp15("dfar")


def test_the_cp15_reads_are_not_mistaken_for_cache_operations_by_the_name_table():
    for name in ("ttbr0", "ttbr1", "ttbcr", "contextidr", "sctlr"):
        assert name in CP15


def test_banked_sp_of_another_mode_is_read_and_the_mode_is_restored():
    c = ArmCore(cpsr=MODE_USR | 0x60000000)                         # user mode, with some flags set
    c.regs[13] = 0x7EFFF000                                         # the user sp
    c.bank_sp = {MODE_SVC: 0xC0123450}
    core = connect(c)
    assert core.read_banked_sp(MODE_SVC) == 0xC0123450
    assert c.cpsr == MODE_USR | 0x60000000 and c.regs[13] == 0x7EFFF000      # nothing else moved
    assert core.read_register("cpsr") == MODE_USR | 0x60000000


def test_banked_sp_of_the_current_mode_switches_nothing():
    c = ArmCore(cpsr=MODE_SVC)
    c.regs[13] = 0xC0AAAAA0
    core = connect(c)
    before = len(c.executed)
    assert core.read_banked_sp(MODE_SVC) == 0xC0AAAAA0
    from fprobe.arm_debug import MSR_CPSR_R0
    assert MSR_CPSR_R0 not in c.executed[before:]


def test_entry_reason_is_the_number_in_dscr_moe():
    c = ArmCore()
    core = connect(c)
    assert core.entry_reason() == 0                                  # halted by our request
    core.resume()
    assert core.entry_reason() is None                               # running: no entry
    c.reach(0x8000)                                                  # (no breakpoint set: stays running)
    c.halted, c.moe = True, 3                                        # a BKPT instruction
    core.state()
    assert core.entry_reason() == 3


def test_invalidate_instruction_caches_runs_iciallu_and_bpiall_on_that_core_only():
    cores = [ArmCore(mpidr=0x80000F00 | i) for i in range(4)]
    apb, _ = bcm2836(cores, True)
    target = FakeTarget(dpidr=0x4BA00477, swd=False, jtag_dp_index=0, aps={0: FakeMemAp(), 1: apb},
                        jtag_chain=FakeChain([Tap(4, 0x4BA00477)]))
    core = open_arm_a(FakeDapLink(target), arm_a_part("bcm2836"), 1, 1_000_000, "halt").target.core
    core.invalidate_instruction_caches()
    assert [op for op, _ in cores[1].cache_ops if op in ("iciallu", "bpiall")] == ["iciallu", "bpiall"]
    assert not cores[0].cache_ops
