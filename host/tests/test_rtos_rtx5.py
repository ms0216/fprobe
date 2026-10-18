"""fprobe.rtos.rtx5（U45）: RTX5 の本物のヘッダの構造体で作った rtx5_shape.elf の、.data の初期値の「止めたときの様子」から。

スレッドの一覧（実行中・準備完了・時間待ち・時間切れの無い待ち・終わり）、待っているもの（オブジェクトの id で見分ける）・
残りのティック（delay_list の差分の和）、スタック（底の印と 0xCC の塗り）、保存された文脈と呼び出し履歴、
カーネルのオブジェクト（ミューテックス・セマフォ・イベントフラグ・メモリプール・メッセージキュー・タイマ）。
"""

from pathlib import Path

import pytest
from rtos_kernel import TaskStackBuilder

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo
from fprobe.rtos import detect, open_rtos
from fprobe.rtos.contention import contention
from fprobe.rtos.cortexm import read_fp_first_context
from fprobe.rtos.model import TaskState
from fprobe.rtos.rtx5 import RTX5
from fprobe.rtos.structs import StructLayout
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "rtx5_shape" / "rtx5_shape.elf"
STACK = 512
NORMAL, ABOVE, BELOW, HIGH, LOW, IDLE, REALTIME7 = 24, 32, 16, 40, 8, 1, 55


@pytest.fixture(scope="module")
def image():
    return DebugInfo(ELF), Symbols(ELF)


def rtx(image, registers=None) -> tuple[RTX5, ImageAccess]:
    info, symbols = image
    access = ImageAccess.from_symbols(symbols, registers)
    return RTX5(info, access, symbols), access


def by_name(items):
    return {x.name: x for x in items}


def test_detect(image):
    info, symbols = image
    assert detect(info) == "rtx5"
    assert isinstance(open_rtos(info, ImageAccess.from_symbols(symbols), symbols), RTX5)


def test_threads_states_and_priorities(image):
    rtos, _ = rtx(image)
    tasks = rtos.tasks()
    assert [(t.name, t.state, t.priority) for t in tasks] == [
        ("main", TaskState.RUNNING, NORMAL), ("logger", TaskState.READY, NORMAL),
        ("osRtxIdleThread", TaskState.READY, IDLE), ("sensor", TaskState.BLOCKED, ABOVE),
        ("sleeper", TaskState.BLOCKED, BELOW), ("ctl", TaskState.BLOCKED, HIGH), ("parked", TaskState.SUSPENDED, LOW),
        ("osRtxTimerThread", TaskState.BLOCKED, REALTIME7), ("gone", TaskState.DELETED, NORMAL)]
    _, symbols = image
    assert tasks[3].handle == symbols.variable("th_sensor").address
    assert all(t.base_priority == t.priority for t in tasks)
    assert rtos.warnings == []


def test_kernel_state(image):
    rtos, _ = rtx(image)
    _, symbols = image
    assert rtos.priority_order == 1                          # RTX5 は大きい方が高い
    assert rtos.scheduler_state() == "running" and rtos.tick_count() == 1000
    assert rtos.current() == symbols.variable("th_main").address
    assert rtos.idle_handles() == {symbols.variable("th_idle").address}
    assert rtos.handle_name(symbols.variable("th_ctl").address) == "ctl"
    assert rtos.handle_priority(symbols.variable("th_ctl").address) == HIGH
    assert rtos.stop_pcs() == [symbols.symbol("osThreadExit").address & ~1]


def test_waits(image):
    rtos, _ = rtx(image)
    _, symbols = image
    t = by_name(rtos.tasks())
    w = t["sensor"].wait
    assert (w.reason, w.kind, w.name, w.object) == ("receive", "semaphore", "sem_data",
                                                    symbols.variable("sem_data").address)
    assert (w.remaining_ticks, w.wake_tick) == (30, 1030)
    w = t["sleeper"].wait
    assert (w.reason, w.remaining_ticks, w.wake_tick, w.object) == ("delay", 50, 1050, None)
    w = t["ctl"].wait
    assert (w.reason, w.kind, w.name, w.holder, w.remaining_ticks) == ("receive", "mutex", "bus_lock", "sleeper", None)
    w = t["osRtxTimerThread"].wait                           # 名前の無いオブジェクトは変数の名前
    assert (w.reason, w.kind, w.name) == ("receive", "message_queue", "timer_mq")
    assert t["parked"].wait.reason == "suspended"
    assert t["main"].wait is None and t["logger"].wait is None


def test_stacks(image):
    rtos, _ = rtx(image)
    _, symbols = image
    s = by_name(rtos.tasks())["sensor"].stack
    base = symbols.variable("sensor_stack").address
    assert (s.base, s.end, s.size, s.pointer) == (base, base + STACK, STACK, base + STACK - 128)
    assert s.fill == "intact" and s.untouched == STACK - 128 and s.peak_used == 128 and not s.overflow
    gone = by_name(rtos.tasks())["gone"]
    assert gone.stack.overflow and "osRtxStackMagicWord" in " ".join(gone.notes)
    assert by_name(rtos.tasks())["main"].stack.pointer is None          # 実行中: レジスタが読めない


def test_running_thread_stack_pointer(image):
    _, symbols = image
    top = symbols.variable("main_stack").address + STACK - 40
    rtos, _ = rtx(image, {"psp": top})
    assert by_name(rtos.tasks())["main"].stack.used == 40


def test_fp_first_context_layout():
    """RTX5 の irq_armv7m.S: SP から [s16〜s31]（拡張フレームのとき）→ r4〜r11 → ハードのフレーム。"""
    memory = ImageAccess([(0x2000_0000, bytes(0x200))])
    at = 0x2000_0000
    for n in range(16):
        memory.write_u32(at + 4 * n, 0x1600 + n)                    # s16〜s31
    for n in range(8):
        memory.write_u32(at + 64 + 4 * n, 0x400 + n)                # r4〜r11
    hw = at + 64 + 32
    for i, v in enumerate([0, 1, 2, 3, 12, 0x0800_0101, 0x0800_0200, 0x0100_0000]):
        memory.write_u32(hw + 4 * i, v)
    ctx = read_fp_first_context(memory.read_memory, at, 0xFFFF_FFED)
    assert ctx.fp and ctx.registers["s16"] == 0x1600 and ctx.registers["s31"] == 0x160F
    assert ctx.registers["r4"] == 0x400 and ctx.registers["r11"] == 0x407 and ctx.registers["pc"] == 0x0800_0200
    assert ctx.sp == hw + 0x68
    plain = read_fp_first_context(memory.read_memory, at, 0xFFFF_FFFD)
    assert not plain.fp and plain.registers["r4"] == 0x1600 and plain.registers["xpsr"] == 0x160F
    assert plain.sp == at + 32 + 32 + 4                                # xpsr の bit 9: 4 バイトの詰め物


def test_backtrace_of_waiting_thread(image):
    """sensor が osSemaphoreAcquire の中で切り替わった: PendSV が r4〜r11 を積み、thread.sp がそれを指す。"""
    info, symbols = image
    rtos, access = rtx(image)
    thread = symbols.variable("th_sensor").address
    sem = symbols.variable("sem_data").address
    b = TaskStackBuilder(symbols, top=0x2000_8000)
    b.memory = access
    b.use_psp(symbols.variable("sensor_stack").address + STACK)
    b.enter("sensor_entry", 0, lr=symbols.symbol("osThreadExit").address | 1)
    pc = b.call("sensor_entry", "osSemaphoreAcquire", sem, 30)
    frame = (b.regs["sp"] - 0x20) & ~7
    words = [b.regs[r] for r in ("r0", "r1", "r2", "r3", "r12", "lr")] + [pc, 0x0100_0000]
    for i, w in enumerate(words):
        access.write_u32(frame + 4 * i, w)
    saved = frame - 32
    for n in range(8):
        access.write_u32(saved + 4 * n, b.regs[f"r{n + 4}"])
    access.write_u32(thread + StructLayout.of(info, "osRtxThread_t").offset("sp"), saved)
    ctx = rtos.saved_context("sensor")
    assert ctx.registers["pc"] == pc and ctx.sp == b.regs["sp"] and not ctx.fp
    frames = rtos.backtrace("sensor", variables=True)
    assert [f["function"] for f in frames] == ["osSemaphoreAcquire", "sensor_entry"]
    assert {a["name"]: a["value"] for a in frames[0]["arguments"]}["semaphore_id"]["pointer"] == sem
    assert rtos.saved_context("main") is None


def test_snapshot_and_current_variable(image):
    rtos, access = rtx(image)
    snap = rtos.snapshot()
    assert snap["rtos"] == "RTX5" and snap["current"] == "main" and len(snap["tasks"]) == 9
    assert snap["scheduler"] == "running" and snap["tick"] == 1000 and snap["version"] == "RTX V5.5.0"
    current = int.from_bytes(access.read_memory(rtos.current_address(), 4), "little")
    assert current == rtos.current()
    assert rtos.saved_sp_address(current) == current + StructLayout.of(rtos.info, "osRtxThread_t").offset("sp")


def test_broken_ready_list_is_reported(image):
    rtos, access = rtx(image)
    _, symbols = image
    logger = symbols.variable("th_logger").address
    access.write_u32(logger + rtos.thread.offset("thread_next"), logger)          # 自分を指す
    names = [t.name for t in rtos.tasks()]
    assert names.count("logger") == 1 and rtos.warnings


# --- カーネルのオブジェクト ---------------------------------------------------------------------------------

def test_objects(image):
    rtos, _ = rtx(image)
    _, symbols = image
    objects = by_name(rtos.objects())
    assert {n: o.kind for n, o in objects.items()} == {
        "bus_lock": "mutex", "sem_data": "semaphore", "sys_events": "event_flags", "buf_pool": "memory_pool",
        "cmd_mq": "message_queue", "timer_mq": "message_queue", "watchdog": "timer", "blink": "timer",
        "oneshot": "timer"}
    assert objects["sem_data"].source == "kernel" and objects["timer_mq"].source == "variable"
    assert objects["watchdog"].aliases == ["tmr_watchdog"]
    assert objects["sem_data"].handle == symbols.variable("sem_data").address


def test_object_fields_and_waiters(image):
    rtos, _ = rtx(image)
    objects = by_name(rtos.objects())
    sem = objects["sem_data"]
    assert sem.fields == {"count": 0, "max": 1}
    assert [(w.task, w.reason, w.priority) for w in sem.waiters] == [("sensor", "receive", ABOVE)]
    assert objects["sys_events"].fields == {"flags": 5}
    assert objects["buf_pool"].fields == {"block_size": 16, "max_blocks": 8, "used_blocks": 3, "free_blocks": 5}
    mq = objects["cmd_mq"].fields
    assert (mq["msg_size"], mq["count"], mq["max_msgs"], mq["free"]) == (8, 2, 4, 2)
    assert [w.task for w in objects["timer_mq"].waiters] == ["osRtxTimerThread"]


def test_timers(image):
    rtos, _ = rtx(image)
    objects = by_name(rtos.objects(kinds=["timer"]))
    assert objects["watchdog"].fields == {"active": True, "periodic": True, "period_ticks": 500,
                                          "remaining_ticks": 10, "callback": "blink_cb", "argument": 0}
    assert objects["blink"].fields["remaining_ticks"] == 30 and objects["blink"].fields["argument"] == 0x1234
    assert objects["oneshot"].fields["active"] is False and objects["oneshot"].fields["periodic"] is False
    assert "remaining_ticks" not in objects["oneshot"].fields


def test_mutex_without_inheritance_is_an_inversion(image):
    rtos, _ = rtx(image)
    tasks = rtos.tasks()
    objects = rtos.objects(tasks=tasks)
    mutex = by_name(objects)["bus_lock"]
    assert mutex.fields["holder"] == {"task": "sleeper", "handle": mutex.holder, "priority": BELOW,
                                      "base_priority": BELOW, "inherited": False}
    assert mutex.fields["lock_count"] == 1 and not mutex.fields["available"] and mutex.fields["recursive"]
    (inv,) = contention(tasks, objects, rtos.priority_order)["inversions"]
    assert inv["waiter"]["task"] == "ctl" and inv["holder"]["task"] == "sleeper"
    assert [i["task"] for i in inv["interferers"]] == ["main", "logger"]


def test_mutex_words_for_the_sampler(image):
    rtos, access = rtx(image)
    _, symbols = image
    mutex = by_name(rtos.objects())["bus_lock"]
    owner, head, delta = rtos.mutex_words(mutex)
    assert int.from_bytes(access.read_memory(owner, 4), "little") == symbols.variable("th_sleeper").address
    assert int.from_bytes(access.read_memory(head, 4), "little") - delta == symbols.variable("th_ctl").address
    assert rtos.mutex_empty_head(mutex) == 0
    assert rtos.mutex_words(by_name(rtos.objects())["sem_data"]) is None
