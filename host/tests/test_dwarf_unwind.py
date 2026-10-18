"""fprobe.dwarf.unwind: CFI による巻き戻しと Cortex-M の例外のフレーム。

偽のスタックは dwarf_stack.StackBuilder が逆アセンブルで関数の前置きをなぞって組み立てる（CFI とは独立）。
"""

from pathlib import Path

import pytest
from dwarf_stack import StackBuilder

from fprobe.dwarf.info import DebugInfo
from fprobe.dwarf.location import NotAvailable
from fprobe.dwarf.unwind import CallFrameTable, Unwinder, is_exc_return
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo"
SOURCE = (FIRMWARE / "dwarf_demo.c").read_text(encoding="utf-8").splitlines()
RESET_LR = 0xFFFFFFFF


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


def load(name):
    info, syms = DebugInfo(FIRMWARE / name), Symbols(FIRMWARE / name)
    return info, syms, Unwinder(CallFrameTable.from_dwarf(info.dwarf))


def names(info, frames):
    return [info.function_at(f.lookup_pc).name if info.function_at(f.lookup_pc) else None for f in frames]


def test_exc_return_values():
    assert all(is_exc_return(v) for v in (0xFFFFFFE1, 0xFFFFFFE9, 0xFFFFFFED, 0xFFFFFFF1, 0xFFFFFFF9, 0xFFFFFFFD,
                                         0xFFFFFFBC))                 # 最後は v8-M（Secure 側の設定）
    assert not any(is_exc_return(v) for v in (0xFFFFFFFF, 0x2000_0041, 0xFFFFFF00 - 1, 0))


def test_o0_deep_recursion_unwinds_to_reset():
    info, syms, unwinder = load("dwarf_demo_O0.elf")
    sensors = syms.variable("sensors").address
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=RESET_LR)
    b.call("reset_handler", "walk", sensors, 3)
    for depth in (2, 1, 0):
        b.call("walk", "walk", sensors + 80, depth)
    b.call("walk", "leaf_sum", sensors + 80, 0)
    pc = b.call("leaf_sum", "probe_point", 1234)
    frames = unwinder.unwind(b.halt_at(pc))

    assert names(info, frames) == ["probe_point", "leaf_sum", "walk", "walk", "walk", "walk", "reset_handler"]
    assert frames[0].pc == pc and frames[0].lookup_pc == pc and all(f.note == "" for f in frames)
    lines = [syms.describe(f.lookup_pc).line for f in frames[1:]]
    assert lines == [line_of("probe_point(total + corner.x);"), line_of("return leaf_sum(s, depth);"),
                     *[line_of("int deeper = walk(")] * 3, line_of("sink = walk(head, 3)")]
    # 呼び出し元の sp・r7 は、その関数が前置きを終えたときの値（なぞったスタックと一致）
    callers = list(reversed(b.calls))                                 # 内側から: (関数, 前置きの後, レジスタ)
    for frame, (callee, _, _), (caller, _, regs) in zip(frames[1:], callers, callers[1:]):
        assert frame.registers.read(13) == regs["sp"] and frame.registers.read(7) == regs["r7"], caller
        assert frame.pc == b.call_site(caller, callee) + 4                # 戻り先（bl の次）
    for inner, outer in zip(frames, frames[1:]):
        assert inner.cfa == outer.registers.read(13)                 # CFA = 呼び出し元の sp
    with pytest.raises(NotAvailable):
        frames[1].registers.read(0)                                  # caller-saved は呼び出し元では分からない


def test_o1_restores_callee_saved_core_and_fp_registers():
    info, syms, unwinder = load("dwarf_demo_O1.elf")
    sensors = syms.variable("sensors").address
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=RESET_LR)
    b.call("reset_handler", "walk", sensors, 3)
    b.set(r4=0x4444, s16=0x1616, s17=0x1717)                         # walk が使っている callee-saved
    b.call("walk", "leaf_sum", sensors + 80, 0)
    b.set(r4=0, r5=600, s16=0x40400000)                              # leaf_sum: depth=0 → r4、total → r5、ratio → s16
    pc = b.call("leaf_sum", "probe_point", 600)
    frames = unwinder.unwind(b.halt_at(pc))
    assert names(info, frames) == ["probe_point", "leaf_sum", "walk", "reset_handler"]
    leaf, walk = frames[1], frames[2]
    assert leaf.registers.read(4) == 0 and leaf.registers.read(5) == 600     # probe_point は触らない: 同じ値
    assert leaf.registers.read(80) == 0x40400000
    assert walk.registers.read(4) == 0x4444                          # leaf_sum が push したものを戻す
    assert walk.registers.read(80) == 0x1616 and walk.registers.read(81) == 0x1717   # vpush {d8} を戻す
    assert walk.registers.read(264) == 0x0000171700001616
    assert syms.describe(walk.lookup_pc).line == line_of("return leaf_sum(s, depth);")


@pytest.mark.parametrize("case", ["msp", "msp_padded", "fp_extended", "psp"])
def test_exception_frames_are_crossed(case):
    info, syms, unwinder = load("dwarf_demo_O0.elf")
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=RESET_LR)
    thread = dict(b.calls[-1][2])
    if case == "psp":
        b.use_psp(0x2000_6000)
    if case == "msp_padded":
        b.set(sp=b.regs["sp"] - 4, msp=b.regs["sp"] - 4)               # 例外の入口で SP が 8 バイトに揃っていない
    sp_before = b.regs["sp"]
    interrupted = b.call_site("reset_handler", "walk")               # bl walk の直前で割り込まれた
    b.set(r0=0xA0, r1=0xA1, r2=0xA2, r3=0xA3, r12=0xAC, lr=0x2000_0123)
    exc = {"msp": 0xFFFFFFF9, "msp_padded": 0xFFFFFFF9, "fp_extended": 0xFFFFFFE9, "psp": 0xFFFFFFFD}[case]
    fp = [0x3F80_0000 + n for n in range(16)]
    if case == "fp_extended":
        b.memory.write_u32(0xE000EF34, 0xC000_0000)                  # FPCCR: ASPEN|LSPEN、LSPACT = 0（退避済み）
    b.interrupt("SysTick_Handler", interrupted, exc_return=exc, fp=fp)
    pc = b.call("SysTick_Handler", "probe_point", -1)
    frames = unwinder.unwind(b.halt_at(pc))

    assert names(info, frames) == ["probe_point", "SysTick_Handler", "reset_handler"]
    stopped = frames[2]
    assert stopped.note == "exception" and stopped.pc == interrupted and stopped.lookup_pc == interrupted
    regs = stopped.registers
    assert [regs.read(n) for n in (0, 1, 2, 3, 12, 14)] == [0xA0, 0xA1, 0xA2, 0xA3, 0xAC, 0x2000_0123]
    assert regs.read(13) == sp_before                                  # 積んだフレーム（と詰め物）の上
    assert regs.read(7) == thread["r7"]                              # r7 は SysTick_Handler が push したもの
    assert regs.special("xpsr") & 0x1FF == 0
    if case == "psp":
        assert regs.special("psp") == sp_before and regs.special("msp") == frames[1].cfa
    else:
        assert regs.special("msp") == sp_before
    if case == "fp_extended":
        assert [regs.read(64 + n) for n in range(16)] == fp
    else:
        with pytest.raises(NotAvailable):
            regs.read(64)


def test_lazy_fp_state_is_not_trusted():
    _, syms, unwinder = load("dwarf_demo_O0.elf")
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=RESET_LR)
    b.memory.write_u32(0xE000EF34, 0xC000_0001)                      # LSPACT = 1: s0〜s15 はまだ積まれていない
    b.interrupt("SysTick_Handler", b.call_site("reset_handler", "walk"), exc_return=0xFFFFFFE9)
    frames = unwinder.unwind(b.halt_at(b.call("SysTick_Handler", "probe_point", -1)))
    assert frames[2].note == "exception"
    with pytest.raises(NotAvailable):
        frames[2].registers.read(64)


def test_o1_tail_call_from_a_handler_returns_straight_to_the_interrupted_code():
    # SysTick_Handler（-O1）は probe_point へ末尾呼び出しする: probe_point の lr が EXC_RETURN
    info, syms, unwinder = load("dwarf_demo_O1.elf")
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=RESET_LR)
    interrupted = b.call_site("reset_handler", "walk")
    b.interrupt("probe_point", interrupted)
    frames = unwinder.unwind(b.halt_at(b.regs["pc"]))
    assert names(info, frames) == ["probe_point", "reset_handler"]
    assert frames[1].note == "exception" and frames[1].pc == interrupted


def test_frame_zero_without_cfi_is_treated_as_a_leaf():
    info, syms, unwinder = load("dwarf_demo_O0.elf")
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=RESET_LR)
    site = b.call_site("reset_handler", "walk")
    access = b.halt_at(0x2000_0010, lr=(site + 4) | 1)               # ベクタ表の中（FDE が無い）
    frames = unwinder.unwind(access)
    assert names(info, frames) == [None, "reset_handler"]
    assert frames[1].pc == site + 4


def test_unwinding_stops_when_the_stack_is_unreadable():
    _, syms, unwinder = load("dwarf_demo_O0.elf")
    b = StackBuilder(syms, top=0x3000_0000)
    pc = b.enter("probe_point", 1, lr=0x2000_0101)
    access = b.halt_at(pc)
    access.registers["r7"] = 0x1000_0000                             # CFA = r7 + 8 が読めない番地
    frames = unwinder.unwind(access)
    assert len(frames) == 1 and frames[0].pc == pc
