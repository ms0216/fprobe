"""fprobe.stepping / Debugger.step_line: ソース行単位のステップ（into / over / out / until）を、命令の意味を辞書で
与える偽の Cortex-M（cpu_fake）と偽の行表で確かめる。"""

import pytest

from cpu_fake import DEMCR, VC_HARDERR, FakeCpu, FakeLines
from fprobe.debug import Debugger
from fprobe.stepping import LineStepper, Runner

# main: 行 10〜18、square: 3〜5（前置きの後 0x202）、lib: 行の情報なし、start: 30〜31、rec: 40〜43（再帰）
PROGRAM = {
    0x100: ("push",), 0x102: ("op",),                       # 10: {
    0x104: ("op",), 0x106: ("op",),                         # 11: a = 1;
    0x108: ("call", 0x200), 0x10C: ("op",),                 # 12: b = square(a);
    0x10E: ("op",),                                         # 13: do { b++;
    0x110: ("op",), 0x112: ("loop", 0x10E, 2),              # 14: } while (...);
    0x114: ("op",),                                         # 15: c = b;
    0x116: ("call", 0x300),                                 # 16: lib();
    0x11A: ("op",),                                         # 17: d = c;
    0x11C: ("pop_pc",),                                     # 18: }
    0x200: ("push",), 0x202: ("op",), 0x204: ("op",), 0x206: ("pop_pc",),
    0x300: ("op",), 0x302: ("ret",),
    0x400: ("call", 0x100), 0x404: ("op",), 0x406: ("spin",),
    0x500: ("op",), 0x502: ("ret",),                        # 割り込みのハンドラ（SysTick）
    0x600: ("push",), 0x602: ("loop", 0x608, 2), 0x604: ("pop_pc",), 0x606: ("op",),
    0x608: ("call", 0x600), 0x60C: ("pop_pc",),
    0x700: ("spin",),                                       # HardFault のハンドラ
    0x800: ("op",), 0x802: ("fault",), 0x804: ("op",), 0x806: ("ret",),
    0x900: ("call", 0xA00), 0x904: ("op",),
    0xA00: ("spin",),
}
FUNCTIONS = [("main", 0x100, 0x20, 0x104), ("square", 0x200, 0x8, 0x202), ("lib", 0x300, 0x4, 0x300),
             ("start", 0x400, 0x8, 0x400), ("SysTick_Handler", 0x500, 0x4, 0x500), ("rec", 0x600, 0x10, 0x602),
             ("HardFault_Handler", 0x700, 0x2, 0x700), ("bad", 0x800, 0x8, 0x800), ("waits", 0x900, 0x8, 0x900),
             ("forever", 0xA00, 0x2, 0xA00)]
ROWS = [(0x100, 10), (0x104, 11), (0x108, 12), (0x10E, 13), (0x110, 14), (0x114, 15), (0x116, 16), (0x11A, 17),
        (0x11C, 18), (0x200, 3), (0x202, 4), (0x206, 5), (0x400, 30), (0x406, 31), (0x500, 50),
        (0x600, 40), (0x602, 41), (0x604, 42), (0x608, 41), (0x60C, 43), (0x700, 60), (0x800, 70), (0x802, 71),
        (0x804, 72), (0x900, 80), (0x904, 81), (0xA00, 90)]


def machine(pc: int = 0x400, **kw) -> tuple[FakeCpu, Debugger]:
    cpu = FakeCpu(PROGRAM, pc, handlers={15: 0x500, 3: 0x700}, **kw)
    return cpu, Debugger(cpu, FakeLines(FUNCTIONS, ROWS))


def at(dbg: Debugger, location: str) -> None:
    """location（関数名か "demo.c:行"）まで走らせて止める（ブレークポイントで）。"""
    dbg.breakpoint(location)
    assert dbg.run_until_halt(1).reason == "breakpoint"
    dbg.remove_breakpoint(location)


def lines(stop) -> tuple:
    return stop.reason, stop.where.function, stop.where.line


def test_step_into_a_call_stops_after_the_callee_prologue():
    cpu, dbg = machine()
    stop = dbg.step_line("into")
    assert lines(stop) == ("step", "main", 11) and stop.pc == 0x104
    assert not cpu.breakpoints                                  # 踏み台のブレークポイントは残さない


def test_step_over_runs_through_the_whole_line_and_over_calls():
    cpu, dbg = machine()
    at(dbg, "main")
    assert lines(dbg.step_line("over")) == ("step", "main", 12)
    stop = dbg.step_line("over")                                # square(a) を踏み越え、行 12 の残りも実行する
    assert lines(stop) == ("step", "main", 13) and stop.pc == 0x10E
    assert not cpu.breakpoints


def test_step_into_enters_a_function_with_line_info():
    cpu, dbg = machine()
    at(dbg, "demo.c:12")
    stop = dbg.step_line("into")
    assert lines(stop) == ("step", "square", 4) and stop.pc == 0x202


def test_step_into_a_function_without_line_info_steps_over_it():
    cpu, dbg = machine()
    at(dbg, "demo.c:16")
    assert lines(dbg.step_line("into")) == ("step", "main", 17)


def test_a_backward_jump_to_another_line_stops_but_until_does_not():
    cpu, dbg = machine()
    at(dbg, "demo.c:14")
    assert lines(dbg.step_line("over")) == ("step", "main", 13)   # ループの先頭へ戻った
    cpu, dbg = machine()
    at(dbg, "demo.c:14")
    assert lines(dbg.step_line("until")) == ("step", "main", 15)  # until は後ろへ戻らない: ループを終えるまで


def test_until_a_location_runs_there():
    cpu, dbg = machine()
    at(dbg, "main")
    stop = dbg.step_line("until", location="demo.c:17")
    assert lines(stop) == ("step", "main", 17) and not cpu.breakpoints


def test_stepping_past_the_end_returns_to_the_caller():
    # GDB と同じく、呼び出し元の呼び出しの行の途中で止まる（戻り値の代入等が残っている）
    cpu, dbg = machine()
    at(dbg, "demo.c:18")
    stop = dbg.step_line("over")
    assert lines(stop) == ("step", "start", 30) and stop.pc == 0x404


def test_step_out_without_cfi_steps_until_the_function_returns():
    cpu, dbg = machine()
    at(dbg, "square")
    stop = dbg.step_line("out")
    assert lines(stop) == ("step", "main", 12) and stop.pc == 0x10C


def test_step_over_a_recursive_call_waits_for_the_same_frame():
    # 戻り先の踏み台に、より深い呼び出し（sp が小さい）で着いたら止まらずに続ける
    cpu, dbg = machine(pc=0x600)
    cpu.regs["lr"] = 0x407                                       # start の中から呼ばれた形
    assert lines(dbg.step_line("over")) == ("step", "rec", 41)   # 前置きの後
    stop = dbg.step_line("over")                                 # 0x602 → 0x608（再帰の呼び出しの行。同じ行 41）
    assert lines(stop) == ("step", "rec", 43) and stop.pc == 0x60C and cpu.regs["sp"] == 0x20001000 - 4


def test_interrupts_are_masked_while_stepping_by_default():
    cpu, dbg = machine()
    at(dbg, "main")
    cpu.pending = 15
    assert lines(dbg.step_line("over")) == ("step", "main", 12)
    assert cpu.step_masks and all(cpu.step_masks) and cpu.pending == 15


def test_an_interrupt_taken_while_stepping_is_stepped_over():
    cpu, dbg = machine()
    at(dbg, "main")
    cpu.pending = 15
    stop = dbg.step_line("over", mask_interrupts=False)
    assert lines(stop) == ("step", "main", 12) and cpu.pending is None and not any(cpu.step_masks)
    assert cpu.ipsr == 0


def test_a_breakpoint_inside_a_stepped_over_call_stops_the_step():
    cpu, dbg = machine()
    at(dbg, "demo.c:12")
    dbg.breakpoint(0x204)
    stop = dbg.step_line("over")
    assert stop.reason == "breakpoint" and stop.pc == 0x204
    assert stop.halt_bits == ["BKPT"]                            # 止まったときの DFSR の立っていたビット（全部）
    assert cpu.breakpoints == {0x204}                            # 利用者のものは残し、踏み台は外す


def test_a_breakpoint_that_asks_to_continue_does_not_stop_the_step():
    cpu, dbg = machine()
    at(dbg, "demo.c:12")
    dbg.breakpoint(0x204)
    hits = []
    stop = dbg.step_line("over", on_breakpoint=lambda: hits.append(cpu.pc) or True)
    assert lines(stop) == ("step", "main", 13) and hits == [0x204]


def test_a_fault_while_stepping_stops_there():
    cpu, dbg = machine(pc=0x802)
    stop = dbg.step_line("over")
    assert stop.reason == "fault" and stop.where.function == "HardFault_Handler"
    cpu, dbg = machine(pc=0x802)
    cpu.write32(DEMCR, VC_HARDERR)                               # ベクタキャッチ: 故障の入口で止まる
    assert dbg.step_line("over").reason == "vector_catch"


def test_a_call_that_never_returns_times_out_and_halts():
    cpu, dbg = machine(pc=0x900)
    stop = dbg.step_line("over", timeout_s=0.05)
    assert stop.reason == "timeout" and stop.where.function == "forever"
    assert cpu.state.name == "HALTED" and not cpu.breakpoints


def test_a_watchpoint_on_an_address_needs_its_size():
    # 番地のウォッチポイントの大きさを 4 と仮定しない（3 回目の点検 ?12）
    _, dbg = machine()
    with pytest.raises(ValueError, match="size"):
        dbg.watchpoint(0x20000000)


def test_step_needs_line_information_and_a_known_mode():
    cpu = FakeCpu(PROGRAM, 0x100)
    with pytest.raises(RuntimeError):
        Debugger(cpu).step_line("over")                          # ELF が無い
    with pytest.raises(ValueError):
        machine()[1].step_line("sideways")


def test_step_from_code_without_line_info_returns_to_the_caller():
    cpu, dbg = machine(pc=0x300)
    cpu.regs["lr"] = 0x11B                                       # main の 16 行目から呼ばれた形
    stop = dbg.step_line("over")
    assert lines(stop) == ("step", "main", 17)


def test_line_stepper_uses_the_call_frame_information_for_step_out():
    # CFI があれば 1 命令ずつではなく戻り先の踏み台まで一気に走る
    cpu = FakeCpu(PROGRAM, 0x204)
    cpu.regs["lr"] = 0x10D
    runner = Runner(cpu, {}, deadline=float("inf"))
    stepper = LineStepper(runner, FakeLines(FUNCTIONS, ROWS), frame_cfa=lambda: cpu.regs["sp"] + 4,
                          caller=lambda: (0x10C, cpu.regs["sp"] + 4))
    cpu.regs["sp"] -= 4
    cpu.write32(cpu.regs["sp"], 0x10D)
    halt = stepper.step("out")
    assert halt.reason == "arrived" and halt.regs.pc == 0x10C and cpu.steps == 0
