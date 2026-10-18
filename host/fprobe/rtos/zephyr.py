"""Zephyr の認識（最小、Cortex-M）: スレッドの一覧・状態・待っているもの・スタック、スレッドごとの呼び出し履歴。

仕様: docs/superpowers/specs/2026-10-17-f6-rtos-design.md の 6 節。
前提: CONFIG_THREAD_MONITOR（_kernel.threads のリスト）。CONFIG_THREAD_NAME（無ければ番地を名前にする）、
CONFIG_THREAD_STACK_INFO（無ければスタックの範囲は分からない）、CONFIG_INIT_STACKS（0xAA の塗り）。
構造体の位置は DWARF から（struct k_thread・struct z_kernel）。FP の文脈の上位（s16〜s31）は扱わない。
"""

from __future__ import annotations

from ..dwarf.access import AccessError, TargetAccess
from ..dwarf.info import DebugInfo
from ..dwarf.typesys import BaseType, strip, type_name
from .base import KernelAwareness
from .cortexm import THREAD_PSP_EXC_RETURN, SavedContext, unstack_exception_frame
from .model import HeapInfo, KernelObject, StackInfo, Task, TaskState, WaitInfo
from .stackcheck import check_stack
from .structs import StructLayout
from .zephyr_objects import ZephyrObjects

__all__ = ["FILL_BYTE", "Zephyr"]

FILL_BYTE = 0xAA                  # CONFIG_INIT_STACKS
MAX_THREADS = 256
# base.thread_state のビット（kernel_structs.h）
PENDING, SLEEPING, DEAD, SUSPENDED, ABORTING, QUEUED = 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 7


class Zephyr(KernelAwareness):
    name = "Zephyr"
    priority_order = -1           # 小さい方が高い（負は協調的なスレッド）

    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None):
        super().__init__(info, access, symbols)
        self.thread = StructLayout.of(info, "struct k_thread")
        self.kernel = StructLayout.of(info, "struct z_kernel")
        self.kernel_address = self.address("_kernel")
        if not self.kernel.has("threads"):
            raise KeyError("_kernel.threads が無い（CONFIG_THREAD_MONITOR が要る）")

    @staticmethod
    def detect(info: DebugInfo) -> bool:
        try:
            info.global_variable("_kernel")
            StructLayout.of(info, "struct k_thread")
            return StructLayout.of(info, "struct z_kernel").has("threads")
        except KeyError:
            return False

    def _field(self, handle: int, path: str, signed: bool = False) -> int:
        return self.mem.uint(handle + self.thread.offset(path), self.thread.member_size(path), signed)

    def current_address(self) -> int:
        """いま CPU 0 で動いているスレッド（_kernel.cpus[0].current）の番地。"""
        return self.kernel_address + self.kernel.offset("cpus.current")

    def threads(self) -> list[int]:
        """_kernel.threads から next_thread を辿ったスレッドの番地。"""
        out: list[int] = []
        handle = self.mem.u32(self.kernel_address + self.kernel.offset("threads"))
        while handle:
            if handle in out or len(out) >= MAX_THREADS:
                self.warnings.append(f"スレッドのリストが壊れている（{len(out)} 個で止めた）")
                break
            out.append(handle)
            handle = self._field(handle, "next_thread")
        return out

    def tasks(self) -> list[Task]:
        self.warnings = []
        current = self.current()
        tasks = [self._task(handle, handle == current) for handle in self.threads()]
        self._names = {t.handle: t.name for t in tasks}
        return tasks

    def thread_name(self, handle: int) -> str:
        if self.thread.has("name"):
            name = self.mem.cstring(handle + self.thread.offset("name"), self.thread.member_size("name"))
            if name:
                return name
        return f"0x{handle:08X}"

    handle_name = thread_name

    def _task(self, handle: int, running: bool) -> Task:
        state_bits = self._field(handle, "base.thread_state")
        signed = isinstance(strip(self.thread.member_type("base.prio")), BaseType) and \
            strip(self.thread.member_type("base.prio")).signed
        priority = self._field(handle, "base.prio", signed)
        wait = None
        if state_bits & (DEAD | ABORTING):
            state = TaskState.DELETED
        elif state_bits & SUSPENDED:
            state = TaskState.SUSPENDED
        elif state_bits & PENDING:
            state = TaskState.BLOCKED
            wait = self._pended_on(self._field(handle, "base.pended_on"))
        elif running:
            state = TaskState.RUNNING
        elif state_bits & QUEUED:
            state = TaskState.READY
        else:
            state = TaskState.BLOCKED                   # 寝ている（_THREAD_SLEEPING、または時間切れの待ち）・起動前
            wait = WaitInfo("sleep")
        return Task(handle, self.thread_name(handle), priority, state, self._stack(handle, running), wait)

    def _pended_on(self, wait_queue: int) -> WaitInfo:
        """待ちキューの番地 → それを持つもの（シンボルの名前と、DWARF の変数の型）。"""
        wait = WaitInfo("pend", list=wait_queue)
        if self.symbols is None or not wait_queue:
            return wait
        hit = self.symbols.symbol_at(wait_queue)
        if hit is None or hit[0].kind != "object":
            return wait
        symbol, offset = hit
        wait.object, wait.name = symbol.address, symbol.name if offset == 0 else f"{symbol.name}+{offset}"
        try:
            wait.kind = type_name(self.info.global_variable(symbol.name).type).removeprefix("struct ")
        except KeyError:
            pass
        return wait

    def _stack(self, handle: int, running: bool) -> StackInfo:
        if self.thread.has("stack_info.start"):
            base = self._field(handle, "stack_info.start")
            end: int | None = base + self._field(handle, "stack_info.size")
        else:
            base, end = 0, None
        if running:
            try:
                pointer: int | None = self.access.read_register("psp")
            except AccessError:
                pointer = None
        else:
            pointer = self._field(handle, "callee_saved.psp")
        return check_stack(self.access.read_memory, base, end, pointer, FILL_BYTE)

    def _exc_return(self, handle: int) -> int:
        if self.thread.has("arch.mode_exc_return"):
            return 0xFFFF_FF00 | self._field(handle, "arch.mode_exc_return")
        return THREAD_PSP_EXC_RETURN

    def saved_context(self, key: Task | str | int) -> SavedContext | None:
        task = self.task(key)
        if task.state is TaskState.RUNNING:
            return None
        psp = self._field(task.handle, "callee_saved.psp")
        exc_return = self._exc_return(task.handle)
        regs = {f"r{n + 4}": self._field(task.handle, f"callee_saved.v{n + 1}") for n in range(8)}
        hw, sp = unstack_exception_frame(self.access.read_memory, psp, exc_return)
        regs.update(hw)
        regs["sp"] = regs["psp"] = sp
        return SavedContext(psp, regs, exc_return, not exc_return & 0x10)

    # --- カーネルのオブジェクト（U08） ----------------------------------------------------------------

    def objects(self, kinds: list[str] | None = None, tasks: list[Task] | None = None) -> list[KernelObject]:
        """k_sem・k_mutex・k_msgq・k_event・k_timer・k_queue / k_fifo / k_lifo・k_stack・k_mem_slab・k_heap。"""
        tasks = self.tasks() if tasks is None else tasks
        return self.select(ZephyrObjects(self).objects(tasks), kinds)

    def heap(self) -> HeapInfo | None:
        return ZephyrObjects(self).heap()

    def handle_priority(self, handle: int) -> int:
        signed = isinstance(strip(self.thread.member_type("base.prio")), BaseType) and             strip(self.thread.member_type("base.prio")).signed
        return self._field(handle, "base.prio", signed)

    def heap_free_word(self) -> tuple[int, int | None] | None:
        """_system_heap の z_heap.free_bytes（CONFIG_SYS_HEAP_RUNTIME_STATS）。"""
        objects = ZephyrObjects(self)
        s, address = objects.layouts.get("k_heap"), self.optional_address("_system_heap")
        z = None if s is None or address is None else objects._z_heap(s, address)
        if z is None or not z[0].has("free_bytes"):
            return None
        heap = self.heap()
        return z[1] + z[0].offset("free_bytes"), None if heap is None else heap.total

    def idle_handles(self) -> set[int]:
        """_kernel.cpus[i].idle_thread。"""
        if not self.kernel.has("cpus.idle_thread"):
            return set()
        count = self.kernel.length("cpus") or 1
        size = self.kernel.member_size("cpus") // count
        at = self.kernel_address + self.kernel.offset("cpus.idle_thread")
        return {h for h in (self.mem.u32(at + i * size) for i in range(count)) if h}

    def saved_sp_address(self, handle: int) -> int:
        return handle + self.thread.offset("callee_saved.psp")

    def mutex_words(self, obj: KernelObject) -> tuple[int, int, int] | None:
        """持ち主 = owner、待ちの先頭 = wait_q.waitq.head（優先度順。スレッド = 値 − base.qnode_dlist の位置）。"""
        layout = ZephyrObjects(self).layouts.get("k_mutex")
        if obj.kind != "k_mutex" or layout is None or not layout.has("owner") or not layout.has("wait_q.waitq.head"):
            return None
        return (obj.handle + layout.offset("owner"), obj.handle + layout.offset("wait_q.waitq.head"),
                self.thread.offset("base.qnode_dlist"))

    def mutex_empty_head(self, obj: KernelObject) -> int:
        return obj.handle + ZephyrObjects(self).layouts["k_mutex"].offset("wait_q.waitq")

    def snapshot(self) -> dict:
        tasks = self.tasks()
        return {"rtos": self.name, "current": self._names.get(self.current()), "tasks": [t.to_dict() for t in tasks],
                "warnings": list(self.warnings)}
