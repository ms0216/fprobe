"""fprobe.rtos.threadx（U45）: ThreadX の本物のヘッダの構造体で作った threadx_shape.elf の、.data の初期値の「止めたときの様子」から。

スレッドの一覧（作られた環）、状態と待っているもの（tx_thread_suspend_control_block の id で見分ける）・残りのティック
（時間の輪 _tx_timer_list）、スタック（0xEF の塗り）、保存された文脈（Cortex-M の port の積み方）と呼び出し履歴、
カーネルのオブジェクト（mutex・semaphore・queue・event flags・block / byte pool・timer）。
"""

from pathlib import Path

import pytest
from rtos_kernel import TaskStackBuilder

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo
from fprobe.rtos import detect, open_rtos
from fprobe.rtos.contention import contention
from fprobe.rtos.cortexm import read_exc_return_first_context
from fprobe.rtos.model import TaskState
from fprobe.rtos.structs import StructLayout
from fprobe.rtos.threadx import ThreadX
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "threadx_shape" / "threadx_shape.elf"
STACK = 512


@pytest.fixture(scope="module")
def image():
    return DebugInfo(ELF), Symbols(ELF)


def tx(image, registers=None, **options) -> tuple[ThreadX, ImageAccess]:
    info, symbols = image
    access = ImageAccess.from_symbols(symbols, registers)
    return ThreadX(info, access, symbols, **options), access


def by_name(items):
    return {x.name: x for x in items}


def address(image, name: str) -> int:
    return image[1].variable(name).address


def test_detect(image):
    info, symbols = image
    assert detect(info) == "threadx"
    assert isinstance(open_rtos(info, ImageAccess.from_symbols(symbols), symbols), ThreadX)


def test_threads_states_and_priorities(image):
    rtos, _ = tx(image)
    tasks = rtos.tasks()
    assert [(t.name, t.state, t.priority) for t in tasks] == [
        ("main", TaskState.RUNNING, 10), ("logger", TaskState.READY, 10), ("sensor", TaskState.BLOCKED, 5),
        ("sleeper", TaskState.BLOCKED, 20), ("ctl", TaskState.BLOCKED, 3), ("parked", TaskState.SUSPENDED, 25),
        ("producer", TaskState.BLOCKED, 8), ("consumer", TaskState.BLOCKED, 9), ("waiter", TaskState.BLOCKED, 12),
        ("alloc", TaskState.BLOCKED, 15), ("gone", TaskState.DELETED, 10)]
    assert tasks[2].handle == address(image, "th_sensor")
    assert all(t.base_priority == t.priority for t in tasks)
    assert rtos.warnings == []


def test_kernel_state(image):
    rtos, _ = tx(image)
    assert rtos.priority_order == -1                         # ThreadX は小さい方が高い（0 が最高）
    assert rtos.port == "armv7m"                             # Cortex-M4 の ELF（Tag_CPU_arch）
    assert rtos.scheduler_state() == "running" and rtos.tick_count() == 1000
    assert rtos.current() == address(image, "th_main")
    assert rtos.current_address() == address(image, "_tx_thread_current_ptr")
    assert rtos.idle_handles() == set()                      # ThreadX にアイドルのスレッドは無い
    assert rtos.handle_name(address(image, "th_ctl")) == "ctl"
    assert rtos.handle_priority(address(image, "th_ctl")) == 3
    assert "ThreadX Cortex-M4/GNU Version 6.5.2" in rtos.version()


def test_scheduler_not_started(image):
    rtos, access = tx(image)
    access.write_u32(address(image, "_tx_thread_system_state"), 0xF0F0_F0F0)    # TX_INITIALIZE_IN_PROGRESS
    assert rtos.scheduler_state() == "not_started"
    access.write_u32(address(image, "_tx_thread_system_state"), 0)
    access.write_u32(address(image, "_tx_thread_preempt_disable"), 1)
    assert rtos.scheduler_state() == "suspended"


def test_idle_when_no_thread_runs(image):
    """_tx_thread_current_ptr が NULL（スケジューラが待っている）: 実行中のスレッドは無い。"""
    rtos, access = tx(image)
    access.write_u32(address(image, "_tx_thread_current_ptr"), 0)
    t = by_name(rtos.tasks())
    assert t["main"].state is TaskState.READY and rtos.current() == 0
    top = address(image, "main_stack") + STACK - 96                     # 止まったスレッドとして積んだ文脈
    access.write_u32(top, 0xFFFF_FFFD)
    assert rtos.saved_context("main").sp == top + 4 + 32 + 32
    assert rtos.snapshot()["current"] is None


def test_waits(image):
    rtos, _ = tx(image)
    t = by_name(rtos.tasks())
    w = t["sensor"].wait
    assert (w.reason, w.kind, w.name, w.object) == ("receive", "semaphore", "sem_data", address(image, "sem_data"))
    assert (w.remaining_ticks, w.wake_tick) == (10, 1010)
    w = t["sleeper"].wait                                    # 32 ティックより先: 最後の枠の残りを足す
    assert (w.reason, w.remaining_ticks, w.wake_tick, w.object) == ("delay", 50, 1050, None)
    w = t["ctl"].wait
    assert (w.reason, w.kind, w.name, w.holder, w.remaining_ticks) == ("receive", "mutex", "bus_lock", "sleeper", None)
    w = t["producer"].wait                                   # 満杯のキュー: 送る
    assert (w.reason, w.kind, w.name) == ("send", "queue", "cmd_q")
    w = t["consumer"].wait                                   # 空のキュー: 受け取る。名前の無いものは変数の名前
    assert (w.reason, w.kind, w.name) == ("receive", "queue", "reply_q")
    w = t["waiter"].wait
    assert (w.reason, w.kind, w.name) == ("event", "event_flags", "sys_events")
    w = t["alloc"].wait
    assert (w.reason, w.kind, w.name) == ("receive", "block_pool", "buf_pool")
    assert t["parked"].wait.reason == "suspended"
    assert t["main"].wait is None and t["logger"].wait is None and t["gone"].wait is None


def test_stacks(image):
    rtos, _ = tx(image)
    tasks = by_name(rtos.tasks())
    s = tasks["sensor"].stack
    base = address(image, "sensor_stack")
    assert (s.base, s.end, s.size, s.pointer) == (base, base + STACK, STACK, base + STACK - 128)
    assert s.fill == "intact" and s.untouched == STACK - 128 and s.peak_used == 128 and not s.overflow
    gone = tasks["gone"]
    assert gone.stack.overflow and gone.stack.fill == "none" and gone.notes
    assert tasks["main"].stack.pointer is None               # 実行中: レジスタが読めない


def test_running_thread_stack_pointer(image):
    top = address(image, "main_stack") + STACK - 40
    rtos, _ = tx(image, {"psp": top})
    assert by_name(rtos.tasks())["main"].stack.used == 40


# --- 保存された文脈 ---------------------------------------------------------------------------------------

def _frame(memory: ImageAccess, at: int) -> int:
    for i, v in enumerate([0, 1, 2, 3, 12, 0x0800_0101, 0x0800_0200, 0x0100_0000]):
        memory.write_u32(at + 4 * i, v)
    return at + 32


def test_exc_return_first_context_armv7m():
    """Cortex-M3/M4/M7/M33 の tx_thread_schedule.S: SP → EXC_RETURN、[s16〜s31]、r4〜r11、ハードのフレーム。"""
    memory = ImageAccess([(0x2000_0000, bytes(0x200))])
    at = 0x2000_0000
    memory.write_u32(at, 0xFFFF_FFED)
    for n in range(16):
        memory.write_u32(at + 4 + 4 * n, 0x1600 + n)
    for n in range(8):
        memory.write_u32(at + 68 + 4 * n, 0x400 + n)
    _frame(memory, at + 100)
    ctx = read_exc_return_first_context(memory.read_memory, at, "armv7m")
    assert ctx.fp and ctx.exc_return == 0xFFFF_FFED and ctx.top == at
    assert ctx.registers["s16"] == 0x1600 and ctx.registers["s31"] == 0x160F
    assert ctx.registers["r4"] == 0x400 and ctx.registers["r11"] == 0x407 and ctx.registers["pc"] == 0x0800_0200
    assert ctx.sp == at + 100 + 0x68
    memory.write_u32(at, 0xFFFF_FFFD)
    plain = read_exc_return_first_context(memory.read_memory, at, "armv7m")
    assert not plain.fp and plain.registers["r4"] == 0x1600
    assert plain.sp == at + 4 + 32 + 32 + 4                                 # xpsr の bit 9: 4 バイトの詰め物


def test_exc_return_first_context_armv6m():
    """Cortex-M0/M0+/M23: SP → EXC_RETURN、r8〜r11、r4〜r7、ハードのフレーム（STM が上向きにしか積めない）。"""
    memory = ImageAccess([(0x2000_0000, bytes(0x100))])
    at = 0x2000_0000
    memory.write_u32(at, 0xFFFF_FFFD)
    for i, n in enumerate([8, 9, 10, 11, 4, 5, 6, 7]):
        memory.write_u32(at + 4 + 4 * i, 0x400 + n)
    end = _frame(memory, at + 36)
    ctx = read_exc_return_first_context(memory.read_memory, at, "armv6m")
    assert not ctx.fp and all(ctx.registers[f"r{n}"] == 0x400 + n for n in range(4, 12))
    assert ctx.registers["pc"] == 0x0800_0200 and ctx.sp == end
    with pytest.raises(ValueError):
        read_exc_return_first_context(memory.read_memory, at, "armv7m_fp")


def test_port_override(image):
    rtos, _ = tx(image, port="armv6m")
    assert rtos.port == "armv6m"
    with pytest.raises(ValueError):
        tx(image, port="riscv")


def test_backtrace_of_waiting_thread(image):
    """sensor が tx_semaphore_get（_txe_semaphore_get）の中で切り替わった: PendSV が r4〜r11 と EXC_RETURN を積み、
    tx_thread_stack_ptr がそれを指す。最初の PC は _tx_thread_shell_entry（スレッドの入口を呼ぶ）。"""
    info, symbols = image
    rtos, access = tx(image)
    thread = address(image, "th_sensor")
    sem = address(image, "sem_data")
    b = TaskStackBuilder(symbols, top=0x2000_8000)
    b.memory = access
    b.use_psp(address(image, "sensor_stack") + STACK)
    b.enter("_tx_thread_shell_entry", lr=0xFFFF_FFFF)
    shell = symbols.symbol("_tx_thread_shell_entry")
    site = next(i for i in b._code(shell.address, shell.size) if i.mnemonic == "blx")
    b.enter("sensor_entry", 0, lr=(site.address + site.size) | 1)
    pc = b.call("sensor_entry", "_txe_semaphore_get", sem, 10)
    frame = (b.regs["sp"] - 0x20) & ~7
    words = [b.regs[r] for r in ("r0", "r1", "r2", "r3", "r12", "lr")] + [pc, 0x0100_0000]
    for i, w in enumerate(words):
        access.write_u32(frame + 4 * i, w)
    saved = frame - 32 - 4
    access.write_u32(saved, 0xFFFF_FFFD)
    for n in range(8):
        access.write_u32(saved + 4 + 4 * n, b.regs[f"r{n + 4}"])
    access.write_u32(thread + StructLayout.of(info, "TX_THREAD").offset("tx_thread_stack_ptr"), saved)
    ctx = rtos.saved_context("sensor")
    assert ctx.registers["pc"] == pc and ctx.sp == b.regs["sp"] and not ctx.fp
    frames = rtos.backtrace("sensor", variables=True)
    assert [f["function"] for f in frames][:3] == ["_txe_semaphore_get", "sensor_entry", "_tx_thread_shell_entry"]
    assert len(frames) == 3
    assert {a["name"]: a["value"] for a in frames[0]["arguments"]}["semaphore_ptr"]["pointer"] == sem
    assert rtos.saved_context("main") is None


def test_snapshot_and_sampler_words(image):
    rtos, access = tx(image)
    snap = rtos.snapshot()
    assert snap["rtos"] == "ThreadX" and snap["current"] == "main" and len(snap["tasks"]) == 11
    assert snap["scheduler"] == "running" and snap["tick"] == 1000 and snap["port"] == "armv7m"
    current = int.from_bytes(access.read_memory(rtos.current_address(), 4), "little")
    assert current == rtos.current()
    offset = StructLayout.of(rtos.info, "TX_THREAD").offset("tx_thread_stack_ptr")
    assert rtos.saved_sp_address(current) == current + offset


def test_broken_created_list_is_reported(image):
    rtos, access = tx(image)
    logger = address(image, "th_logger")
    access.write_u32(logger + rtos.thread.offset("tx_thread_created_next"), logger)          # 自分を指す
    names = [t.name for t in rtos.tasks()]
    assert names == ["main", "logger"] and rtos.warnings


def test_wrong_id_is_reported(image):
    rtos, access = tx(image)
    access.write_u32(address(image, "th_parked"), 0)                                         # tx_thread_id を消す
    names = [t.name for t in rtos.tasks()]
    assert "parked" not in names and any("parked" in w or "0x" in w for w in rtos.warnings)


# --- カーネルのオブジェクト ---------------------------------------------------------------------------------

def test_objects(image):
    rtos, _ = tx(image)
    objects = by_name(rtos.objects())
    assert {n: o.kind for n, o in objects.items()} == {
        "bus_lock": "mutex", "sem_data": "semaphore", "cmd_q": "queue", "reply_q": "queue",
        "sys_events": "event_flags", "buf_pool": "block_pool", "heap_pool": "byte_pool", "watchdog": "timer",
        "blink": "timer", "oneshot": "timer"}
    assert objects["sem_data"].source == "kernel" and objects["reply_q"].source == "variable"
    assert objects["watchdog"].aliases == ["tmr_watchdog"]
    assert objects["sem_data"].handle == address(image, "sem_data")
    assert rtos.warnings == []


def test_object_fields_and_waiters(image):
    rtos, _ = tx(image)
    objects = by_name(rtos.objects())
    sem = objects["sem_data"]
    assert sem.fields == {"count": 0}
    assert [(w.task, w.reason, w.priority) for w in sem.waiters] == [("sensor", "receive", 5)]
    assert objects["cmd_q"].fields == {"msg_size": 8, "count": 4, "max_msgs": 4, "free": 0}
    assert [(w.task, w.reason) for w in objects["cmd_q"].waiters] == [("producer", "send")]
    assert objects["reply_q"].fields == {"msg_size": 4, "count": 0, "max_msgs": 8, "free": 8}
    assert [(w.task, w.reason) for w in objects["reply_q"].waiters] == [("consumer", "receive")]
    events = objects["sys_events"]
    assert events.fields == {"flags": 1}
    assert [(w.task, w.reason, w.detail) for w in events.waiters] == [
        ("waiter", "event", {"flags": 6, "all": True, "clear": True})]
    assert objects["buf_pool"].fields == {"block_size": 32, "max_blocks": 8, "used_blocks": 8, "free_blocks": 0}
    assert [w.task for w in objects["buf_pool"].waiters] == ["alloc"]
    assert objects["heap_pool"].fields == {"size": 4096, "free": 1000, "used": 3096, "fragments": 5}
    assert objects["heap_pool"].waiters == []


def test_timers(image):
    rtos, _ = tx(image)
    objects = by_name(rtos.objects(kinds=["timer"]))
    assert objects["watchdog"].fields == {"active": True, "periodic": True, "period_ticks": 500,
                                          "remaining_ticks": 7, "callback": "blink_cb", "argument": 0}
    assert objects["blink"].fields["remaining_ticks"] == 40 and objects["blink"].fields["argument"] == 0x1234
    oneshot = objects["oneshot"].fields
    assert oneshot["active"] is False and oneshot["periodic"] is False and "remaining_ticks" not in oneshot


def test_mutex_without_inheritance_is_an_inversion(image):
    rtos, _ = tx(image)
    tasks = rtos.tasks()
    objects = rtos.objects(tasks=tasks)
    mutex = by_name(objects)["bus_lock"]
    assert mutex.fields["holder"] == {"task": "sleeper", "handle": mutex.holder, "priority": 20,
                                      "base_priority": 20, "inherited": False}
    assert mutex.fields["lock_count"] == 1 and not mutex.fields["available"]
    assert mutex.fields["priority_inherit"] is False
    (inv,) = contention(tasks, objects, rtos.priority_order)["inversions"]
    assert inv["waiter"]["task"] == "ctl" and inv["holder"]["task"] == "sleeper"
    assert [i["task"] for i in inv["interferers"]] == ["main", "logger"]


def test_mutex_words_for_the_sampler(image):
    rtos, access = tx(image)
    mutex = by_name(rtos.objects())["bus_lock"]
    owner, head, delta = rtos.mutex_words(mutex)
    assert int.from_bytes(access.read_memory(owner, 4), "little") == address(image, "th_sleeper")
    assert int.from_bytes(access.read_memory(head, 4), "little") - delta == address(image, "th_ctl")
    assert rtos.mutex_empty_head(mutex) == 0
    assert rtos.mutex_words(by_name(rtos.objects())["sem_data"]) is None
