"""RISC-V の Debug Module（fprobe.riscv_dm）と、pyOCD のターゲットの口に合わせた包み（fprobe.riscv_target）。
機器は使わない（Hazard3 に倣った DM の偽物）。"""

import pytest
from pyocd.core.target import Target
from riscv_fakes import CSR_DCSR, CSR_DPC, FakeHazard3Dm

from fprobe.debug import Debugger
from fprobe.riscv_dm import CSR, MC_EXECUTE, DebugModule, DmError
from fprobe.riscv_target import RiscvTarget


def module(hart=0, **kw):
    fake = FakeHazard3Dm(**kw)
    dm = DebugModule(fake, hart)
    info = dm.activate()
    return fake, dm, info


def test_activate_finds_the_harts_and_features():
    fake, dm, info = module()
    assert info.as_dict() == {"version": "0.13", "progbuf_size": 2, "data_count": 1, "system_bus": True,
                              "harts": [0, 1]}
    with pytest.raises(DmError, match="ハート 2 が無い"):
        DebugModule(FakeHazard3Dm(), 2).activate()


def test_halt_resume_and_step():
    fake, dm, _ = module(hart=1)
    assert dm.state() == "running"
    dm.halt()
    assert fake.harts[1].halted and not fake.harts[0].halted and dm.halt_cause() == "haltreq"
    dpc = dm.read_csr(CSR["dpc"])
    dm.step()
    assert dm.read_csr(CSR["dpc"]) == dpc + 4 and dm.halt_cause() == "step"
    assert not fake.harts[1].csrs[CSR_DCSR] & (1 << 2)                     # dcsr.step を下ろした
    dm.resume()
    assert dm.state() == "running"


def test_gprs_by_abstract_command_and_csrs_by_the_program_buffer():
    fake, dm, _ = module()
    dm.halt()
    dm.write_gpr(10, 0x1234)
    assert fake.harts[0].gprs[10] == 0x1234 and dm.read_gpr(10) == 0x1234
    fake.harts[0].gprs[8] = 0xCAFE                                          # s0 は借りて戻す
    assert dm.read_csr(CSR["misa"]) == 0x40901105
    dm.write_csr(CSR["mscratch"], 0x55AA)
    assert fake.harts[0].csrs[0x340] == 0x55AA and fake.harts[0].gprs[8] == 0xCAFE
    assert dm._csr_abstract is False                                       # 抽象コマンドの CSR は未対応だった


def test_abstract_commands_need_a_halted_hart():
    fake, dm, _ = module()
    with pytest.raises(DmError, match="止まっていない"):
        dm.read_gpr(1)
    assert fake.cmderr == 0                                                # 誤りは消してある


def test_system_bus_reads_and_writes_without_reading_past_the_end():
    fake, dm, _ = module(memory={0x20000000 + i: i for i in range(16)})
    assert dm.read_memory(0x20000000, 3) == [0x03020100, 0x07060504, 0x0B0A0908]
    reads = [entry for entry in fake.log if entry[0] == "sbread"]
    assert [r[1] for r in reads] == [0x20000000, 0x20000004, 0x20000008]    # 最後の後ろは読まない
    dm.write_memory(0x20000100, [0xDDCCBBAA, 0x11223344])
    assert [fake.memory[0x20000100 + i] for i in range(8)] == [0xAA, 0xBB, 0xCC, 0xDD, 0x44, 0x33, 0x22, 0x11]
    dm.write_memory(0x20000201, [0x7F], size=1)
    assert fake.memory[0x20000201] == 0x7F and dm.read_memory(0x20000201, 1, size=1) == [0x7F]
    fake.bus_errors.add(0x40000000)
    with pytest.raises(DmError, match="bad_address"):
        dm.read_memory(0x40000000, 1)
    assert dm.read_memory(0x20000000, 1) == [0x03020100]                    # 誤りは消してある


def test_triggers_take_execute_but_not_load_store():
    fake, dm, _ = module()
    dm.halt()
    assert dm.trigger_count() == 4
    assert dm.set_trigger(0, 0x20000040, MC_EXECUTE)
    assert fake.harts[0].triggers[0][1] == 0x20000040
    assert not dm.set_trigger(1, 0x20001000, 0b11)                          # 読み・書きの一致は無い
    assert fake.harts[0].triggers[1][0] & 0xF == 0


def test_target_adapter_runs_the_debugger_to_a_breakpoint():
    fake, dm, _ = module(memory={0x20000040 + i: 0x13 if i == 0 else 0 for i in range(4)})
    t = RiscvTarget(dm)
    dbg = Debugger(t)
    assert t.get_state() == Target.State.RUNNING
    dbg.breakpoint(0x20000040)                                             # 走っている間に置く（一時的に止めて書く）
    assert t.get_state() == Target.State.RUNNING and fake.harts[0].triggers[0][1] == 0x20000040
    assert fake.reach(0x20000040)
    assert t.get_state() == Target.State.HALTED and t.get_halt_reason() == Target.HaltReason.BREAKPOINT
    assert t.read_core_register("pc") == 0x20000040
    dbg.resume()                                                           # 1 命令踏み越えてから再開
    assert t.get_state() == Target.State.RUNNING and fake.harts[0].pc == 0x20000044
    assert fake.harts[0].triggers[0][0] & MC_EXECUTE                       # 踏み越えた後で置き直した
    t.halt()
    assert t.get_halt_reason() == Target.HaltReason.USER
    dbg.remove_breakpoint(0x20000040)
    assert not fake.harts[0].triggers[0][0] & MC_EXECUTE


def test_target_adapter_registers_and_memory():
    fake, dm, _ = module(memory={0x20000000 + i: (0x10 + i) for i in range(8)})
    t = RiscvTarget(dm)
    t.halt()
    t.write_core_register("a0", 0x77)
    assert fake.harts[0].gprs[10] == 0x77
    assert t.read_core_registers_raw(["a0", "x10", "zero", "pc"]) == [0x77, 0x77, 0, fake.harts[0].csrs[CSR_DPC]]
    t.write_core_register("pc", 0x20000100)
    assert fake.harts[0].csrs[CSR_DPC] == 0x20000100
    assert t.read_core_register("mscratch") == 0
    with pytest.raises(ValueError, match="知らない"):
        t.read_core_register("r0")
    assert t.read32(0x20000000) == 0x13121110
    assert bytes(t.read_memory_block8(0x20000001, 5)) == bytes([0x11, 0x12, 0x13, 0x14, 0x15])
    t.write_memory_block8(0x20000003, [0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5])
    assert [fake.memory.get(0x20000000 + i, 0) for i in range(10)] == \
        [0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0]
    t.write_memory(0x20000010, 0xBEEF, transfer_size=16)
    assert t.read_memory(0x20000010, transfer_size=16) == 0xBEEF
    assert t.read_memory_block32(0x20000000, 1) == [0xA0121110]
    assert not t.set_watchpoint(0x20000000, 4, Target.WatchpointType.WRITE)  # Hazard3 には無い
    with pytest.raises(RuntimeError, match="リセット"):
        t.reset_and_halt()
