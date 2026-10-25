"""U32: Cortex-A7（Raspberry Pi 2 の BCM2836）と ARM1176（Pi Zero の BCM2835）の JTAG のデバッグ。機器は使わない
（偽の CMSIS-DAP の層の JTAG-DP・APB-AP の向こうのデバッグレジスタ、偽の JTAG のチェーンの ARM11 の TAP）。"""

import pytest
from arm_a_fakes import HDBGEN, INTDIS, ITREN, ArmCore, arm11_tap, bcm2836
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from jtag_fakes import FakeChain, Tap
from pyocd.core.target import Target

from fprobe.arm_a_target import arm_a_part, open_arm_a
from fprobe.arm_debug import REGISTER_NAMES, ArmDebugError

PI2_IDCODE = 0x4BA00477


def pi2(with_rom: bool = True, cores: list[ArmCore] | None = None):
    cores = cores or [ArmCore(mpidr=0x80000F00 | i) for i in range(4)]
    apb, blocks = bcm2836(cores, with_rom)
    target = FakeTarget(dpidr=PI2_IDCODE, swd=False, jtag_dp_index=0, aps={0: FakeMemAp(), 1: apb},
                        jtag_chain=FakeChain([Tap(4, PI2_IDCODE)]))
    return FakeDapLink(target), cores, blocks


def zero(core: ArmCore | None = None):
    core = core or ArmCore(arch=6, wrps=2, ctr=0x1D152152, mpidr=0)
    tap = arm11_tap(core)
    return FakeDapLink(FakeTarget(swd=False, jtag_chain=FakeChain([tap]))), core, tap


def connect(link, name: str, core: int = 0, mode: str = "halt"):
    return open_arm_a(link, arm_a_part(name), core, 1_000_000, mode)


# --- Cortex-A7（ARMv7-A）-----------------------------------------------------------------------------

def test_v7_finds_the_core_through_the_rom_table_unlocks_and_halts_only_that_core():
    link, cores, blocks = pi2()
    cores[1].regs[15] = 0x8000
    conn = connect(link, "bcm2836", core=1)
    info = conn.info()
    assert info["arch"] == "armv7-a" and info["core"] == 1 and info["cores"] == 4
    assert info["debug_base"] == 0x80012000 and info["apb_ap"] == 1 and info["discovery"] == "rom_table"
    assert info["breakpoints"] == 5 and info["watchpoints"] == 4 and info["jtag_chain"] == [4]
    assert not blocks[1].os_lock and cores[1].halted and not cores[0].halted
    assert conn.target.get_state() == Target.State.HALTED
    assert conn.target.read_core_register("pc") == 0x8000
    conn.close()
    assert link.closed


def test_v7_falls_back_to_the_part_table_without_a_rom_table():
    link, cores, _ = pi2(with_rom=False)
    conn = connect(link, "bcm2836", core=3)
    assert conn.info()["discovery"] == "part_table" and conn.info()["debug_base"] == 0x80016000
    assert cores[3].halted
    link2, _, _ = pi2(with_rom=False)
    with pytest.raises(ArmDebugError, match="ROM"):
        connect(link2, "cortex_a")                                   # 汎用の名前には表が無い
    assert link2.closed


def test_v7_attach_does_not_halt_and_core_out_of_range_is_refused():
    link, cores, _ = pi2()
    conn = connect(link, "bcm2836", mode="attach")
    assert not cores[0].halted and cores[0].ctrl & HDBGEN
    assert conn.target.get_state() == Target.State.RUNNING
    with pytest.raises(ArmDebugError, match="走っている"):
        conn.target.read_core_register("r0")
    link2, _, _ = pi2()
    with pytest.raises(ValueError, match="0〜3"):
        connect(link2, "bcm2836", core=4)


def test_v7_halt_refused_when_debug_is_not_allowed():
    core = ArmCore()
    core.write_dscr = lambda value: setattr(core, "ctrl", value & ~HDBGEN)          # DBGEN が低い
    link, _, _ = pi2(cores=[core] * 4)
    with pytest.raises(ArmDebugError, match="DBGEN"):
        connect(link, "bcm2836")


def test_v7_registers_are_cached_and_written_back_on_resume():
    link, cores, _ = pi2()
    core = cores[0]
    core.regs = [0x11 * i for i in range(16)]
    core.regs[15], core.cpsr = 0x8000, 0x600001D3
    t = connect(link, "bcm2836").target
    values = t.read_core_registers_raw(REGISTER_NAMES)
    assert values == [0x11 * i for i in range(15)] + [0x8000, 0x600001D3]
    t.write_core_register("r0", 0xA0)
    t.write_core_register("r5", 0x55)
    t.write_core_register("pc", 0x9000)
    t.write_core_register("cpsr", 0x1D3)
    assert core.regs[5] == 0x55                                      # r2〜r14 はすぐ書く
    assert t.read_core_register("pc") == 0x9000 and t.read_core_register("r0") == 0xA0
    t.resume()
    assert not core.halted and not core.ctrl & ITREN
    assert core.regs[0] == 0xA0 and core.regs[1] == 0x11 and core.regs[15] == 0x9000 and core.cpsr == 0x1D3
    with pytest.raises(ValueError, match="r0〜r15"):
        t.read_core_register("spsr")


def test_v7_memory_through_the_core_with_cache_maintenance_after_writes():
    link, cores, _ = pi2()
    core = cores[0]
    core.memory = {0x1000 + i: i for i in range(0x100)}
    core.regs[0], core.regs[1] = 0x1234, 0x5678
    t = connect(link, "bcm2836").target
    assert t.read_memory_block8(0x1001, 6) == [1, 2, 3, 4, 5, 6]
    assert t.read32(0x1004) == 0x07060504 and t.read_memory(0x1002, 16) == 0x0302
    t.write_memory_block8(0x103E, [0xAA, 0xBB, 0xCC, 0xDD, 0xEE])
    assert [core.memory[0x103E + i] for i in range(5)] == [0xAA, 0xBB, 0xCC, 0xDD, 0xEE]
    ops = core.cache_ops
    assert ("dccmvau", 0x1000) in ops and ("dccmvau", 0x1040) in ops            # 64 バイトの行の 2 つにまたがる
    assert ("iciallis", 0) in ops and ("bpiallis", 0) in ops                     # MP 拡張: 全コアの I キャッシュ
    t.resume()
    assert core.regs[0] == 0x1234 and core.regs[1] == 0x5678                    # 借りたレジスタは戻す


def test_v7_fast_read_matches_slow_read():
    """4 語以上は DCC の速いモードとブロック転送で読む（1 語ごとの往復を無くす）。最後の語を落とさない"""
    link, cores, _ = pi2()
    core = cores[0]
    core.memory = {0x2000 + i: (i * 37 + 5) & 0xFF for i in range(0x400)}
    expect = [int.from_bytes(bytes(core.memory[0x2000 + 4 * w + i] for i in range(4)), "little") for w in range(256)]
    t = connect(link, "bcm2836").target
    for n in (1, 2, 3, 4, 5, 256):
        assert t.read_memory_block32(0x2000, n) == expect[:n], n
    assert link.repeat_reads >= 255                                     # 速い読みはブロック転送を使った
    assert core.ctrl & (3 << 20) == 0                                   # 非ブロックのモードに戻した
    t.resume()


def test_v7_fast_read_reports_abort():
    link, cores, _ = pi2()
    cores[0].faults = [(0x2010, 0x2014)]
    t = connect(link, "bcm2836").target
    with pytest.raises(ArmDebugError, match="0x00002000.*アボート"):
        t.read_memory_block32(0x2000, 16)
    assert cores[0].sticky == 0 and cores[0].ctrl & (3 << 20) == 0
    assert t.read_memory_block32(0x3000, 8) == [0] * 8                   # 続けて読める
    t.resume()


def test_v7_abort_is_reported_with_the_address_and_cleared():
    link, cores, _ = pi2()
    cores[0].faults = [(0x5000, 0x5004)]
    t = connect(link, "bcm2836").target
    with pytest.raises(ArmDebugError, match="0x00005000.*アボート"):
        t.read32(0x5000)
    assert cores[0].sticky == 0
    with pytest.raises(ArmDebugError, match="0x00005000"):
        t.write32(0x5000, 1)
    assert t.read32(0x6000) == 0


def test_v7_step_masks_interrupts_with_intdis_and_uses_a_mismatch_breakpoint():
    link, cores, _ = pi2()
    core = cores[0]
    core.regs[15], core.irq_pending = 0x8000, True
    t = connect(link, "bcm2836").target
    t.step()
    assert t.read_core_register("pc") == 0x8004 and t.get_halt_reason() == Target.HaltReason.DEBUG
    assert not any(c & 1 for c in core.bcr) and not core.ctrl & INTDIS
    t.step(disable_interrupts=False)
    assert t.read_core_register("pc") == 0x18                                   # 割り込みに入った
    t.write_core_register("cpsr", 0x33)                                          # Thumb
    t.write_core_register("pc", 0x8002)
    core.irq_pending = False
    t.step()
    assert t.read_core_register("pc") == 0x8004


def test_a_step_that_times_out_and_is_halted_by_request_reports_the_request():
    # ステップが時間切れ（WFI 等）で停止要求で止めたら、止まった理由は step ではなく要求（測った事実。?2）
    link, cores, _ = pi2()
    core = cores[0]
    core.regs[15], core.asleep = 0x8000, True
    t = connect(link, "bcm2836").target
    t.core.timeout_s = 0.05
    t.step()
    assert t.get_halt_reason() == Target.HaltReason.USER and t.core.halt_cause() == "halt_request"


def test_v7_breakpoints_and_watchpoints_hit():
    link, cores, _ = pi2()
    core = cores[0]
    t = connect(link, "bcm2836").target
    assert t.set_breakpoint(0x8100)
    assert core.bvr[0] == 0x8100 and core.bcr[0] == (0xF << 5) | 7
    assert not t.set_breakpoint(0x9000, Target.BreakpointType.SW)                # ソフトの BKPT は使わない
    assert all(t.set_breakpoint(0x8200 + 4 * i) for i in range(4))
    assert not t.set_breakpoint(0x8300)                                         # 6 組のうち 1 組はステップ用
    t.resume()
    assert core.reach(0x8100)
    assert t.get_state() == Target.State.HALTED and t.read_core_register("pc") == 0x8100
    assert t.get_halt_reason() == Target.HaltReason.BREAKPOINT
    t.remove_breakpoint(0x8100)
    assert not core.bcr[0] & 1
    assert t.set_watchpoint(0x2002, 2, Target.WatchpointType.WRITE)
    assert core.wvr[0] == 0x2000 and core.wcr[0] == (0b1100 << 5) | (2 << 3) | 7
    assert not t.set_watchpoint(0x2003, 2, Target.WatchpointType.READ)          # 語をまたぐ
    t.resume()
    assert not core.access(0x2002, write=False) and core.access(0x2002, write=True)
    assert t.get_halt_reason() == Target.HaltReason.WATCHPOINT


def test_v7_breakpoint_set_while_running_halts_briefly():
    link, cores, _ = pi2()
    core = cores[0]
    t = connect(link, "bcm2836", mode="attach").target
    assert t.set_breakpoint(0x8100)
    assert not core.halted and core.restarts == 1 and core.bcr[0] & 1


def test_v7_reset_and_vector_catch_are_refused():
    link, _, _ = pi2()
    t = connect(link, "bcm2836").target
    with pytest.raises(RuntimeError, match="リセット"):
        t.reset()
    with pytest.raises(RuntimeError, match="ベクタキャッチ"):
        t.set_vector_catch(1)


# --- ARM1176（ARMv6）----------------------------------------------------------------------------------

def test_arm11_connect_halts_through_the_tap():
    link, core, tap = zero()
    core.regs[15], core.cpsr = 0xC000, 0x1D3
    conn = connect(link, "bcm2835")
    info = conn.info()
    assert info["arch"] == "armv6" and info["core"] == 0 and info["tap"] == 0 and info["jtag_chain"] == [5]
    assert info["breakpoints"] == 5 and info["watchpoints"] == 2
    assert core.halted and core.ctrl & HDBGEN
    assert conn.target.read_core_register("pc") == 0xC000
    assert not core.ignored                                                     # 命令はどれも実行された
    with pytest.raises(RuntimeError, match="DP"):
        conn.target.dp.read_dp(0)


def test_arm11_registers_memory_and_v6_cache_maintenance():
    link, core, _ = zero()
    core.regs = [0x100 + i for i in range(16)]
    core.regs[15] = 0xC000
    core.memory = {0x2000 + i: i for i in range(0x40)}
    t = connect(link, "bcm2835").target
    assert t.read_core_registers_raw(["r0", "r1", "r7", "sp", "lr"]) == [0x100, 0x101, 0x107, 0x10D, 0x10E]
    t.write_core_register("r7", 0x77)
    assert core.regs[7] == 0x77
    assert t.read_memory_block32(0x2004, 2) == [0x07060504, 0x0B0A0908]
    t.write_memory_block8(0x201F, [0xAA, 0xBB])
    assert core.memory[0x201F] == 0xAA and core.memory[0x2020] == 0xBB
    names = [name for name, _ in core.cache_ops]
    assert ("v6_clean_dline", 0x2000) in core.cache_ops and ("v6_clean_dline", 0x2020) in core.cache_ops
    assert "v6_dsb" in names and "iciallu" in names and "bpiall" in names
    assert "dsb" not in names and core.sticky == 0                              # ARMv7 の命令は使わない
    t.resume()
    assert core.regs[0] == 0x100 and core.regs[1] == 0x101 and not core.halted


def test_arm11_step_masks_interrupts_in_cpsr_only_for_that_instruction():
    link, core, _ = zero()
    core.regs[15], core.irq_pending = 0xC000, True
    t = connect(link, "bcm2835").target
    t.step()
    assert t.read_core_register("pc") == 0xC004 and t.read_core_register("cpsr") == 0x13
    t.resume()
    assert core.cpsr == 0x13                                                    # I は元に戻した


def test_arm11_breakpoints_go_through_scan_chain_7():
    link, core, tap = zero()
    t = connect(link, "bcm2835").target
    assert t.set_breakpoint(0xC100)
    assert (64, 0xC100) in tap.sc7_writes and (80, (0xF << 5) | 7) in tap.sc7_writes
    t.resume()
    assert core.reach(0xC100) and t.get_halt_reason() == Target.HaltReason.BREAKPOINT
    assert t.set_watchpoint(0x3000, 4, Target.WatchpointType.READ_WRITE)
    assert (96, 0x3000) in tap.sc7_writes and (112, (0xF << 5) | (3 << 3) | 7) in tap.sc7_writes


def test_arm11_not_found_on_the_chain():
    link, _, _ = pi2()
    with pytest.raises(ArmDebugError, match="ARM1176"):
        connect(link, "bcm2835")
    assert link.closed


def test_zynq7000_is_in_the_part_table():
    # Zynq-7000（Cortex-A9 × 2）。デバッグの番地は OpenOCD の zynq の設定と同じ値で、実機では未確認（ROM テーブルが読めればそちらが先）
    part = arm_a_part("zynq7000")
    assert part is not None and part.kind == "v7" and part.idcode == 0x0BA00477
    assert part.debug_bases == (0x80090000, 0x80092000)
