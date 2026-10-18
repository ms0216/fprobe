"""fprobe.rtos.freertos: 止まった FreeRTOS のタスクの一覧・状態・待っているもの・スタック・タスクごとの呼び出し履歴。

偽のメモリは rtos_kernel.FreeRTOSBuilder（freertos_demo.elf の像の上に TCB・リストを組み立てる）。
"""

import pytest
from rtos_kernel import FILL, PORT_MAX_DELAY, FreeRTOSBuilder, shared

from fprobe.dwarf.access import AccessError, ImageAccess
from fprobe.rtos import detect, open_rtos
from fprobe.rtos.freertos import FreeRTOS
from fprobe.rtos.model import TaskState
from fprobe.rtos.structs import StructLayout

TICK = 0xFFFF_FFF0                           # 折り返しの直前（時間切れが折り返す待ちを試す）


@pytest.fixture
def kernel():
    """ctrl（キューの無期限の受信）・sensor（実行中）・worker（セマフォの時間切れ付き、折り返し）・fpu（遅延）・
    logger（ミューテックス、持ち主 worker）・dormant（停止）・oneshot（削除待ち）・IDLE（実行可能）。"""
    k = FreeRTOSBuilder()
    k.set_tick(TICK)
    h = {name: k.task(name, prio, words) for name, prio, words in
         [("ctrl", 3, 256), ("sensor", 2, 160), ("worker", 1, 512), ("fpu", 1, 256), ("logger", 1, 128),
          ("dormant", 1, 128), ("oneshot", 2, 128), ("IDLE", 0, 128)]}
    cmd_queue = k.queue("cmd_queue", "queue", 4, 4)
    done_sem = k.queue("done_sem", "binary_semaphore", 1, 0)
    log_mutex = k.queue("log_mutex", "mutex", 1, 0, holder=h["worker"])
    k.put_state(h["ctrl"], k.var("xSuspendedTaskList"))
    k.put_event(h["ctrl"], k.receivers(cmd_queue))
    k.put_state(h["sensor"], k.ready(2))
    k.set_current(h["sensor"])
    k.put_state(h["worker"], k.var("xDelayedTaskList2"), (TICK + 50) & 0xFFFF_FFFF)    # 折り返した: 溢れのリスト
    k.put_event(h["worker"], k.receivers(done_sem))
    k.put_state(h["fpu"], k.var("xDelayedTaskList1"), TICK + 2)
    k.put_state(h["logger"], k.var("xSuspendedTaskList"))
    k.put_event(h["logger"], k.receivers(log_mutex))
    k.put_state(h["dormant"], k.var("xSuspendedTaskList"))
    k.put_state(h["oneshot"], k.var("xTasksWaitingTermination"))
    k.put_state(h["IDLE"], k.ready(0))
    k.handles, k.queues = h, {"cmd_queue": cmd_queue, "done_sem": done_sem, "log_mutex": log_mutex}
    return k


def rtos_of(k: FreeRTOSBuilder, **registers) -> FreeRTOS:
    k.memory.registers = dict(registers)
    info, symbols = shared()
    return FreeRTOS(info, k.memory, symbols)


def by_name(tasks):
    return {t.name: t for t in tasks}


def test_detect_and_layouts():
    info, symbols = shared()
    assert detect(info) == "freertos"
    tcb = StructLayout.of(info, "TCB_t")
    assert tcb.offset("pxTopOfStack") == 0                  # FreeRTOS の約束: 先頭は保存した SP
    assert tcb.offset("xStateListItem.pxContainer") == tcb.offset("xStateListItem") + 16
    assert not tcb.has("xMPUSettings") and tcb.has("pxEndOfStack")
    assert tcb.length("pcTaskName") == 12
    rtos = open_rtos(info, ImageAccess.from_symbols(symbols), symbols)
    assert isinstance(rtos, FreeRTOS) and rtos.port == "armv7m_fp"      # ARM_CM4F の port（vPortEnableVFP がある）


def test_task_list_states(kernel):
    tasks = rtos_of(kernel).tasks()
    names = [t.name for t in tasks]
    assert names == ["ctrl", "sensor", "worker", "fpu", "logger", "dormant", "oneshot", "IDLE"]   # 作った順
    t = by_name(tasks)
    assert {n: t[n].state for n in names} == {
        "ctrl": TaskState.BLOCKED, "sensor": TaskState.RUNNING, "worker": TaskState.BLOCKED, "fpu": TaskState.BLOCKED,
        "logger": TaskState.BLOCKED, "dormant": TaskState.SUSPENDED, "oneshot": TaskState.DELETED,
        "IDLE": TaskState.READY}
    assert t["ctrl"].priority == 3 and t["IDLE"].priority == 0 and t["worker"].number == 3
    assert t["sensor"].handle == kernel.handles["sensor"]


def test_waits(kernel):
    t = by_name(rtos_of(kernel).tasks())
    ctrl = t["ctrl"].wait
    assert (ctrl.reason, ctrl.name, ctrl.kind, ctrl.object, ctrl.wake_tick) == \
        ("receive", "cmd_queue", "queue", kernel.queues["cmd_queue"], None)
    worker = t["worker"].wait
    assert (worker.reason, worker.name, worker.kind) == ("receive", "done_sem", "binary_semaphore")
    assert worker.wake_tick == (TICK + 50) & 0xFFFF_FFFF and worker.remaining_ticks == 50
    fpu = t["fpu"].wait
    assert (fpu.reason, fpu.wake_tick, fpu.remaining_ticks, fpu.object) == ("delay", TICK + 2, 2, None)
    logger = t["logger"].wait
    assert (logger.kind, logger.name, logger.holder) == ("mutex", "log_mutex", "worker")
    assert t["dormant"].wait is None and t["sensor"].wait is None and t["IDLE"].wait is None
    assert t["ctrl"].to_dict()["wait"] == {"reason": "receive", "object": kernel.queues["cmd_queue"],
                                           "name": "cmd_queue", "kind": "queue", "list": kernel.receivers(
                                               kernel.queues["cmd_queue"])}


def test_send_wait_and_unregistered_queue(kernel):
    k = kernel
    full = k.queue(None, "queue", 2, 8)                      # 登録簿に無い
    sender = k.task("producer", 2)
    k.put_state(sender, k.var("xDelayedTaskList1"), TICK + 7)
    k.put_event(sender, k.senders(k.queues["cmd_queue"]))
    lost = k.task("lost", 2)
    k.put_state(lost, k.var("xSuspendedTaskList"))
    k.put_event(lost, k.receivers(full))
    t = by_name(rtos_of(k).tasks())
    assert (t["producer"].wait.reason, t["producer"].wait.name, t["producer"].wait.remaining_ticks) == \
        ("send", "cmd_queue", 7)
    w = t["lost"].wait
    assert t["lost"].state is TaskState.BLOCKED
    assert (w.reason, w.object, w.name, w.list) == ("event", None, None, k.receivers(full))


def test_symbol_names_a_static_queue(kernel):
    """登録簿に無いキューでも、静的に置いたキュー（シンボルがある）なら名前が分かる。"""
    k = kernel
    info, symbols = shared()
    q = k.queue(None, "counting_semaphore", 3, 0)
    waiting = k.task("waiter", 2)
    k.put_state(waiting, k.var("xSuspendedTaskList"))
    k.put_event(waiting, k.receivers(q))

    class Static:
        def __init__(self, inner):
            self.inner = inner

        def symbol_at(self, address):
            if q <= address < q + 80:
                from fprobe.symbols import Symbol
                return Symbol("static_sem", q, 80, "object"), address - q
            return self.inner.symbol_at(address)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    rtos = FreeRTOS(info, k.memory, Static(symbols))
    w = by_name(rtos.tasks())["waiter"].wait
    assert (w.reason, w.name, w.object, w.kind) == ("receive", "static_sem", q, "counting_semaphore")


def test_notification_wait_and_pending_ready(kernel):
    k = kernel
    notified = k.task("notified", 2)
    k.put_state(notified, k.var("xSuspendedTaskList"))
    k.notify_waiting(notified)
    woken = k.task("woken", 2)
    k.put_state(woken, k.var("xDelayedTaskList1"), TICK + 9)
    k.put_event(woken, k.var("xPendingReadyList"))         # スケジューラの停止中に起こされた
    t = by_name(rtos_of(k).tasks())
    assert t["notified"].state is TaskState.BLOCKED and t["notified"].wait.reason == "notification"
    assert t["woken"].state is TaskState.READY and "pending_ready" in t["woken"].notes


def test_stack_usage(kernel):
    k = kernel
    worker = k.handles["worker"]
    end = k.stack_end(worker)
    k.memory.write_memory(end - 900, b"\x00" * 100)        # 一度ここまで使った（いまは浅い）
    t = by_name(rtos_of(k).tasks())
    s = t["worker"].stack
    base = k.read32(worker + k.tcb.offset("pxStack"))
    assert s.base == base and s.end == end and s.size == end - base
    assert s.pointer == end - 64 and s.used == 64
    assert s.fill == "intact" and s.untouched == (end - 900) - base and s.peak_used == 900
    assert not s.overflow
    d = s.to_dict()
    assert d["peak_used"] == 900 and d["size"] == end - base


def test_running_task_stack_pointer_is_psp(kernel):
    k = kernel
    end = k.stack_end(k.handles["sensor"])
    s = by_name(rtos_of(k, psp=end - 200).tasks())["sensor"].stack
    assert s.pointer == end - 200 and s.used == 200
    s = by_name(rtos_of(k).tasks())["sensor"].stack              # 走っている（レジスタが読めない）
    assert s.pointer is None and s.used is None and s.untouched is not None


def test_stack_overflow_marks(kernel):
    k = kernel
    smashed = k.task("smashed", 2, 64)
    k.put_state(smashed, k.ready(2))
    base = k.read32(smashed + k.tcb.offset("pxStack"))
    k.memory.write_memory(base, b"\x11" * 8)                  # 底が壊れた（上には塗りが残る）
    under = k.task("under", 2, 64)
    k.put_state(under, k.ready(2))
    k.u32(under + k.tcb.offset("pxTopOfStack"), k.read32(under + k.tcb.offset("pxStack")) - 8)
    plain = k.task("plain", 2, 64, fill=False)
    k.put_state(plain, k.ready(2))
    t = by_name(rtos_of(k).tasks())
    assert t["smashed"].stack.overflow and t["smashed"].stack.fill == "none"
    assert t["under"].stack.overflow
    assert t["plain"].stack.fill == "none" and not t["plain"].stack.overflow and t["plain"].stack.peak_used is None


def test_snapshot(kernel):
    snap = rtos_of(kernel).snapshot()
    assert snap["rtos"] == "FreeRTOS" and snap["tick"] == TICK and snap["current"] == "sensor"
    assert snap["scheduler"] == "running" and len(snap["tasks"]) == 8
    assert snap["tasks"][1]["state"] == "running"


def test_corrupt_list_stops(kernel):
    k = kernel
    lst = k.ready(0)
    item = k.read32(lst + k.list.offset("xListEnd") + k.item.offset("pxNext"))
    k.u32(item + k.item.offset("pxNext"), item)              # 自分を指す: 終わらないリスト
    rtos = rtos_of(k)
    tasks = rtos.tasks()                                     # 止まる
    assert "IDLE" in [t.name for t in tasks]
    assert any("壊れ" in w for w in rtos.warnings)


def test_missing_kernel_is_reported():
    from fprobe.dwarf.info import DebugInfo
    from pathlib import Path
    sram = DebugInfo(Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf")
    assert detect(sram) is None
    with pytest.raises(KeyError):
        FreeRTOS(sram, ImageAccess())


# --- タスクごとの呼び出し履歴 -------------------------------------------------------------------

def exit_trap() -> int:
    return shared()[1].symbol("prvTaskExitError").address | 1


def functions(frames):
    return [f["function"] for f in frames]


def arguments(frame):
    return {a["name"]: a["value"] for a in frame["arguments"]}


def test_backtrace_of_preempted_task_in_recursion(kernel):
    """worker が recurse の 4 段目で割り込まれ、PendSV で切り替わった（FP の文脈なし）。"""
    k = kernel
    h = k.handles["worker"]
    b = k.stack_builder(h)
    b.enter("worker_task", lr=exit_trap())
    b.call("worker_task", "recurse", 6, 0)
    for depth in (5, 4, 3):
        b.call("recurse", "recurse", depth, 0x1234)
    pc = b.regs["pc"]
    k.switch_out(h, b, pc)
    rtos = rtos_of(k)
    ctx = rtos.saved_context("worker")
    assert not ctx.fp and ctx.registers["pc"] == pc and ctx.sp == b.regs["sp"]
    frames = rtos.backtrace("worker", variables=True)
    assert functions(frames) == ["recurse"] * 4 + ["worker_task"]        # prvTaskExitError で止まる
    assert frames[0]["pc"] == pc
    assert arguments(frames[0])["depth"] == 3
    insp = rtos.inspector("worker")
    assert insp.read("depth")["value"] == 3


def test_backtrace_with_fp_context(kernel):
    """fpu が fpu_step の中で切り替わった: EXC_RETURN 0xFFFFFFED、拡張フレームの s0（引数 x）と s16〜s31。"""
    import struct
    k = kernel
    h = k.handles["fpu"]
    b = k.stack_builder(h)
    b.enter("fpu_task", lr=exit_trap())
    pc = b.call("fpu_task", "fpu_step", 0, 200)
    s0 = struct.unpack("<I", struct.pack("<f", 3.5))[0]
    k.switch_out(h, b, pc, fp=[s0] + [0x3F80_0000] * 15, fp_high=[0x4000_0000 + n for n in range(16)])
    rtos = rtos_of(k)
    ctx = rtos.saved_context("fpu")
    assert ctx.fp and ctx.exc_return == 0xFFFF_FFED
    access = rtos.task_access("fpu")
    assert access.read_register("s0") == s0 and access.read_register("s17") == 0x4000_0001
    frames = rtos.backtrace("fpu", variables=True)
    assert functions(frames) == ["fpu_step", "fpu_task"]
    assert arguments(frames[0])["x"] == 3.5


def test_backtrace_of_task_blocked_in_queue_receive(kernel):
    k = kernel
    h = k.handles["ctrl"]
    q = k.queues["cmd_queue"]
    b = k.stack_builder(h)
    b.enter("ctrl_task", lr=exit_trap())
    pc = b.call("ctrl_task", "xQueueReceive", q, 0x2000_FF00, PORT_MAX_DELAY)
    k.switch_out(h, b, pc)
    frames = rtos_of(k).backtrace("ctrl", variables=True)
    assert functions(frames) == ["xQueueReceive", "ctrl_task"]
    assert arguments(frames[0])["xQueue"]["pointer"] == q


def test_backtrace_of_task_that_never_ran(kernel):
    k = kernel
    h = k.handles["dormant"]
    k.initial_context(h, "dormant_task", parameter=0x55)
    rtos = rtos_of(k)
    frames = rtos.backtrace("dormant")
    assert functions(frames) == ["dormant_task"]
    assert frames[0]["pc"] == shared()[1].symbol("dormant_task").address
    assert rtos.task_access("dormant").read_register("r0") == 0x55


def test_backtrace_of_running_task_uses_real_registers(kernel):
    k = kernel
    h = k.handles["sensor"]
    b = k.stack_builder(h)
    b.enter("sensor_task", lr=exit_trap())
    pc = b.call("sensor_task", "vTaskDelay", 5)
    access = b.halt_at(pc)
    info, symbols = shared()
    rtos = FreeRTOS(info, access, symbols)
    assert rtos.task_access("sensor") is access and rtos.saved_context("sensor") is None
    assert functions(rtos.backtrace("sensor")) == ["vTaskDelay", "sensor_task"]
    assert by_name(rtos.tasks())["sensor"].stack.pointer == b.regs["psp"]


def test_backtrace_of_running_task_halted_in_interrupt(kernel):
    """実行中の sensor に SysTick が入ったところで止めた: 例外のフレームを越えてタスクの関数へ戻る。"""
    k = kernel
    h = k.handles["sensor"]
    b = k.stack_builder(h)
    b.enter("sensor_task", lr=exit_trap())
    in_delay = b.call("sensor_task", "vTaskDelay", 5)
    pc = b.interrupt("xPortSysTickHandler", in_delay, exc_return=0xFFFF_FFFD)
    info, symbols = shared()
    rtos = FreeRTOS(info, b.halt_at(pc), symbols)
    frames = rtos.backtrace("sensor")
    assert functions(frames) == ["xPortSysTickHandler", "vTaskDelay", "sensor_task"]
    assert frames[1]["note"] == "exception"


def test_task_lookup_errors(kernel):
    rtos = rtos_of(kernel)
    with pytest.raises(KeyError):
        rtos.backtrace("nobody")
    assert rtos.task(kernel.handles["IDLE"]).name == "IDLE"
    with pytest.raises(AccessError):
        rtos.task_access("worker").read_register("msp")      # 保存された文脈に無い


def test_current_variable_and_names_by_handle(kernel):
    rtos = rtos_of(kernel)
    assert rtos.current_address() == kernel.var("pxCurrentTCB")
    assert rtos.current() == kernel.handles["sensor"]
    assert rtos.handle_name(kernel.handles["worker"]) == "worker"
