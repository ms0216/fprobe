"""fprobe.rtos.ucos3（U45）: µC/OS-III の本物のヘッダの構造体で作った ucos3_shape.elf（OS_CFG_DBG_EN = 1）と
ucos3_shape_nodbg.elf（= 0）の、.data の初期値の「止めたときの様子」から。

タスクの一覧（DbgNextPtr の列。無ければ変数・準備完了・時間のリストから集める）、状態と待っているもの（PendOn・
PendObjPtr）・残りのティック（OSTickList の差分の和）、スタック（OS_OPT_TASK_STK_CLR の 0 の塗りと壊れた底）、
保存された文脈（ARMv7-M の PendSV の積み方）と呼び出し履歴、カーネルのオブジェクト（sem・mutex・queue・flag・
memory_pool・timer・cond）。
"""

from pathlib import Path

import pytest
from rtos_kernel import TaskStackBuilder

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo
from fprobe.rtos import detect, open_rtos
from fprobe.rtos.contention import contention
from fprobe.rtos.model import TaskState
from fprobe.rtos.structs import StructLayout
from fprobe.rtos.ucos3 import UCOS3
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "ucos3_shape"
ELF = FIRMWARE / "ucos3_shape.elf"
NODBG_ELF = FIRMWARE / "ucos3_shape_nodbg.elf"
STACK = 512                                                  # 128 語
TMR_TASK, IDLE_TASK = "uC/OS-III Timer Task", "uC/OS-III Idle Task"


@pytest.fixture(scope="module")
def image():
    return DebugInfo(ELF), Symbols(ELF)


@pytest.fixture(scope="module")
def nodbg():
    return DebugInfo(NODBG_ELF), Symbols(NODBG_ELF)


def os3(image, registers=None, **options) -> tuple[UCOS3, ImageAccess]:
    info, symbols = image
    access = ImageAccess.from_symbols(symbols, registers)
    return UCOS3(info, access, symbols, **options), access


def by_name(items):
    return {x.name: x for x in items}


def address(image, name: str) -> int:
    return image[1].variable(name).address


def test_detect(image, nodbg):
    for img in (image, nodbg):
        info, symbols = img
        assert detect(info) == "ucos3"
        assert isinstance(open_rtos(info, ImageAccess.from_symbols(symbols), symbols), UCOS3)


def test_tasks_states_and_priorities(image):
    rtos, _ = os3(image)
    tasks = rtos.tasks()
    assert [(t.name, t.state, t.priority, t.base_priority) for t in tasks] == [
        ("main", TaskState.RUNNING, 10, 10), ("logger", TaskState.READY, 10, 10), ("sensor", TaskState.BLOCKED, 5, 5),
        ("sleeper", TaskState.BLOCKED, 3, 20), ("ctl", TaskState.BLOCKED, 3, 3),
        ("parked", TaskState.SUSPENDED, 25, 25), ("consumer", TaskState.BLOCKED, 9, 9),
        ("waiter", TaskState.BLOCKED, 12, 12), ("notified", TaskState.BLOCKED, 14, 14),
        ("frozen", TaskState.SUSPENDED, 16, 16), (TMR_TASK, TaskState.BLOCKED, 61, 61),
        (IDLE_TASK, TaskState.READY, 63, 63)]
    assert tasks[2].handle == address(image, "AppTaskSensorTCB")
    assert rtos.warnings == []


def test_kernel_state(image):
    rtos, access = os3(image)
    assert rtos.priority_order == -1                          # 0 が最高
    assert rtos.port == "armv7m"
    assert rtos.scheduler_state() == "running" and rtos.tick_count() == 1000
    assert rtos.version() == "3.08.02"                        # OSDbg_VersionNbr = 30802
    assert rtos.current() == address(image, "AppTaskMainTCB")
    assert rtos.current_address() == address(image, "OSTCBCurPtr")
    assert rtos.idle_handles() == {address(image, "OSIdleTaskTCB")}
    assert rtos.stop_pcs() == [image[1].symbol("OS_TaskReturn").address & ~1]
    assert rtos.handle_name(address(image, "AppTaskCtlTCB")) == "ctl"
    assert rtos.handle_priority(address(image, "AppTaskSleeperTCB")) == 20             # 継承の前
    access.write_memory(address(image, "OSRunning"), b"\0")
    assert rtos.scheduler_state() == "not_started"
    access.write_memory(address(image, "OSRunning"), b"\1")
    access.write_memory(address(image, "OSSchedLockNestingCtr"), b"\1")
    assert rtos.scheduler_state() == "suspended"


def test_waits(image):
    rtos, _ = os3(image)
    t = by_name(rtos.tasks())
    w = t["sensor"].wait
    assert (w.reason, w.kind, w.name, w.object) == ("receive", "semaphore", "sem_data", address(image, "sem_data"))
    assert (w.remaining_ticks, w.wake_tick) == (10, 1010)                 # 差分の列の先頭
    w = t[TMR_TASK].wait                                      # 名前の無い条件変数はシンボルの名前
    assert (w.reason, w.kind, w.name, w.remaining_ticks) == ("pend", "cond", "OSTmrCond", 15)
    w = t["sleeper"].wait                                     # 差分の和: 10 + 5 + 15 + 20
    assert (w.reason, w.kind, w.object, w.remaining_ticks, w.wake_tick) == ("delay", None, None, 50, 1050)
    w = t["ctl"].wait
    assert (w.reason, w.kind, w.name, w.holder, w.remaining_ticks) == ("receive", "mutex", "bus_lock", "sleeper", None)
    w = t["consumer"].wait
    assert (w.reason, w.kind, w.name) == ("receive", "queue", "cmd_q")
    w = t["waiter"].wait
    assert (w.reason, w.kind, w.name) == ("event", "event_flags", "sys_events")
    w = t["notified"].wait                                    # タスクの組み込みのセマフォ（待つオブジェクトは無い）
    assert (w.reason, w.kind, w.object) == ("notification", "task_semaphore", None)
    assert t["parked"].wait.reason == "suspended" and any("2" in n for n in t["parked"].notes)
    frozen = t["frozen"]                                      # 遅延の途中で止められた
    assert (frozen.wait.reason, frozen.wait.remaining_ticks) == ("delay", 30) and frozen.notes
    assert t["main"].wait is None and t["logger"].wait is None and t[IDLE_TASK].wait is None


def test_stacks(image):
    rtos, _ = os3(image)
    tasks = by_name(rtos.tasks())
    s = tasks["sensor"].stack
    base = address(image, "sensor_stk")
    assert (s.base, s.end, s.size, s.pointer) == (base, base + STACK, STACK, base + STACK - 128)
    assert s.fill == "intact" and s.untouched == STACK - 128 and s.peak_used == 128 and not s.overflow
    logger = tasks["logger"].stack                            # OS_OPT_TASK_STK_CLR 無し: 塗りが無い
    assert logger.fill == "none" and logger.untouched is None and not logger.overflow
    parked = tasks["parked"]
    assert parked.stack.overflow and parked.stack.fill == "none" and parked.notes
    assert tasks["main"].stack.pointer is None                # 実行中: レジスタが読めない


def test_running_task_stack_pointer(image):
    top = address(image, "main_stk") + STACK - 40
    rtos, _ = os3(image, {"psp": top})
    assert by_name(rtos.tasks())["main"].stack.used == 40


def test_backtrace_of_waiting_task(image):
    """sensor が OSSemPend の中で切り替わった: PendSV（ARMv7-M の os_cpu_a.S）が r4〜r11 と EXC_RETURN を積み、
    StkPtr がそれを指す。タスクの関数の戻り先は OS_TaskReturn（OSTaskStkInit が LR に置く）で、そこで止まる。"""
    info, symbols = image
    rtos, access = os3(image)
    tcb = address(image, "AppTaskSensorTCB")
    sem = address(image, "sem_data")
    b = TaskStackBuilder(symbols, top=0x2000_8000)
    b.memory = access
    b.use_psp(address(image, "sensor_stk") + STACK)
    b.enter("AppTaskSensor", 0, lr=symbols.symbol("OS_TaskReturn").address | 1)
    pc = b.call("AppTaskSensor", "OSSemPend", sem, 10)
    frame = (b.regs["sp"] - 0x20) & ~7
    words = [b.regs[r] for r in ("r0", "r1", "r2", "r3", "r12", "lr")] + [pc, 0x0100_0000]
    for i, w in enumerate(words):
        access.write_u32(frame + 4 * i, w)
    saved = frame - 36
    for n in range(8):
        access.write_u32(saved + 4 * n, b.regs[f"r{n + 4}"])
    access.write_u32(saved + 32, 0xFFFF_FFFD)
    access.write_u32(tcb + StructLayout.of(info, "OS_TCB").offset("StkPtr"), saved)
    ctx = rtos.saved_context("sensor")
    assert ctx.registers["pc"] == pc and ctx.sp == b.regs["sp"] and not ctx.fp
    frames = rtos.backtrace("sensor", variables=True)
    assert [f["function"] for f in frames] == ["OSSemPend", "AppTaskSensor"]
    assert {a["name"]: a["value"] for a in frames[0]["arguments"]}["p_sem"]["pointer"] == sem
    assert rtos.saved_context("main") is None


def test_fp_context(image):
    """EXC_RETURN の bit 4 = 0（FPU を使ったタスク）: r4〜r11、EXC_RETURN の上に s16〜s31 がある。"""
    info, _ = image
    rtos, access = os3(image)
    tcb = address(image, "AppTaskCtlTCB")
    saved = address(image, "ctl_stk") + 64
    for n in range(8):
        access.write_u32(saved + 4 * n, 0x400 + n)
    access.write_u32(saved + 32, 0xFFFF_FFED)
    for n in range(16):
        access.write_u32(saved + 36 + 4 * n, 0x1600 + n)
    frame = saved + 36 + 64
    for i, v in enumerate([0, 1, 2, 3, 12, 0x0800_0101, 0x0800_0200, 0x0100_0000]):
        access.write_u32(frame + 4 * i, v)
    access.write_u32(tcb + StructLayout.of(info, "OS_TCB").offset("StkPtr"), saved)
    ctx = rtos.saved_context("ctl")
    assert ctx.fp and ctx.registers["s16"] == 0x1600 and ctx.registers["r11"] == 0x407
    assert ctx.registers["pc"] == 0x0800_0200 and ctx.sp == frame + 0x68


def test_snapshot_and_sampler_words(image):
    rtos, access = os3(image)
    snap = rtos.snapshot()
    assert snap["rtos"] == "uC/OS-III" and snap["current"] == "main" and len(snap["tasks"]) == 12
    assert snap["scheduler"] == "running" and snap["tick"] == 1000 and snap["port"] == "armv7m"
    assert snap["version"] == "3.08.02"
    current = int.from_bytes(access.read_memory(rtos.current_address(), 4), "little")
    assert current == rtos.current()
    assert rtos.saved_sp_address(current) == current + StructLayout.of(rtos.info, "OS_TCB").offset("StkPtr")


def test_broken_debug_list_is_reported(image):
    rtos, access = os3(image)
    logger = address(image, "AppTaskLoggerTCB")
    access.write_u32(logger + rtos.tcb.offset("DbgNextPtr"), logger)                 # 自分を指す
    names = [t.name for t in rtos.tasks()]
    assert names == ["main", "logger"] and rtos.warnings


def test_task_count_mismatch_is_reported(image):
    rtos, access = os3(image)
    access.write_memory(address(image, "OSTaskQty"), (13).to_bytes(2, "little"))
    assert len(rtos.tasks()) == 12 and any("数が合わない" in w for w in rtos.warnings)


# --- カーネルのオブジェクト ---------------------------------------------------------------------------------

def test_objects(image):
    rtos, _ = os3(image)
    objects = by_name(rtos.objects())
    assert {n: o.kind for n, o in objects.items()} == {
        "sem_data": "semaphore", "bus_lock": "mutex", "OS Tmr Mutex": "mutex", "OSTmrCond": "cond",
        "cmd_q": "queue", "reply_q": "queue", "sys_events": "event_flags", "buf_mem": "memory_pool",
        "watchdog": "timer", "blink": "timer", "oneshot": "timer"}
    assert objects["sem_data"].source == "kernel" and objects["reply_q"].source == "variable"
    assert objects["OS Tmr Mutex"].aliases == ["OSTmrMutex"]
    assert objects["watchdog"].aliases == ["tmr_watchdog"]
    assert objects["sem_data"].handle == address(image, "sem_data")
    assert rtos.warnings == []


def test_object_fields_and_waiters(image):
    rtos, _ = os3(image)
    objects = by_name(rtos.objects())
    sem = objects["sem_data"]
    assert sem.fields == {"count": 0}
    assert [(w.task, w.reason, w.priority) for w in sem.waiters] == [("sensor", "receive", 5)]
    assert objects["cmd_q"].fields == {"count": 0, "max_msgs": 8, "peak": 5}
    assert [(w.task, w.reason) for w in objects["cmd_q"].waiters] == [("consumer", "receive")]
    assert objects["reply_q"].fields == {"count": 3, "max_msgs": 4, "peak": 4} and objects["reply_q"].waiters == []
    events = objects["sys_events"]
    assert events.fields == {"flags": 1}
    assert [(w.task, w.reason, w.detail) for w in events.waiters] == [
        ("waiter", "event", {"flags": 6, "all": True, "clear": True})]
    assert objects["buf_mem"].fields == {"block_size": 32, "max_blocks": 8, "used_blocks": 6, "free_blocks": 2}
    cond = objects["OSTmrCond"]
    assert cond.fields == {"mutex": "OS Tmr Mutex"} and [w.task for w in cond.waiters] == [TMR_TASK]
    assert objects["OS Tmr Mutex"].fields == {"available": True, "holder": None, "lock_count": 0,
                                              "priority_inherit": True}


def test_timers(image):
    """OSTmrListPtr の差分の和から、時間のタスクがまだ引いていない経過（OSTickCtr − OSTmrTaskTickBase）を引く。"""
    rtos, _ = os3(image)
    objects = by_name(rtos.objects(kinds=["timer"]))
    assert objects["watchdog"].fields == {"state": "running", "active": True, "periodic": True, "period_ticks": 500,
                                          "delay_ticks": 0, "remaining_ticks": 5, "callback": "blink_cb",
                                          "argument": 0}
    assert objects["blink"].fields["remaining_ticks"] == 38 and objects["blink"].fields["argument"] == 0x1234
    oneshot = objects["oneshot"].fields
    assert oneshot["state"] == "stopped" and oneshot["active"] is False and oneshot["periodic"] is False
    assert oneshot["delay_ticks"] == 50 and "remaining_ticks" not in oneshot


def test_mutex_inheritance(image):
    """µC/OS-III のミューテックスは必ず継承する: 持ち主の sleeper は待つ ctl の優先度（3）で動いている。"""
    rtos, _ = os3(image)
    tasks = rtos.tasks()
    objects = rtos.objects(tasks=tasks)
    mutex = by_name(objects)["bus_lock"]
    assert mutex.fields["holder"] == {"task": "sleeper", "handle": mutex.holder, "priority": 3, "base_priority": 20,
                                      "inherited": True}
    assert mutex.fields["lock_count"] == 1 and not mutex.fields["available"] and mutex.fields["priority_inherit"]
    (inv,) = contention(tasks, objects, rtos.priority_order)["inversions"]
    assert inv["waiter"]["task"] == "ctl" and inv["inherited"] and inv["interferers"] == []


def test_mutex_words_for_the_sampler(image):
    rtos, access = os3(image)
    objects = by_name(rtos.objects())
    owner, head, delta = rtos.mutex_words(objects["bus_lock"])
    assert int.from_bytes(access.read_memory(owner, 4), "little") == address(image, "AppTaskSleeperTCB")
    assert int.from_bytes(access.read_memory(head, 4), "little") - delta == address(image, "AppTaskCtlTCB")
    assert rtos.mutex_empty_head(objects["bus_lock"]) == 0
    assert rtos.mutex_words(objects["sem_data"]) is None


# --- OS_CFG_DBG_EN = 0（一覧も名前も無い） -------------------------------------------------------------------

def test_without_debug_lists_tasks_are_collected(nodbg):
    """一覧（OSTaskDbgListPtr）が無い: OS_TCB の変数・準備完了の列・時間の列から集め、名前は変数の名前。"""
    rtos, _ = os3(nodbg)
    tasks = by_name(rtos.tasks())
    assert {n: t.state for n, t in tasks.items()} == {
        "AppTaskMainTCB": TaskState.RUNNING, "AppTaskLoggerTCB": TaskState.READY,
        "AppTaskSensorTCB": TaskState.BLOCKED, "AppTaskSleeperTCB": TaskState.BLOCKED,
        "AppTaskCtlTCB": TaskState.BLOCKED, "AppTaskParkedTCB": TaskState.SUSPENDED,
        "AppTaskConsumerTCB": TaskState.BLOCKED, "AppTaskWaiterTCB": TaskState.BLOCKED,
        "AppTaskNotifiedTCB": TaskState.BLOCKED, "AppTaskFrozenTCB": TaskState.SUSPENDED,
        "OSTmrTaskTCB": TaskState.BLOCKED, "OSIdleTaskTCB": TaskState.READY}
    assert any("OS_CFG_DBG_EN" in w for w in rtos.warnings)
    assert rtos.version() is None
    w = tasks["AppTaskSleeperTCB"].wait
    assert (w.reason, w.remaining_ticks) == ("delay", 50)
    w = tasks["AppTaskCtlTCB"].wait
    assert (w.kind, w.name, w.holder) == ("mutex", "bus_lock", "AppTaskSleeperTCB")
    assert tasks["AppTaskSensorTCB"].stack.untouched == STACK - 128


def test_without_debug_lists_objects_come_from_variables(nodbg):
    rtos, _ = os3(nodbg)
    objects = by_name(rtos.objects())
    assert {n: o.kind for n, o in objects.items()} == {
        "sem_data": "semaphore", "bus_lock": "mutex", "OSTmrMutex": "mutex", "OSTmrCond": "cond",
        "cmd_q": "queue", "reply_q": "queue", "sys_events": "event_flags", "buf_mem": "memory_pool",
        "tmr_watchdog": "timer", "tmr_blink": "timer", "tmr_oneshot": "timer"}
    assert all(o.source == "variable" for o in objects.values())
    assert objects["cmd_q"].fields == {"count": 0, "max_msgs": 8}                     # 最大は DBG のときだけ
    assert [w.task for w in objects["sem_data"].waiters] == ["AppTaskSensorTCB"]
    assert objects["tmr_watchdog"].fields["remaining_ticks"] == 5
