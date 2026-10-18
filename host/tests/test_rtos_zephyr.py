"""fprobe.rtos.zephyr（最小）: Zephyr と同じ形の構造体を持つ zephyr_shape.elf の、.data の初期値の「止めたときの様子」から。"""

from pathlib import Path

import pytest
from rtos_kernel import TaskStackBuilder

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo
from fprobe.rtos import detect, open_rtos
from fprobe.rtos.model import TaskState
from fprobe.rtos.structs import StructLayout
from fprobe.rtos.zephyr import Zephyr
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "zephyr_shape" / "zephyr_shape.elf"
STACK = 512


@pytest.fixture(scope="module")
def image():
    return DebugInfo(ELF), Symbols(ELF)


def zephyr(image, registers=None) -> tuple[Zephyr, ImageAccess]:
    info, symbols = image
    access = ImageAccess.from_symbols(symbols, registers)
    return Zephyr(info, access, symbols), access


def test_detect(image):
    info, symbols = image
    assert detect(info) == "zephyr"
    assert isinstance(open_rtos(info, ImageAccess.from_symbols(symbols), symbols), Zephyr)


def test_threads_states_and_priorities(image):
    rtos, _ = zephyr(image)
    tasks = rtos.tasks()
    assert [(t.name, t.state, t.priority) for t in tasks] == [
        ("main", TaskState.RUNNING, 0), ("sensor", TaskState.BLOCKED, 5), ("sleeper", TaskState.BLOCKED, 7),
        ("parked", TaskState.SUSPENDED, -2), ("idle", TaskState.READY, 15), ("gone", TaskState.DELETED, 3),
        ("ctl", TaskState.BLOCKED, 2), ("logger", TaskState.READY, 4)]
    _, symbols = image
    assert tasks[1].handle == symbols.variable("sensor_thread").address


def test_waits(image):
    rtos, _ = zephyr(image)
    t = {x.name: x for x in rtos.tasks()}
    _, symbols = image
    w = t["sensor"].wait
    assert (w.reason, w.name, w.kind, w.object) == ("pend", "sem_data", "k_sem", symbols.variable("sem_data").address)
    assert t["sleeper"].wait.reason == "sleep"
    assert t["main"].wait is None and t["idle"].wait is None and t["parked"].wait is None


def test_stacks(image):
    rtos, _ = zephyr(image)
    _, symbols = image
    s = {x.name: x for x in rtos.tasks()}["sensor"].stack
    base = symbols.variable("sensor_stack").address
    assert (s.base, s.end, s.size, s.pointer) == (base, base + STACK, STACK, base + STACK - 128)
    assert s.fill == "intact" and s.untouched == STACK - 128 and s.peak_used == 128 and s.used == 128
    main = {x.name: x for x in rtos.tasks()}["main"].stack                # 実行中: レジスタが読めない
    assert main.pointer is None


def test_running_thread_stack_pointer(image):
    _, symbols = image
    top = symbols.variable("main_stack").address + STACK - 40
    rtos, _ = zephyr(image, {"psp": top})
    assert {x.name: x for x in rtos.tasks()}["main"].stack.used == 40


def test_backtrace_of_pended_thread(image):
    """sensor が k_sem_take の中で切り替わった: r4〜r11 と psp は callee_saved、ハードのフレームは psp に。"""
    info, symbols = image
    rtos, access = zephyr(image)
    thread = symbols.variable("sensor_thread").address
    sem = symbols.variable("sem_data").address
    b = TaskStackBuilder(symbols, top=0x2000_8000)
    b.memory = access
    b.use_psp(symbols.variable("sensor_stack").address + STACK)
    b.enter("sensor_entry", 0, 0, 0, lr=0)
    pc = b.call("sensor_entry", "k_sem_take", sem, 0xFFFF_FFFF)
    frame = (b.regs["sp"] - 0x20) & ~7
    words = [b.regs[r] for r in ("r0", "r1", "r2", "r3", "r12", "lr")] + [pc, 0x0100_0000]
    for i, w in enumerate(words):
        access.write_u32(frame + 4 * i, w)
    layout = StructLayout.of(info, "struct k_thread")
    for n in range(8):
        access.write_u32(thread + layout.offset(f"callee_saved.v{n + 1}"), b.regs[f"r{n + 4}"])
    access.write_u32(thread + layout.offset("callee_saved.psp"), frame)
    ctx = rtos.saved_context("sensor")
    assert ctx.registers["pc"] == pc and ctx.sp == b.regs["sp"] and not ctx.fp
    frames = rtos.backtrace("sensor", variables=True)
    assert [f["function"] for f in frames] == ["k_sem_take", "sensor_entry"]
    assert {a["name"]: a["value"] for a in frames[0]["arguments"]}["sem"]["pointer"] == sem
    assert rtos.saved_context("main") is None


def test_snapshot(image):
    rtos, _ = zephyr(image)
    snap = rtos.snapshot()
    assert snap["rtos"] == "Zephyr" and snap["current"] == "main" and len(snap["tasks"]) == 8


def test_current_variable_and_names_by_handle(image):
    rtos, access = zephyr(image)
    current = rtos.current()
    assert int.from_bytes(access.read_memory(rtos.current_address(), 4), "little") == current
    assert rtos.handle_name(current) == "main"
