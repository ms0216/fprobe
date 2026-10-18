"""CMSIS-RTOS2 RTX5 の認識（Cortex-M）: スレッドの一覧・状態・待っているもの・スタック、スレッドごとの呼び出し履歴、
カーネルのオブジェクト（rtx5_objects）。

仕様: docs/superpowers/specs/2026-10-18-u45-rtx5-design.md
構造体の位置は DWARF から（osRtxThread_t・osRtxInfo_t。rtx_os.h の型。アプリが rtx_os.h を含めば DWARF に入る）。
RTX5 はスレッドの全体の一覧を持たないので、カーネルのリストを辿って集める（rtx_thread.c と同じ繋ぎ方）:
  - 実行中 osRtxInfo.thread.run.curr
  - 準備完了 osRtxInfo.thread.ready.thread_list（thread_next）
  - 時間待ち delay_list（delay_next。delay は前との差分）、時間切れの無い待ち・停止 wait_list（delay_next）
  - 終わって join を待つ terminate_list（thread_next）、アイドル・タイマのスレッド
待っているオブジェクトは thread_prev を辿った先の、id がスレッド（0xF1）でない構造体（オブジェクトの thread_list は
スレッドの thread_next と同じ位置にあり、先頭の待ち手の thread_prev はオブジェクトを指す）。
保存された文脈は PendSV（irq_armv7m.S）の積み方: thread.sp から [s16〜s31]、r4〜r11、ハードのフレーム。EXC_RETURN の
下位 8 ビットは thread.stack_frame。スタックは底に osRtxStackMagicWord（壊れていればあふれ）、上を 0xCC で塗る
（osRtxConfigStackWatermark のとき）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError, TargetAccess
from ..dwarf.info import DebugInfo
from .base import KernelAwareness
from .cortexm import THREAD_PSP_EXC_RETURN, SavedContext, read_fp_first_context
from .model import KernelObject, StackInfo, Task, TaskState, WaitInfo
from .rtx5_objects import RTX5Objects
from .stackcheck import check_stack
from .structs import StructLayout

__all__ = ["FILL_BYTE", "ID_KINDS", "MAGIC_WORD", "RTX5"]

MAGIC_WORD = 0xE25A2EA5           # osRtxStackMagicWord（スタックの底）
FILL_BYTE = 0xCC                  # osRtxStackFillPattern
MAX_THREADS = 256
NAME_LIMIT = 64
ID_THREAD = 0xF1
ID_KINDS = {0xF2: "timer", 0xF3: "event_flags", 0xF5: "mutex", 0xF6: "semaphore", 0xF7: "memory_pool",
            0xFA: "message_queue"}
# osRtxThreadState（下位 4 ビット）
INACTIVE, READY, RUNNING, BLOCKED, TERMINATED = 0, 1, 2, 3, 4
# 待ちの種類（上位 4 ビット）→ 共通の言葉（WaitInfo.reason）
WAITS = {0x10: "delay", 0x20: "join", 0x30: "notification", 0x40: "event", 0x50: "receive", 0x60: "receive",
         0x70: "receive", 0x80: "receive", 0x90: "send"}
WAIT_KINDS = {0x30: "thread_flags", 0x40: "event_flags", 0x50: "mutex", 0x60: "semaphore", 0x70: "memory_pool",
              0x80: "message_queue", 0x90: "message_queue"}
# osKernelState → scheduler_state
SCHEDULER = {0: "not_started", 1: "not_started", 2: "running", 3: "suspended", 4: "suspended"}


class RTX5(KernelAwareness):
    name = "RTX5"
    priority_order = 1            # 大きい方が高い（osPriorityIdle = 1 〜 osPriorityISR = 56）

    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None):
        super().__init__(info, access, symbols)
        self.thread = StructLayout.of(info, "osRtxThread_t", "struct osRtxThread_s")
        self.os = StructLayout.of(info, "osRtxInfo_t")
        self.object = StructLayout.of(info, "osRtxObject_t")
        self.mutex = StructLayout.of(info, "osRtxMutex_t", "struct osRtxMutex_s")
        self.info_address = self.address("osRtxInfo")
        self._handles: list[int] = []

    @staticmethod
    def detect(info: DebugInfo) -> bool:
        try:
            info.global_variable("osRtxInfo")
            StructLayout.of(info, "osRtxThread_t", "struct osRtxThread_s")
            return StructLayout.of(info, "osRtxInfo_t").has("thread.run.curr")
        except KeyError:
            return False

    # --- 読み --------------------------------------------------------------------------------------

    def _field(self, handle: int, path: str, signed: bool = False) -> int:
        return self.mem.uint(handle + self.thread.offset(path), self.thread.member_size(path), signed)

    def _os(self, path: str) -> int:
        return self.mem.uint(self.info_address + self.os.offset(path), self.os.member_size(path))

    def _id(self, address: int) -> int:
        return self.mem.u8(address + self.object.offset("id"))

    def object_kind(self, address: int) -> str | None:
        """オブジェクトの id → 種類（mutex・semaphore 等。スレッド・分からない id は None）。"""
        return ID_KINDS.get(self._id(address))

    def object_name(self, address: int) -> str | None:
        """オブジェクト（スレッドも）の name（const char *）。無ければ None。"""
        pointer = self.mem.u32(address + self.object.offset("name"))
        if not pointer:
            return None
        try:
            return self.mem.cstring(pointer, NAME_LIMIT) or None
        except AccessError:
            return None

    def thread_name(self, handle: int) -> str:
        return self.object_name(handle) or f"0x{handle:08X}"

    handle_name = thread_name

    def current_address(self) -> int:
        """osRtxInfo.thread.run.curr の番地。"""
        return self.info_address + self.os.offset("thread.run.curr")

    def tick_count(self) -> int | None:
        return self._os("kernel.tick")

    def scheduler_state(self) -> str:
        return SCHEDULER.get(self._os("kernel.state"), "unknown")

    def version(self) -> str | None:
        pointer = self._os("os_id")
        return self.mem.cstring(pointer, NAME_LIMIT) if pointer else None

    # --- リスト ------------------------------------------------------------------------------------

    def walk(self, head: int, link: str, what: str) -> list[int]:
        """head から link（thread_next・delay_next）を辿ったスレッド。壊れていれば止め、warnings に書く。"""
        out: list[int] = []
        while head:
            if head in out or len(out) >= MAX_THREADS:
                self.warnings.append(f"{what} のリストが壊れている（{len(out)} 個で止めた）")
                break
            out.append(head)
            head = self._field(head, link)
        return out

    def delays(self) -> dict[int, int]:
        """delay_list のスレッド → 残りのティック（差分の和）。"""
        out, total = {}, 0
        for handle in self.walk(self._os("thread.delay_list"), "delay_next", "delay_list"):
            total += self._field(handle, "delay")
            out[handle] = total
        return out

    def threads(self) -> list[int]:
        """カーネルのリストに載っているスレッドの番地（実行中・準備完了・時間待ち・待ち・終わり・アイドル・タイマ）。"""
        found = [self.current()]
        found += self.walk(self._os("thread.ready.thread_list"), "thread_next", "ready")
        found += self.walk(self._os("thread.delay_list"), "delay_next", "delay_list")
        found += self.walk(self._os("thread.wait_list"), "delay_next", "wait_list")
        found += self.walk(self._os("thread.terminate_list"), "thread_next", "terminate_list")
        found.append(self._os("thread.idle"))
        if self.os.has("timer.thread"):
            found.append(self._os("timer.thread"))
        out: list[int] = []
        for handle in found:
            if not handle or handle in out:
                continue
            if self._id(handle) != ID_THREAD:
                self.warnings.append(f"0x{handle:08X} はスレッドではない（id 0x{self._id(handle):02X}）")
                continue
            out.append(handle)
        return out

    # --- スレッド ----------------------------------------------------------------------------------

    def tasks(self) -> list[Task]:
        self.warnings = []
        current = self.current()
        running = self.scheduler_state() == "running"
        delays = self.delays()
        tick = self.tick_count()
        self._handles = self.threads()
        tasks = [self._task(h, running and h == current, delays.get(h), tick) for h in self._handles]
        self._names = {t.handle: t.name for t in tasks}
        return tasks

    def _task(self, handle: int, running: bool, remaining: int | None, tick: int | None) -> Task:
        state_byte = self._field(handle, "state")
        state_low, wait_type = state_byte & 0x0F, state_byte & 0xF0
        wait = None
        notes: list[str] = []
        if running or state_low == RUNNING:
            state = TaskState.RUNNING if running else TaskState.READY
        elif state_low == READY:
            state = TaskState.READY
        elif state_low == BLOCKED and wait_type == 0:
            state = TaskState.SUSPENDED                       # osThreadSuspend（時間切れの無い待ちのリストに載る）
            wait = WaitInfo("suspended")
        elif state_low == BLOCKED:
            state = TaskState.BLOCKED
            wait = self._wait(handle, wait_type, remaining, tick)
        else:
            state = TaskState.DELETED                         # 終わり（join 待ち）・未使用
        stack = self._stack(handle, running)
        if stack.overflow and stack.base:
            notes.append(f"スタックの底の osRtxStackMagicWord（0x{MAGIC_WORD:08X}）が壊れているか、SP が底より下")
        priority = self._field(handle, "priority", True)
        return Task(handle, self.thread_name(handle), priority, state, stack, wait,
                    self._field(handle, "priority_base", True), notes=notes)

    def _wait(self, handle: int, wait_type: int, remaining: int | None, tick: int | None) -> WaitInfo:
        wait = WaitInfo(WAITS.get(wait_type, "unknown"), kind=WAIT_KINDS.get(wait_type))
        if remaining is not None:
            wait.remaining_ticks = remaining
            wait.wake_tick = None if tick is None else (tick + remaining) & 0xFFFF_FFFF
        if wait_type == 0x20:                                 # join: 待つ相手は thread_join が自分のスレッド
            for other in self._handles:
                if other != handle and self._field(other, "thread_join") == handle:
                    wait.object, wait.name, wait.kind = other, self.thread_name(other), "thread"
            return wait
        if wait_type in (0x10, 0x30):
            return wait
        owner = self.waited_object(handle)
        if owner is None:
            return wait
        wait.object = wait.list = owner
        wait.name = self.object_label(owner)
        kind = self.object_kind(owner)
        if kind is not None:
            wait.kind = kind
        if kind == "mutex":
            holder = self.mem.u32(owner + self.mutex.offset("owner_thread"))
            wait.holder = self.thread_name(holder) if holder else None
        return wait

    def waited_object(self, handle: int) -> int | None:
        """thread_prev を辿って、待ち手の列の持ち主（id がスレッドでない構造体）。"""
        node, seen = self._field(handle, "thread_prev"), set()
        while node and node not in seen and len(seen) < MAX_THREADS:
            seen.add(node)
            if self._id(node) != ID_THREAD:
                return node
            node = self._field(node, "thread_prev")
        return None

    def object_label(self, address: int) -> str:
        """オブジェクトの名前: name、無ければ変数（シンボル）、無ければ番地。"""
        name = self.object_name(address)
        if name:
            return name
        if self.symbols is not None:
            hit = self.symbols.symbol_at(address)
            if hit is not None and hit[1] == 0 and hit[0].kind == "object":
                return hit[0].name
        return f"0x{address:08X}"

    def _stack(self, handle: int, running: bool) -> StackInfo:
        base = self._field(handle, "stack_mem")
        size = self._field(handle, "stack_size")
        end = base + size if base else None
        if running:
            try:
                pointer: int | None = self.access.read_register("psp")
            except AccessError:
                pointer = None
        else:
            pointer = self._field(handle, "sp")
        if not base:
            return StackInfo(0, None, pointer)
        try:
            magic = self.mem.u32(base)
        except AccessError:
            return StackInfo(base, end, pointer)
        if magic != MAGIC_WORD:
            info = check_stack(self.access.read_memory, base, end, pointer, FILL_BYTE)
            info.overflow = True
            return info
        info = check_stack(self.access.read_memory, base + 4, end, pointer, FILL_BYTE)
        info.base = base
        if info.untouched is not None:
            info.untouched += 4                               # 底の印の語は使っていない
        return info

    # --- 呼び出し履歴 ------------------------------------------------------------------------------

    def saved_context(self, key: Task | str | int) -> SavedContext | None:
        task = self.task(key)
        if task.state is TaskState.RUNNING:
            return None
        frame = self._field(task.handle, "stack_frame") if self.thread.has("stack_frame") else 0
        exc_return = 0xFFFF_FF00 | frame if frame else THREAD_PSP_EXC_RETURN
        return read_fp_first_context(self.access.read_memory, self._field(task.handle, "sp"), exc_return)

    def stop_pcs(self) -> list[int]:
        """スレッドの関数の戻り先は osThreadExit（svcRtxThreadNew が LR に置く）: そこから先は呼び出し元ではない。"""
        exit_function = self.function_address("osThreadExit")
        return [] if exit_function is None else [exit_function & ~1]

    # --- カーネルのオブジェクト ------------------------------------------------------------------------

    def objects(self, kinds: list[str] | None = None, tasks: list[Task] | None = None) -> list[KernelObject]:
        """ミューテックス・セマフォ・イベントフラグ・メモリプール・メッセージキュー・タイマ（rtx5_objects）。"""
        tasks = self.tasks() if tasks is None else tasks
        return self.select(RTX5Objects(self).objects(tasks), kinds)

    def handle_priority(self, handle: int) -> int:
        return self._field(handle, "priority_base", True)

    def idle_handles(self) -> set[int]:
        idle = self._os("thread.idle")
        return {idle} if idle else set()

    def saved_sp_address(self, handle: int) -> int:
        return handle + self.thread.offset("sp")

    def mutex_words(self, obj: KernelObject) -> tuple[int, int, int] | None:
        """持ち主 = owner_thread、待ちの先頭 = thread_list（優先度順。スレッドそのものを指すので差は 0）。"""
        if obj.kind != "mutex":
            return None
        return obj.handle + self.mutex.offset("owner_thread"), obj.handle + self.mutex.offset("thread_list"), 0

    def mutex_empty_head(self, obj: KernelObject) -> int:
        return 0

    def snapshot(self) -> dict:
        tasks = self.tasks()
        return {"rtos": self.name, "version": self.version(), "scheduler": self.scheduler_state(),
                "tick": self.tick_count(), "current": self._names.get(self.current()),
                "tasks": [t.to_dict() for t in tasks], "warnings": list(self.warnings)}
