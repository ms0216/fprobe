"""fprobe.rtos.nuttx（U45）: NuttX の本物のヘッダの構造体で作った nuttx_shape.elf の、.data の初期値の「止めたときの様子」から。

タスクの一覧（g_pidhash）、状態（enum tstate_e の名前を DWARF から）と待っているもの（waitobj。セマフォ・ミューテックスの
持ち主の TID・メッセージキュー・合図）・残りのティック（waitdog の expired）、スタック（0xdeadbeef の塗りと壊れた底）、
保存された文脈（xcp.regs の配列。FPU の枠の有無を REG_R13 から）と呼び出し履歴、カーネルのオブジェクト。
"""

from pathlib import Path

import pytest
from rtos_kernel import TaskStackBuilder

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo
from fprobe.rtos import detect, open_rtos
from fprobe.rtos.contention import contention
from fprobe.rtos.model import TaskState
from fprobe.rtos.nuttx import NuttX
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "nuttx_shape" / "nuttx_shape.elf"
STACK = 512
CONTEXT = 216                                     # XCPTCONTEXT_SIZE（CONFIG_ARCH_FPU）
REG_R13, REG_R4, REG_EXC_RETURN, SW_FPU = 0, 2, 11, 12          # arch/armv7-m/irq.h


@pytest.fixture(scope="module")
def image():
    return DebugInfo(ELF), Symbols(ELF)


def nx(image, registers=None) -> tuple[NuttX, ImageAccess]:
    info, symbols = image
    access = ImageAccess.from_symbols(symbols, registers)
    return NuttX(info, access, symbols), access


def by_name(items):
    return {x.name: x for x in items}


def address(image, name: str) -> int:
    return image[1].variable(name).address


def test_detect(image):
    info, symbols = image
    assert detect(info) == "nuttx"
    assert isinstance(open_rtos(info, ImageAccess.from_symbols(symbols), symbols), NuttX)


def test_tasks_states_and_priorities(image):
    rtos, _ = nx(image)
    tasks = rtos.tasks()
    assert [(t.number, t.name, t.state, t.priority, t.base_priority) for t in tasks] == [
        (0, "Idle_Task", TaskState.READY, 0, 0), (1, "main", TaskState.RUNNING, 100, 100),
        (2, "logger", TaskState.READY, 100, 100), (3, "sensor", TaskState.BLOCKED, 150, 150),
        (4, "sleeper", TaskState.BLOCKED, 200, 50), (5, "ctl", TaskState.BLOCKED, 200, 200),
        (6, "consumer", TaskState.BLOCKED, 120, 120), (7, "producer", TaskState.BLOCKED, 110, 110),
        (8, "waiter", TaskState.BLOCKED, 90, 90), (9, "stopped", TaskState.SUSPENDED, 80, 80)]
    assert tasks[3].handle == address(image, "th_sensor")
    assert rtos.warnings == []


def test_kernel_state(image):
    rtos, access = nx(image)
    assert rtos.priority_order == 1                           # POSIX: 大きい方が高い
    assert rtos.port == "armv7m"
    assert rtos.scheduler_state() == "running" and rtos.tick_count() == 1000
    assert rtos.version() is None
    assert rtos.current() == address(image, "th_main")
    assert rtos.current_address() == address(image, "g_running_tasks")
    assert rtos.idle_handles() == {address(image, "g_idletcb")}
    assert rtos.handle_name(address(image, "th_ctl")) == "ctl"
    assert rtos.handle_priority(address(image, "th_sleeper")) == 50                # 継承の前
    access.write_u32(address(image, "g_nx_initstate"), 3)                            # OSINIT_MEMORY
    assert rtos.scheduler_state() == "not_started"
    access.write_u32(address(image, "g_nx_initstate"), 5)                            # OSINIT_IDLELOOP
    lockcount = address(image, "th_main") + rtos.tcb.offset("lockcount")
    access.write_memory(lockcount, (1).to_bytes(2, "little"))
    assert rtos.scheduler_state() == "suspended"


def test_waits(image):
    rtos, _ = nx(image)
    t = by_name(rtos.tasks())
    w = t["sensor"].wait
    assert (w.reason, w.kind, w.name, w.object) == ("receive", "semaphore", "sem_data", address(image, "sem_data"))
    assert (w.remaining_ticks, w.wake_tick) == (10, 1010)
    w = t["sleeper"].wait                                     # usleep: TSTATE_WAIT_SIG で合図を待たない
    assert (w.reason, w.kind, w.object, w.remaining_ticks, w.wake_tick) == ("delay", None, None, 50, 1050)
    w = t["ctl"].wait                                         # 持ち主は val.mholder の TID
    assert (w.reason, w.kind, w.name, w.holder, w.remaining_ticks) == ("receive", "mutex", "bus_lock", "sleeper", None)
    w = t["consumer"].wait
    assert (w.reason, w.kind, w.name) == ("receive", "message_queue", "cmd_mq")
    w = t["producer"].wait
    assert (w.reason, w.kind, w.name, w.remaining_ticks) == ("send", "message_queue", "full_mq", 25)
    w = t["waiter"].wait                                      # sigwaitinfo(SIGUSR1)
    assert (w.reason, w.kind, w.name, w.object) == ("event", "signal", "SIGUSR1", None)
    assert t["stopped"].wait.reason == "suspended" and t["stopped"].notes
    assert t["main"].wait is None and t["logger"].wait is None and t["Idle_Task"].wait is None


def test_stacks(image):
    rtos, _ = nx(image)
    tasks = by_name(rtos.tasks())
    s = tasks["sensor"].stack
    base = address(image, "sensor_stack")
    assert (s.base, s.end, s.size, s.pointer) == (base, base + STACK, STACK, base + STACK - CONTEXT)
    assert s.fill == "intact" and s.untouched == STACK - 256 and s.peak_used == 256 and not s.overflow
    stopped = tasks["stopped"]
    assert not stopped.stack.overflow and stopped.stack.fill == "none" and stopped.stack.fill_above and stopped.notes
    assert tasks["main"].stack.pointer is None                # 実行中: レジスタが読めない


def test_running_task_stack_pointer(image):
    """実行中のタスクの SP: psp がスタックの中ならそれ（割り込みのスタックを使う設定）、無ければ sp（MSP で動く設定）。"""
    top = address(image, "main_stack") + STACK - 40
    rtos, _ = nx(image, {"psp": 0x2000_7F00, "sp": top})
    assert by_name(rtos.tasks())["main"].stack.used == 40
    rtos, _ = nx(image, {"psp": top - 8, "sp": 0x2000_7F00})
    assert by_name(rtos.tasks())["main"].stack.used == 48


def test_initial_context(image):
    """up_initial_state の形の文脈: PC = start（nxtask_start）、SP = スタックの上、EXC_RETURN は拡張フレーム。"""
    rtos, _ = nx(image)
    ctx = rtos.saved_context("logger")
    top = address(image, "logger_stack") + STACK
    assert ctx.top == top - CONTEXT and ctx.fp and ctx.exc_return == 0xFFFF_FFE9
    assert ctx.registers["pc"] == image[1].symbol("nxtask_start").address & ~1 and ctx.sp == top
    assert rtos.saved_context("main") is None


def test_context_without_fpu_slots(image):
    """REG_R13 が regs + 80（CONFIG_ARCH_FPU 無し）なら s16〜s31 の枠は無く、ハードのフレームは regs + 48。"""
    rtos, access = nx(image)
    regs = address(image, "consumer_stack") + 64
    for n in range(8):
        access.write_u32(regs + 4 * (REG_R4 + n), 0x400 + n)
    access.write_u32(regs + 4 * REG_R13, regs + 80)
    access.write_u32(regs + 4 * REG_EXC_RETURN, 0xFFFF_FFF9)
    for i, v in enumerate([0, 1, 2, 3, 12, 0x0800_0101, 0x0800_0200, 0x0100_0000]):
        access.write_u32(regs + 48 + 4 * i, v)
    access.write_u32(address(image, "th_consumer") + rtos.tcb.offset("xcp.regs"), regs)
    ctx = rtos.saved_context("consumer")
    assert not ctx.fp and ctx.registers["r11"] == 0x407 and ctx.registers["pc"] == 0x0800_0200
    assert ctx.sp == regs + 80


def test_backtrace_of_waiting_task(image):
    """sensor が nxsem_wait の中で切り替わった: arm_exception.S が R13・BASEPRI・r4〜r11・CONTROL・EXC_RETURN と s16〜s31 の
    枠を積み、xcp.regs がそれを指す。標準のフレーム（EXC_RETURN 0xFFFFFFF9）でも枠はある（subne）。"""
    info, symbols = image
    rtos, access = nx(image)
    sem = address(image, "sem_data")
    b = TaskStackBuilder(symbols, top=0x2000_8000)
    b.memory = access
    b.use_psp(address(image, "sensor_stack") + STACK)
    b.enter("nxtask_start", lr=0)
    start = symbols.symbol("nxtask_start")
    site = next(i for i in b._code(start.address, start.size) if i.mnemonic == "blx")
    b.enter("sensor_main", 0, lr=(site.address + site.size) | 1)
    pc = b.call("sensor_main", "nxsem_wait", sem)
    frame = (b.regs["sp"] - 0x20) & ~7
    words = [b.regs[r] for r in ("r0", "r1", "r2", "r3", "r12", "lr")] + [pc, 0x0100_0000]
    for i, w in enumerate(words):
        access.write_u32(frame + 4 * i, w)
    regs = frame - 4 * (SW_FPU + 16)
    access.write_u32(regs + 4 * REG_R13, regs + CONTEXT)                 # HW_XCPT_SIZE は FPU の枠を含む定数
    for n in range(8):
        access.write_u32(regs + 4 * (REG_R4 + n), b.regs[f"r{n + 4}"])
    access.write_u32(regs + 4 * REG_EXC_RETURN, 0xFFFF_FFF9)
    access.write_u32(address(image, "th_sensor") + rtos.tcb.offset("xcp.regs"), regs)
    ctx = rtos.saved_context("sensor")
    assert ctx.registers["pc"] == pc and ctx.sp == b.regs["sp"] and not ctx.fp
    frames = rtos.backtrace("sensor", variables=True)
    assert [f["function"] for f in frames] == ["nxsem_wait", "sensor_main", "nxtask_start"]
    assert {a["name"]: a["value"] for a in frames[0]["arguments"]}["sem"]["pointer"] == sem


def test_snapshot_and_sampler_words(image):
    rtos, access = nx(image)
    snap = rtos.snapshot()
    assert snap["rtos"] == "NuttX" and snap["current"] == "main" and len(snap["tasks"]) == 10
    assert snap["scheduler"] == "running" and snap["tick"] == 1000 and snap["port"] == "armv7m"
    current = int.from_bytes(access.read_memory(rtos.current_address(), 4), "little")
    assert current == rtos.current()
    assert rtos.saved_sp_address(current) == current + rtos.tcb.offset("xcp.regs")


def test_misplaced_pidhash_entry_is_reported(image):
    rtos, access = nx(image)
    table = address(image, "pidhash_table")
    access.write_u32(table + 4 * 12, address(image, "th_logger"))       # PID 2 は位置 2 のはず
    names = [t.name for t in rtos.tasks()]
    assert names.count("logger") == 1 and any("logger" in w or "PID 2" in w for w in rtos.warnings)


# --- カーネルのオブジェクト ---------------------------------------------------------------------------------

def test_objects(image):
    rtos, _ = nx(image)
    objects = by_name(rtos.objects())
    assert {n: o.kind for n, o in objects.items()} == {
        "sem_data": "semaphore", "bus_lock": "mutex", "idle_lock": "mutex", "cmd_mq": "message_queue",
        "full_mq": "message_queue"}
    assert all(o.source == "variable" for o in objects.values())
    assert objects["sem_data"].fields == {"count": 0}
    assert [(w.task, w.reason, w.priority) for w in objects["sem_data"].waiters] == [("sensor", "receive", 150)]
    assert objects["idle_lock"].fields == {"available": True, "holder": None, "priority_inherit": True}
    assert objects["cmd_mq"].fields == {"count": 0, "max_msgs": 8, "msg_size": 16}
    assert [(w.task, w.reason) for w in objects["cmd_mq"].waiters] == [("consumer", "receive")]
    assert objects["full_mq"].fields == {"count": 4, "max_msgs": 4, "msg_size": 8}
    assert [(w.task, w.reason) for w in objects["full_mq"].waiters] == [("producer", "send")]
    assert rtos.warnings == []


def test_mutex_inheritance(image):
    rtos, _ = nx(image)
    tasks = rtos.tasks()
    objects = rtos.objects(tasks=tasks)
    mutex = by_name(objects)["bus_lock"]
    assert mutex.fields == {"available": False, "priority_inherit": True,
                            "holder": {"task": "sleeper", "handle": address(image, "th_sleeper"), "pid": 4,
                                       "priority": 200, "base_priority": 50, "inherited": True}}
    assert [w.task for w in mutex.waiters] == ["ctl"]
    (inv,) = contention(tasks, objects, rtos.priority_order)["inversions"]
    assert inv["waiter"]["task"] == "ctl" and inv["inherited"] and inv["interferers"] == []
    assert rtos.mutex_words(mutex) is None                    # 持ち主は TID（TCB の番地ではない）


def test_baseline_context_is_refused(image):
    """ARMv6-M の port（arch/arm/include/armv6-m/irq.h）は配列の並びが違う: 嘘の文脈を出さない。"""
    rtos, _ = nx(image)
    rtos.port = "armv6m"
    with pytest.raises(ValueError):
        rtos.saved_context("logger")
