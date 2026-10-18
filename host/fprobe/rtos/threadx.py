"""Eclipse ThreadX（旧 Azure RTOS）の認識（Cortex-M）: スレッドの一覧・状態・待っているもの・スタック、スレッドごとの
呼び出し履歴、カーネルのオブジェクト（threadx_objects）。

仕様: docs/superpowers/specs/2026-10-18-u45-threadx-design.md
構造体の位置は DWARF から（TX_THREAD・TX_TIMER_INTERNAL 等。tx_api.h の型。アプリが tx_api.h を含めば DWARF に入る）。
- スレッドの一覧は作られた環 _tx_thread_created_ptr（tx_thread_created_next。数は _tx_thread_created_count）。
  id が TX_THREAD_ID でないものは警告して外す。環が元に戻らない・数が多すぎるときは警告して止める。
- 実行中は _tx_thread_current_ptr（NULL ならスケジューラが待っている = 実行中のスレッドは無い）。
- 状態は tx_thread_state（TX_READY・TX_SLEEP・TX_*_SUSP 等）。待つオブジェクトは tx_thread_suspend_control_block
  （先頭の id で種類を見分ける）。キューは空なら受け取り、そうでなければ送りを待つ（ThreadX の待ち手は片方だけ）。
- 残りのティックはスレッドの tx_thread_timer（時間の輪 _tx_timer_list。tx_timer_info_get.c と同じ計算）。
- 優先度は小さい方が高い（0 が最高）。元の優先度は tx_thread_user_priority。
- スタックは tx_thread_stack_start〜tx_thread_stack_end（最後のバイト）。全体を 0xEF（TX_STACK_FILL。乱数の塗りなら
  tx_thread_stack_fill_value）で塗る。底が塗りのままでなければあふれ。
- 保存された文脈は tx_thread_stack_ptr から（cortexm.read_exc_return_first_context。port はメインライン "armv7m"
  かベースライン "armv6m"。ELF の Tag_CPU_arch から決める）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError, TargetAccess
from ..dwarf.info import DebugInfo
from .base import KernelAwareness
from .cortexm import EXC_RETURN_FIRST_LAYOUTS, SavedContext, arm_baseline, read_exc_return_first_context
from .model import KernelObject, StackInfo, Task, TaskState, WaitInfo
from .stackcheck import check_stack
from .structs import StructLayout
from .threadx_objects import IDS, KINDS, ThreadXObjects

__all__ = ["FILL_BYTE", "ThreadX"]

FILL_BYTE = 0xEF                  # TX_STACK_FILL（0xEFEFEFEF）
MAX_ITEMS = 4096
NAME_LIMIT = 64
VERSION_LIMIT = 160
TX_THREAD_ID = 0x54485244
INITIALIZING = (0xF0F0_F0F0, 0xF0F0_F0F1)            # TX_INITIALIZE_IN_PROGRESS・TX_INITIALIZE_ALMOST_DONE
# tx_thread_state
(READY, COMPLETED, TERMINATED, SUSPENDED, SLEEP, QUEUE_SUSP, SEMAPHORE_SUSP, EVENT_FLAG, BLOCK_MEMORY, BYTE_MEMORY,
 IO_DRIVER, FILE, TCP_IP, MUTEX_SUSP, PRIORITY_CHANGE) = range(15)
# 待ちの種類 → (共通の言葉 WaitInfo.reason, 待つものの種類)
WAITS = {SLEEP: ("delay", None), QUEUE_SUSP: ("receive", "queue"), SEMAPHORE_SUSP: ("receive", "semaphore"),
         EVENT_FLAG: ("event", "event_flags"), BLOCK_MEMORY: ("receive", "block_pool"),
         BYTE_MEMORY: ("receive", "byte_pool"), MUTEX_SUSP: ("receive", "mutex"), IO_DRIVER: ("pend", "io_driver"),
         FILE: ("pend", "filex"), TCP_IP: ("pend", "netx")}


class ThreadX(KernelAwareness):
    name = "ThreadX"
    priority_order = -1           # 小さい方が高い（0 〜 TX_MAX_PRIORITIES - 1）

    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None, port: str = "auto"):
        super().__init__(info, access, symbols)
        self.thread = StructLayout.of(info, "TX_THREAD", "struct TX_THREAD_STRUCT")
        self.timer = StructLayout.of(info, "TX_TIMER_INTERNAL", "struct TX_TIMER_INTERNAL_STRUCT")
        self.layouts: dict[str, tuple[StructLayout, str]] = {}            # 種類 → (構造体, メンバーの接頭辞)
        for kind, (type_name, prefix) in KINDS.items():
            try:
                self.layouts[kind] = (StructLayout.of(info, type_name, f"struct {type_name}_STRUCT"), prefix)
            except KeyError:
                continue                                                  # アプリが使わない種類
        self.created_address = self.address("_tx_thread_created_ptr")
        self.port = ("armv6m" if arm_baseline(info.elf) else "armv7m") if port == "auto" else port
        if self.port not in EXC_RETURN_FIRST_LAYOUTS:
            raise ValueError(f"port の積み方 {port!r} は扱わない（{', '.join(EXC_RETURN_FIRST_LAYOUTS)}）")

    @staticmethod
    def detect(info: DebugInfo) -> bool:
        try:
            info.global_variable("_tx_thread_created_ptr")
            info.global_variable("_tx_thread_current_ptr")
            return StructLayout.of(info, "TX_THREAD", "struct TX_THREAD_STRUCT").has("tx_thread_created_next")
        except KeyError:
            return False

    # --- 読み --------------------------------------------------------------------------------------

    def _field(self, handle: int, path: str) -> int:
        return self.mem.uint(handle + self.thread.offset(path), self.thread.member_size(path))

    def _variable(self, name: str) -> int | None:
        address = self.optional_address(name)
        return None if address is None else self.mem.u32(address)

    def object_kind(self, address: int) -> str | None:
        """制御ブロックの先頭の id → 種類（mutex・queue 等。スレッド・分からない id は None）。"""
        return IDS.get(self.mem.u32(address))

    def object_name(self, address: int, kind: str) -> str | None:
        """オブジェクトの名前（*_name、const char *）。無ければ None。"""
        if kind == "thread":
            offset = self.thread.offset("tx_thread_name")
        elif kind in self.layouts:
            layout, prefix = self.layouts[kind]
            offset = layout.offset(f"{prefix}_name")
        else:
            return None
        pointer = self.mem.u32(address + offset)
        if not pointer:
            return None
        try:
            return self.mem.cstring(pointer, NAME_LIMIT) or None
        except AccessError:
            return None

    def object_label(self, address: int, kind: str) -> str:
        """オブジェクトの名前: 自分の名前、無ければ変数（シンボル）、無ければ番地。"""
        name = self.object_name(address, kind)
        if name:
            return name
        if self.symbols is not None:
            hit = self.symbols.symbol_at(address)
            if hit is not None and hit[1] == 0 and hit[0].kind == "object":
                return hit[0].name
        return f"0x{address:08X}"

    def thread_name(self, handle: int) -> str:
        return self.object_name(handle, "thread") or f"0x{handle:08X}"

    handle_name = thread_name

    def current_address(self) -> int:
        """_tx_thread_current_ptr の番地。"""
        return self.address("_tx_thread_current_ptr")

    def tick_count(self) -> int | None:
        return self._variable("_tx_timer_system_clock")

    def scheduler_state(self) -> str:
        state = self._variable("_tx_thread_system_state")
        if state is None:
            return "unknown"
        if state in INITIALIZING:
            return "not_started"
        return "suspended" if self._variable("_tx_thread_preempt_disable") else "running"

    def version(self) -> str | None:
        address = self.optional_address("_tx_version_id")
        if address is None:
            return None
        try:
            return self.mem.cstring(address, VERSION_LIMIT) or None
        except AccessError:
            return None

    # --- 環 ----------------------------------------------------------------------------------------

    def ring(self, head: int, link: int, what: str, ident: int | None = None, count: int | None = None) -> list[int]:
        """head から link（*_created_next・tx_thread_suspended_next のバイト位置）を辿った環。元に戻らない・多すぎる
        ときは止めて warnings に書く。ident があれば先頭の語がそれでないものを外す（警告）。count は期待する数。"""
        out: list[int] = []
        node, seen = head, set()
        while node:
            if node in seen or len(seen) >= MAX_ITEMS:
                self.warnings.append(f"{what} の環が壊れている（{len(seen)} 個で止めた）")
                return out
            seen.add(node)
            if ident is not None and self.mem.u32(node) != ident:
                self.warnings.append(f"{what} の 0x{node:08X} は id が違う（0x{self.mem.u32(node):08X}）")
            else:
                out.append(node)
            node = self.mem.u32(node + link)
            if node == head:
                break
        if count is not None and len(seen) != count:
            self.warnings.append(f"{what} の数が合わない（数 {count}、環 {len(seen)}）")
        return out

    def threads(self) -> list[int]:
        return self.ring(self.mem.u32(self.created_address), self.thread.offset("tx_thread_created_next"),
                         "_tx_thread_created_ptr", TX_THREAD_ID, self._variable("_tx_thread_created_count"))

    def timer_remaining(self, internal: int) -> int | None:
        """内部のタイマ（TX_TIMER_INTERNAL）の残りのティック（動いていなければ None）。tx_timer_info_get.c と同じ:
        時間の輪の枠と今の枠の差 + 1、32 ティックより先なら残り（remaining_ticks − 枠の数）を足す。"""
        t = self.timer
        head = self.mem.u32(internal + t.offset("tx_timer_internal_list_head"))
        if not head:
            return None
        remaining = self.mem.u32(internal + t.offset("tx_timer_internal_remaining_ticks"))
        start, end = self._variable("_tx_timer_list_start"), self._variable("_tx_timer_list_end")
        current = self._variable("_tx_timer_current_ptr")
        if start is None or end is None or current is None or end <= start:
            return None
        entries = (end - start) // 4
        if start <= head < end:
            if head >= current:
                left = (head - current) // 4 + 1
            else:
                left = (head - start) // 4 + (end - current) // 4 + 1
            return left + (remaining - entries if remaining > entries else 0)
        if self._variable("_tx_timer_expired_timer_ptr") == internal:
            return 0                                                      # いま時間切れの処理中
        return remaining - entries if remaining > entries else 0          # 時間切れの一時の列にある

    # --- スレッド ----------------------------------------------------------------------------------

    def tasks(self) -> list[Task]:
        self.warnings = []
        current = self.current()
        tick = self.tick_count()
        tasks = [self._task(h, current, tick) for h in self.threads()]
        self._names = {t.handle: t.name for t in tasks}
        return tasks

    def _task(self, handle: int, current: int, tick: int | None) -> Task:
        state_value = self._field(handle, "tx_thread_state")
        wait = None
        notes: list[str] = []
        running = handle == current and state_value == READY
        if state_value == READY:
            state = TaskState.RUNNING if running else TaskState.READY
        elif state_value == PRIORITY_CHANGE:
            state = TaskState.READY                                       # 優先度の変更の途中（カーネルの中の一瞬）
            notes.append("優先度を変える途中（TX_PRIORITY_CHANGE）")
        elif state_value == SUSPENDED:
            state = TaskState.SUSPENDED
            wait = WaitInfo("suspended")
        elif state_value in (COMPLETED, TERMINATED):
            state = TaskState.DELETED                                     # 入口から戻った・tx_thread_terminate
        else:
            state = TaskState.BLOCKED
            wait = self._wait(handle, state_value, tick)
        if self._field(handle, "tx_thread_delayed_suspend"):
            notes.append("待ちが終わると止まる（tx_thread_suspend を待ちの間に受けた）")
        stack = self._stack(handle, running)
        if stack.overflow:
            notes.append(f"スタックの底（tx_thread_stack_start）が塗り（0x{FILL_BYTE:02X}）のままでないか、SP が底より下")
        return Task(handle, self.thread_name(handle), self._field(handle, "tx_thread_priority"), state, stack, wait,
                    self._field(handle, "tx_thread_user_priority"), notes=notes)

    def _wait(self, handle: int, state_value: int, tick: int | None) -> WaitInfo:
        reason, kind = WAITS.get(state_value, ("unknown", None))
        wait = WaitInfo(reason, kind=kind)
        remaining = self.timer_remaining(handle + self.thread.offset("tx_thread_timer"))
        if remaining is not None:
            wait.remaining_ticks = remaining
            wait.wake_tick = None if tick is None else (tick + remaining) & 0xFFFF_FFFF
        if state_value == SLEEP:
            return wait
        block = self._field(handle, "tx_thread_suspend_control_block")
        if not block:
            return wait
        wait.object = wait.list = block
        found = self.object_kind(block)
        if found is not None:
            wait.kind = found
        wait.name = self.object_label(block, wait.kind or "")
        if wait.kind == "queue" and "queue" in self.layouts:
            layout, prefix = self.layouts["queue"]
            if self.mem.u32(block + layout.offset(f"{prefix}_enqueued")):
                wait.reason = "send"                                      # 空でない（満杯）キューの待ち手は送り手
        elif wait.kind == "mutex" and "mutex" in self.layouts:
            layout, prefix = self.layouts["mutex"]
            holder = self.mem.u32(block + layout.offset(f"{prefix}_owner"))
            wait.holder = self.thread_name(holder) if holder else None
        return wait

    def _stack(self, handle: int, running: bool) -> StackInfo:
        base = self._field(handle, "tx_thread_stack_start")
        last = self._field(handle, "tx_thread_stack_end")
        end = last + 1 if last else (base + self._field(handle, "tx_thread_stack_size") if base else None)
        if running:
            try:
                pointer: int | None = self.access.read_register("psp")
            except AccessError:
                pointer = None
        else:
            pointer = self._field(handle, "tx_thread_stack_ptr")
        if not base:
            return StackInfo(0, None, pointer)
        fill = FILL_BYTE
        if self.thread.has("tx_thread_stack_fill_value"):                 # TX_ENABLE_RANDOM_NUMBER_STACK_FILLING
            fill = self._field(handle, "tx_thread_stack_fill_value") & 0xFF
        return check_stack(self.access.read_memory, base, end, pointer, fill)

    # --- 呼び出し履歴 ------------------------------------------------------------------------------

    def saved_context(self, key: Task | str | int) -> SavedContext | None:
        task = self.task(key)
        if task.state is TaskState.RUNNING:
            return None
        return read_exc_return_first_context(self.access.read_memory, self._field(task.handle, "tx_thread_stack_ptr"),
                                             self.port)

    # --- カーネルのオブジェクト ------------------------------------------------------------------------

    def objects(self, kinds: list[str] | None = None, tasks: list[Task] | None = None) -> list[KernelObject]:
        """mutex・semaphore・queue・event_flags・block_pool・byte_pool・timer（threadx_objects）。"""
        tasks = self.tasks() if tasks is None else tasks
        return self.select(ThreadXObjects(self).objects(tasks), kinds)

    def handle_priority(self, handle: int) -> int:
        return self._field(handle, "tx_thread_user_priority")

    def saved_sp_address(self, handle: int) -> int:
        return handle + self.thread.offset("tx_thread_stack_ptr")

    def mutex_words(self, obj: KernelObject) -> tuple[int, int, int] | None:
        """持ち主 = tx_mutex_owner、待ちの先頭 = tx_mutex_suspension_list（スレッドそのものを指すので差は 0）。"""
        if obj.kind != "mutex" or "mutex" not in self.layouts:
            return None
        layout, prefix = self.layouts["mutex"]
        return (obj.handle + layout.offset(f"{prefix}_owner"), obj.handle + layout.offset(f"{prefix}_suspension_list"),
                0)

    def mutex_empty_head(self, obj: KernelObject) -> int:
        return 0

    def snapshot(self) -> dict:
        tasks = self.tasks()
        current = self.current()
        return {"rtos": self.name, "version": self.version(), "port": self.port, "scheduler": self.scheduler_state(),
                "tick": self.tick_count(), "current": self._names.get(current) if current else None,
                "tasks": [t.to_dict() for t in tasks], "warnings": list(self.warnings)}
