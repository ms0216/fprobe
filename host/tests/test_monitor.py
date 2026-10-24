"""モニタ方式の手続き（fprobe.monitor）: 箱の見つけ方、切り替え、止める・再開・1 命令、レジスタの対応と書き込み、
ブレークポイント・ウォッチポイントの理由、誤り。偽のコア（monitor_fake）と、試験用ファーム（monitor_demo.elf）の配置。"""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from monitor_fake import BOX, DEMCR, FAST, LOOP, LOOPS, MON_EN, MON_PEND, SLOW, WORK, WRITER, MonitorChip

from pyocd.core.target import Target

from fprobe.dwarf.info import DebugInfo
from fprobe.monitor import (BOX_SIZE, MAGIC, MonitorError, MonitorTarget, Offsets, check_architecture, enter_monitor,
                            find_box, keeps_running, leave_monitor, write_priority)
from fprobe.symbols import Symbols

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "monitor_demo" / "monitor_demo.elf"


def stopped(chip: MonitorChip, **kw) -> MonitorTarget:
    """停止方式で止まった chip をモニタ方式にする（同じ PC で止まったまま）。"""
    target = MonitorTarget(chip, find_box(chip), **kw)
    enter_monitor(chip, target.box)
    return target


def run(target: MonitorTarget, polls: int = 40) -> None:
    for _ in range(polls):
        target.get_state()


# --- 箱 ------------------------------------------------------------------------------------------


def test_the_box_is_found_from_the_debugmon_vector():
    assert find_box(MonitorChip()) == BOX


def test_a_firmware_without_the_monitor_is_a_clear_error():
    with pytest.raises(MonitorError, match="組み込む"):
        find_box(MonitorChip(installed=False))


def test_a_monitor_not_initialized_is_a_clear_error():
    with pytest.raises(MonitorError, match="fprobe_monitor_init"):
        find_box(MonitorChip(initialized=False))


def test_armv6m_has_no_debug_monitor():
    with pytest.raises(MonitorError, match="DebugMonitor"):
        check_architecture(SimpleNamespace(architecture=SimpleNamespace(name="ARMv6M")))
    check_architecture(SimpleNamespace(architecture=SimpleNamespace(name="ARMv7M")))


def test_the_box_layout_matches_the_firmware():
    """試験用ファームの DWARF の struct fprobe_monitor の配置が、ホストの定数と同じ。"""
    t = DebugInfo(DEMO).type_named("struct fprobe_monitor")
    offsets = {m.name: m.offset for m in t.members}
    assert t.size == BOX_SIZE
    for name, offset in vars(Offsets).items():
        if not name.startswith("_"):
            assert offsets[name] == offset, name


def test_the_firmware_handler_carries_the_mark_and_the_box_address():
    symbols = Symbols(DEMO)
    handler = symbols.symbol("DebugMon_Handler").address & ~1
    box = symbols.symbol("fprobe_monitor").address

    def word(address):
        segment = next(s for s in symbols.segments if s.address <= address < s.address + len(s.data))
        return struct.unpack_from("<I", segment.data, address - segment.address)[0]

    vectors = symbols.symbol("vectors").address
    assert word(vectors + 4 * 12) == handler | 1
    marks = [word(a) for a in range(handler & ~3, handler + 16, 4)]
    i = marks.index(MAGIC)
    assert marks[i + 1] == box


# --- 切り替え・止める・再開 --------------------------------------------------------------------------


def test_switching_while_halted_keeps_the_stop_at_the_same_pc():
    chip = MonitorChip()
    target = stopped(chip)
    assert not chip.debugen and chip.demcr & MON_EN and chip.in_monitor
    assert target.get_state() == Target.State.HALTED
    assert target.read_core_register("pc") == LOOP
    assert target.get_halt_reason() == Target.HaltReason.USER


def test_while_stopped_the_higher_priority_interrupt_keeps_running():
    chip = MonitorChip()
    target = stopped(chip)
    fast, slow, loops = chip.var(FAST), chip.var(SLOW), chip.var(LOOPS)
    run(target)
    assert chip.var(FAST) > fast + 30                         # 優先度の高い割り込みは数え続ける
    assert (chip.var(SLOW), chip.var(LOOPS)) == (slow, loops)  # 低い割り込みとスレッドは止まっている
    target.resume()
    run(target)
    assert chip.var(LOOPS) > loops and chip.var(SLOW) > slow


def test_halt_and_resume():
    chip = MonitorChip()
    target = stopped(chip)
    target.resume()
    assert target.get_state() == Target.State.RUNNING
    run(target, 5)
    target.halt()
    assert target.get_state() == Target.State.HALTED
    assert target.get_halt_reason() == Target.HaltReason.USER
    assert not chip.demcr & MON_PEND


def test_halt_that_cannot_enter_the_monitor_is_an_error_and_withdraws_the_request():
    chip = MonitorChip()
    target = stopped(chip, timeout_s=0.05)
    target.resume()
    chip.blocked = True
    with pytest.raises(MonitorError, match="DebugMon に入らない"):
        target.halt()
    assert not chip.demcr & MON_PEND


def test_step_runs_one_instruction_and_masks_lower_interrupts_by_default():
    chip = MonitorChip()
    target = stopped(chip)
    target.step()
    assert target.read_core_register("pc") == LOOP + 2
    assert target.get_halt_reason() == Target.HaltReason.DEBUG
    assert target.read32(0xE000ED30) == 1                     # DFSR は箱の理由（HALTED）
    target.step(disable_interrupts=False)
    assert target.read_core_register("pc") == LOOP + 4
    assert chip.step_args == [1, 0]
    assert chip.box(Offsets.step_forced) == 0                 # 次の入口で BASEPRI を戻した
    assert chip.regs["basepri"] == 0


def test_breakpoint_and_watchpoint_stops_tell_their_reason():
    chip = MonitorChip()
    target = stopped(chip)
    target.set_breakpoint(WORK)
    target.resume()
    run(target)
    assert target.get_state() == Target.State.HALTED
    assert target.read_core_register("pc") == WORK
    assert target.get_halt_reason() == Target.HaltReason.BREAKPOINT
    target.remove_breakpoint(WORK)
    chip.set_watchpoint(LOOPS, 4, None)
    target.resume()
    run(target)
    assert target.get_halt_reason() == Target.HaltReason.WATCHPOINT
    assert target.read_core_register("pc") == WRITER + 2      # ウォッチポイントは命令の後で止まる


def test_line_steps_work_through_the_monitor():
    """行単位のステップ（stepping.LineStepper）は MonitorTarget をそのまま使う。1 命令ずつ割り込みを止めて進める。"""
    from cpu_fake import FakeLines

    from fprobe.debug import Debugger
    chip = MonitorChip()
    target = stopped(chip)
    rows = [(LOOP, 10), (LOOP + 4, 11), (LOOP + 8, 12), (LOOP + 12, 13)]
    dbg = Debugger(target, FakeLines([("spin_loop", LOOP, 16, LOOP)], rows))
    stop = dbg.step_line("over")
    assert (stop.reason, stop.where.line, stop.pc) == ("step", 11, LOOP + 4)
    stop = dbg.step_line("over")
    assert (stop.reason, stop.where.line, stop.pc) == ("step", 12, LOOP + 8)
    assert chip.step_args == [1, 1, 1, 1]


# --- レジスタ ------------------------------------------------------------------------------------


def test_registers_are_those_of_the_interrupted_code():
    chip = MonitorChip()
    truth = dict(chip.regs)
    target = stopped(chip)
    names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "primask", "basepri", "control"]
    assert dict(zip(names, target.read_core_registers_raw(names))) == {n: truth[n] for n in names[:-4]} | {
        "msp": truth["sp"], "primask": 0, "basepri": 0, "control": 0}
    assert target.read_core_register_raw("psp") == chip.other_sp
    assert target.read_core_register_raw("r13") == truth["sp"]


def test_written_registers_take_effect_when_resumed():
    chip = MonitorChip()
    target = stopped(chip)
    target.write_core_registers_raw(["r0", "r5", "pc", "xpsr"], [0xAAAA, 0x5555, WRITER | 1, (1 << 24) | 0x200])
    assert target.read_core_register_raw("r0") == 0xAAAA     # 写しも直る
    target.resume()
    assert chip.regs["r0"] == 0xAAAA and chip.regs["r5"] == 0x5555
    assert chip.regs["xpsr"] == 1 << 24                       # bit9（枠の詰め物）は書いても無視
    run(target)
    assert chip.var(LOOPS) >= 1                               # WRITER から走った


@pytest.mark.parametrize("odd", [False, True])
def test_sp_write_moves_the_frame(odd):
    chip = MonitorChip(odd_sp=odd)
    target = stopped(chip)
    old = target.read_core_register_raw("sp")
    assert old == chip.regs["sp"]
    new = old - 0x40 - (4 if not odd else 0)                  # 詰め物の要る・要らない向きを替える
    target.write_core_registers_raw(["r1", "sp"], [0x1234, new])
    assert target.read_core_register_raw("sp") == new
    target.resume()
    assert chip.regs["sp"] == new and chip.regs["r1"] == 0x1234 and chip.regs["pc"] == LOOP


def test_read_only_registers_accept_the_same_value_only():
    chip = MonitorChip()
    target = stopped(chip)
    control, psp = target.read_core_registers_raw(["control", "psp"])
    target.write_core_registers_raw(["control", "psp", "faultmask"], [control, psp, 0])
    with pytest.raises(MonitorError, match="書けない"):
        target.write_core_register_raw("control", 1)
    with pytest.raises(MonitorError, match="書けない"):
        target.write_core_register_raw("psp", psp + 8)


def test_thread_on_psp():
    chip = MonitorChip(psp=True)
    truth = dict(chip.regs)
    target = stopped(chip)
    sp, psp, msp, control = target.read_core_registers_raw(["sp", "psp", "msp", "control"])
    assert sp == psp == truth["sp"] and msp == chip.other_sp and control == 2
    target.write_core_register_raw("psp", sp - 0x20)          # 枠のある側は書ける
    target.resume()
    assert chip.regs["sp"] == truth["sp"] - 0x20


def test_fpu_registers_with_an_extended_frame():
    chip = MonitorChip(fp=True)
    truth = dict(chip.regs)
    target = stopped(chip)
    names = ["s0", "s15", "s16", "s31", "fpscr"]
    assert target.read_core_registers_raw(names) == [truth[n] for n in names]
    assert target.read_core_register("s0") == pytest.approx(struct.unpack("<f", struct.pack("<I", truth["s0"]))[0])
    assert target.read_core_register_raw("control") == 4      # FPCA
    target.write_core_register("s3", 2.5)
    target.write_core_register_raw("s20", 0x4000_0000)
    target.resume()
    assert chip.regs["s3"] == 0x4020_0000 and chip.regs["s20"] == 0x4000_0000


def test_fpu_registers_without_an_fp_context_are_an_error():
    target = stopped(MonitorChip(fp=False))
    with pytest.raises(MonitorError, match="FPU の文脈"):
        target.read_core_register_raw("s0")


def test_registers_while_running_are_an_error():
    chip = MonitorChip()
    target = stopped(chip)
    target.resume()
    with pytest.raises(MonitorError, match="止まっていない"):
        target.read_core_register_raw("pc")


# --- 停止方式へ戻る・停止方式だけの操作 ----------------------------------------------------------------


def test_leaving_while_stopped_keeps_the_stop_at_the_same_pc():
    chip = MonitorChip()
    target = stopped(chip)
    target.step()
    out = leave_monitor(target, chip)
    assert out["stop"] == "breakpoint_at_pc" and out["pc_before"] == out["pc_after"] == LOOP + 2
    assert chip.debugen and chip.halted and not chip.demcr & MON_EN
    assert chip.regs["pc"] == LOOP + 2
    assert chip.breakpoints == {}                             # 一時のブレークポイントは外した


def test_leaving_keeps_a_user_breakpoint_at_the_pc():
    chip = MonitorChip()
    target = stopped(chip)
    target.set_breakpoint(LOOP)
    leave_monitor(target, chip)
    assert chip.halted and chip.regs["pc"] == LOOP and LOOP in chip.breakpoints


def test_leaving_says_when_it_had_to_halt_by_request():
    # PC に置けない・戻らないときの停止要求（近くで止める）を黙ってしない: どう止めたかと前後の PC を返す（?35）
    chip = MonitorChip()
    target = stopped(chip)
    chip.set_breakpoint = lambda *a, **k: False
    out = leave_monitor(target, chip)
    assert out["stop"] == "halt_request" and out["pc_before"] == LOOP and "pc_after" in out


def test_leaving_while_running_keeps_running():
    chip = MonitorChip()
    target = stopped(chip)
    target.resume()
    leave_monitor(target, chip)
    assert chip.debugen and not chip.halted


def test_halting_only_operations_are_refused():
    chip = MonitorChip()
    target = stopped(chip)
    with pytest.raises(MonitorError, match="止めてのリセット"):
        target.reset_and_halt()
    with pytest.raises(MonitorError, match="ベクタキャッチ"):
        target.set_vector_catch(1)
    target.set_vector_catch(0)


# --- 優先度 --------------------------------------------------------------------------------------


def test_priority_is_rounded_to_the_implemented_bits_and_tells_what_keeps_running():
    chip = MonitorChip()
    assert write_priority(chip, 0x8F) == 0x80
    names = [e["name"] for e in keeps_running(chip, 0x80)]
    assert names == ["SysTick", "IRQ3"]
    assert [e["name"] for e in keeps_running(chip, 0x00)] == []
