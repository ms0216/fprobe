"""FreeRTOS の認識（止まったターゲット）: タスクの一覧・状態・待っているもの・スタック、タスクごとの呼び出し履歴。

仕様: docs/superpowers/specs/2026-10-17-f6-rtos-design.md の 3・4 節。

    rtos = FreeRTOS(DebugInfo("app.elf"), access, symbols=Symbols("app.elf"))
    rtos.tasks()                              # [Task(name, priority, state, stack, wait, ...)]
    rtos.backtrace("worker", variables=True)  # タスクの保存された文脈から、F2 の CFI で巻き戻す
    rtos.inspector("worker").read("depth", frame=2)

構造体の位置は DWARF から引く（設定で TCB の形が変わっても読める）。ターゲットが動いたら作り直す（リストは
止まっている間しか一貫しない）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError, TargetAccess
from ..dwarf.info import DebugInfo
from ..dwarf.typesys import ArrayType, strip
from .base import NAME_LIMIT, KernelAwareness, stopped_note
from .cortexm import LAYOUTS, SavedContext, read_saved_context
from .freertos_objects import FreeRTOSObjects
from .model import HeapInfo, KernelObject, StackInfo, Task, TaskState, WaitInfo, field_location
from .stackcheck import check_stack
from .structs import StructLayout

__all__ = ["FILL_BYTE", "FreeRTOS"]

FILL_BYTE = 0xA5                  # tskSTACK_FILL_BYTE
MAX_LIST_ITEMS = 256              # 壊れたリストでも止まる
WAITING_NOTIFICATION = 1          # taskWAITING_NOTIFICATION
QUEUE_KINDS = {0: "queue", 1: "mutex", 2: "counting_semaphore", 3: "binary_semaphore", 4: "recursive_mutex"}
MUTEXES = ("mutex", "recursive_mutex")
_DELAYED = ("xDelayedTaskList1", "xDelayedTaskList2")
_REQUIRED = ("pxCurrentTCB", "pxReadyTasksLists")


class FreeRTOS(KernelAwareness):
    name = "FreeRTOS"

    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None, port: str | None = None,
                 name_limit: int = NAME_LIMIT):
        """port: 保存された文脈の積み方（"armv7m" = ARM_CM3 等、"armv7m_fp" = ARM_CM4F・ARM_CM7）。省けば ELF の事実
        だけで決める: ARM_CM4F・ARM_CM7 の port だけが定める関数 vPortEnableVFP があれば armv7m_fp（port_basis =
        "vPortEnableVFP"）。無いことからは決めない（port = None。一覧・状態・スタックは読め、保存された文脈は
        誤りで port を渡すよう言う）。明示すれば port_basis = "explicit"。"""
        super().__init__(info, access, symbols, name_limit)
        self.tcb = StructLayout.of(info, "TCB_t", "struct tskTaskControlBlock")
        self.list = StructLayout.of(info, "List_t", "struct xLIST")
        self.item = StructLayout.of(info, "ListItem_t", "struct xLIST_ITEM")
        try:
            self.queue: StructLayout | None = StructLayout.of(info, "Queue_t", "struct QueueDefinition")
        except KeyError:
            self.queue = None                        # queue.c が無い（キューを使わない）
        for name in _REQUIRED:
            self.address(name)                       # 無ければ KeyError（分かりやすい誤り）
        if port is None:
            self.port, self.port_basis = self._port_from_elf()
        else:
            if port not in LAYOUTS:
                raise ValueError(f"port の積み方 {port!r} は扱わない（{', '.join(LAYOUTS)}）")
            self.port, self.port_basis = port, "explicit"
        self.tick_mask = (1 << (8 * self.item.member_size("xItemValue"))) - 1

    @staticmethod
    def detect(info: DebugInfo) -> bool:
        try:
            for name in _REQUIRED:
                info.global_variable(name)
            StructLayout.of(info, "TCB_t", "struct tskTaskControlBlock")
        except KeyError:
            return False
        return True

    def _port_from_elf(self) -> tuple[str | None, str | None]:
        """(port, 根拠)。ARM_CM4F・ARM_CM7 の port にだけ vPortEnableVFP がある（FP の文脈を EXC_RETURN と一緒に積む）。
        無いことは ARM_CM3 の証拠にならない（別の port・名前の違う実装もある）ので (None, None)。"""
        if self.function_address("vPortEnableVFP") is not None:
            return "armv7m_fp", "vPortEnableVFP"
        return None, None

    # --- カーネルの変数 -----------------------------------------------------------------------

    def current_address(self) -> int:
        """pxCurrentTCB（実行中のタスクの TCB）の番地。"""
        return self.address("pxCurrentTCB")

    def tick_count(self) -> int | None:
        address = self.optional_address("xTickCount")
        return None if address is None else self.mem.uint(address, self.item.member_size("xItemValue"))

    def scheduler_state(self) -> str:
        """running / not_started / suspended（分からなければ unknown）。"""
        running, suspended = self.optional_address("xSchedulerRunning"), self.optional_address("uxSchedulerSuspended")
        if running is None:
            return "unknown"
        if not self.mem.u32(running):
            return "not_started"
        if suspended is not None and self.mem.u32(suspended):
            return "suspended"
        return "running"

    def ready_lists(self) -> list[int]:
        var = self.info.global_variable("pxReadyTasksLists")
        t = strip(var.type)
        count = t.length if isinstance(t, ArrayType) and t.length else 1
        base = self.address("pxReadyTasksLists")
        return [base + i * self.list.size for i in range(count)]

    # --- リスト ------------------------------------------------------------------------------

    def walk(self, lst: int) -> list[tuple[int, int]]:
        """リストの (項目の番地, 持ち主) を先頭から。壊れていれば（巡回・数の超過）そこで止め、warnings に書く。"""
        end = lst + self.list.offset("xListEnd")
        count = self.mem.u32(lst + self.list.offset("uxNumberOfItems"))
        out: list[tuple[int, int]] = []
        seen: set[int] = set()
        item = self.mem.u32(end + self.item.offset("pxNext"))
        while item != end:
            if item in seen or len(out) >= min(count, MAX_LIST_ITEMS) or item == 0:
                reason = ("cycle" if item in seen else "null" if item == 0
                          else "over_count" if len(out) >= count else "limit")
                self.warnings.append(stopped_note(f"リスト 0x{lst:08X}", len(out), reason,
                                                  count if reason == "over_count" else MAX_LIST_ITEMS))
                break
            seen.add(item)
            out.append((item, self.mem.u32(item + self.item.offset("pvOwner"))))
            item = self.mem.u32(item + self.item.offset("pxNext"))
        return out

    # --- タスク --------------------------------------------------------------------------------

    def tasks(self) -> list[Task]:
        self.warnings = []
        current = self.current()
        scheduler = self.scheduler_state()
        where: dict[int, str] = {}

        def add(handle: int, place: str) -> None:
            if handle and handle not in where:
                where[handle] = place
        for lst in self.ready_lists():
            for _, handle in self.walk(lst):
                add(handle, "ready")
        for name in _DELAYED:
            lst = self.optional_address(name)
            if lst is not None:
                for _, handle in self.walk(lst):
                    add(handle, "delayed")
        pending = self.optional_address("xPendingReadyList")
        if pending is not None:
            for _, handle in self.walk(pending):
                where[handle] = "pending"                         # 項目は xEventListItem。状態のリストより優先
        for name, place in (("xSuspendedTaskList", "suspended"), ("xTasksWaitingTermination", "deleted")):
            lst = self.optional_address(name)
            if lst is not None:
                for _, handle in self.walk(lst):
                    add(handle, place)
        add(current, "current")
        tick = self.tick_count()
        tasks = [self._task(handle, place, handle == current and scheduler != "not_started", tick)
                 for handle, place in where.items()]
        self._names = {t.handle: t.name for t in tasks}
        if self.tcb.has("uxTCBNumber"):
            tasks.sort(key=lambda t: t.number)
        return tasks

    def _field(self, handle: int, path: str) -> int:
        return self.mem.uint(handle + self.tcb.offset(path), self.tcb.member_size(path))

    def _optional_field(self, handle: int, path: str) -> int | None:
        return self._field(handle, path) if self.tcb.has(path) else None

    def task_name(self, handle: int) -> str:
        return self.mem.cstring(handle + self.tcb.offset("pcTaskName"), self.tcb.member_size("pcTaskName"), whole=True)

    handle_name = task_name

    def _task(self, handle: int, place: str, running: bool, tick: int | None) -> Task:
        name = self.task_name(handle)
        notes: list[str] = []
        event_list = self._field(handle, "xEventListItem.pxContainer")
        waiting_notification = self._notification_waiting(handle)
        if place == "pending":
            notes.append("pending_ready")
        if running:
            state = TaskState.RUNNING
        elif place in ("ready", "pending", "current"):
            state = TaskState.READY
        elif place == "delayed":
            state = TaskState.BLOCKED
        elif place == "suspended":
            state = TaskState.BLOCKED if event_list or waiting_notification else TaskState.SUSPENDED
        else:
            state = TaskState.DELETED
        wait = None
        if state is TaskState.BLOCKED:
            wait = self._wait(handle, place, event_list, waiting_notification, tick)
        task = Task(handle, name, self._field(handle, "uxPriority"), state, self._stack(handle, running), wait,
                    self._optional_field(handle, "uxBasePriority"), self._optional_field(handle, "uxTCBNumber"), notes)
        task.locations["priority"] = field_location(self.tcb, handle, "uxPriority")
        if self.tcb.has("uxBasePriority"):
            task.locations["base_priority"] = field_location(self.tcb, handle, "uxBasePriority")
        return task

    def _notification_waiting(self, handle: int) -> bool:
        if not self.tcb.has("ucNotifyState"):
            return False
        n = self.tcb.member_size("ucNotifyState")
        return WAITING_NOTIFICATION in self.mem.read(handle + self.tcb.offset("ucNotifyState"), n)

    # --- 待っているもの ----------------------------------------------------------------------------

    def _wait(self, handle: int, place: str, event_list: int, notification: bool, tick: int | None) -> WaitInfo:
        if event_list:
            wait = self._event_object(event_list)
        elif notification:
            wait = WaitInfo("notification")
        else:
            wait = WaitInfo("delay")
        if place == "delayed":
            wait.wake_tick = self._field(handle, "xStateListItem.xItemValue")
            if tick is not None:
                wait.remaining_ticks = (wait.wake_tick - tick) & self.tick_mask
        return wait

    def registry(self) -> list[tuple[str, int]]:
        """キューの登録簿（configQUEUE_REGISTRY_SIZE）: [(名前, ハンドル)]。"""
        try:
            var = self.info.global_variable("xQueueRegistry")
        except KeyError:
            return []
        t = strip(var.type)
        if not isinstance(t, ArrayType) or not t.length:
            return []
        entry = StructLayout("QueueRegistryItem_t", t.element)
        base = self.address("xQueueRegistry")
        out = []
        for i in range(t.length):
            at = base + i * entry.size
            handle = self.mem.u32(at + entry.offset("xHandle"))
            name_ptr = self.mem.u32(at + entry.offset("pcQueueName"))
            if handle and name_ptr:
                try:
                    out.append((self.mem.cstring(name_ptr, self.name_limit), handle))
                except AccessError:
                    out.append((f"0x{handle:08X}", handle))
        return out

    def _event_object(self, event_list: int) -> WaitInfo:
        if self.queue is None:
            return WaitInfo("event", list=event_list)
        receive = self.queue.offset("xTasksWaitingToReceive")
        send = self.queue.offset("xTasksWaitingToSend")
        found: tuple[int, str, str] | None = None
        for name, handle in self.registry():
            if event_list in (handle + receive, handle + send):
                found = (handle, name, "receive" if event_list == handle + receive else "send")
                break
        if found is None and self.symbols is not None:
            hit = self.symbols.symbol_at(event_list)
            if hit is not None and hit[0].kind == "object" and hit[1] in (receive, send):
                symbol, offset = hit
                found = (symbol.address, symbol.name, "receive" if offset == receive else "send")
        if found is None:
            return WaitInfo("event", list=event_list)
        handle, name, reason = found
        kind = self._queue_kind(handle)
        wait = WaitInfo(reason, object=handle, name=name, kind=kind, list=event_list)
        if kind in MUTEXES and self.queue.has("u.xSemaphore.xMutexHolder"):
            holder = self.mem.u32(handle + self.queue.offset("u.xSemaphore.xMutexHolder"))
            if holder:
                wait.holder = self.task_name(holder)
        return wait

    def _queue_kind(self, handle: int) -> str:
        q = self.queue
        if q.has("ucQueueType"):
            return QUEUE_KINDS.get(self.mem.u8(handle + q.offset("ucQueueType")), "queue")
        if self.mem.u32(handle + q.offset("pcHead")) == 0:
            return "mutex"                                     # queueQUEUE_IS_MUTEX
        if self.mem.u32(handle + q.offset("uxItemSize")) == 0:
            return "semaphore"
        return "queue"

    # --- スタック -------------------------------------------------------------------------------

    def _stack(self, handle: int, running: bool) -> StackInfo:
        base = self._field(handle, "pxStack")
        end_word = self._optional_field(handle, "pxEndOfStack")
        end = None if end_word is None else end_word + 4
        if running:
            try:
                pointer: int | None = self.access.read_register("psp")
            except AccessError:
                pointer = None                                 # 動いている: いまの SP は分からない
        else:
            pointer = self._field(handle, "pxTopOfStack")
        return check_stack(self.access.read_memory, base, end, pointer, FILL_BYTE)

    # --- タスクごとの呼び出し履歴 -----------------------------------------------------------------

    def saved_context(self, key: Task | str | int) -> SavedContext | None:
        task = self.task(key)
        if task.state is TaskState.RUNNING:
            return None
        if self.port is None:
            raise ValueError("FreeRTOS の port の積み方が ELF から決まらない（ARM_CM4F・ARM_CM7 の port の関数 "
                             "vPortEnableVFP が無い）。port を渡す（armv7m = ARM_CM3 等 / armv7m_fp = ARM_CM4F・ARM_CM7）")
        return read_saved_context(self.access.read_memory, self._field(task.handle, "pxTopOfStack"), self.port)

    def stop_pcs(self) -> list[int]:
        """タスクの関数の戻り先は prvTaskExitError（port が最初の文脈に積む罠）: そこから先は呼び出し元ではない。"""
        exit_trap = self.function_address("prvTaskExitError")
        return [] if exit_trap is None else [exit_trap]

    # --- カーネルのオブジェクト（U08） ----------------------------------------------------------------

    def objects(self, kinds: list[str] | None = None, tasks: list[Task] | None = None) -> list[KernelObject]:
        """キュー・セマフォ・ミューテックス・タイマ・イベントグループ・ストリーム / メッセージバッファ（freertos_objects）。"""
        tasks = self.tasks() if tasks is None else tasks
        return self.select(FreeRTOSObjects(self).objects(tasks), kinds)

    def heap(self) -> HeapInfo | None:
        return FreeRTOSObjects(self).heap()

    def handle_priority(self, handle: int) -> int:
        base = self._optional_field(handle, "uxBasePriority")
        return self._field(handle, "uxPriority") if base is None else base

    def heap_free_word(self) -> tuple[int, int | None] | None:
        address = self.optional_address("xFreeBytesRemaining")
        if address is None:
            return None
        heap = self.heap()
        return address, None if heap is None else heap.total

    def idle_handles(self) -> set[int]:
        """xIdleTaskHandles（V11、コアごと）か xIdleTaskHandle（それより前）。"""
        for name in ("xIdleTaskHandles", "xIdleTaskHandle"):
            address = self.optional_address(name)
            if address is None:
                continue
            t = strip(self.info.global_variable(name).type)
            count = t.length if isinstance(t, ArrayType) and t.length else 1
            return {h for h in (self.mem.u32(address + 4 * i) for i in range(count)) if h}
        return set()

    def saved_sp_address(self, handle: int) -> int:
        return handle + self.tcb.offset("pxTopOfStack")

    def mutex_words(self, obj: KernelObject) -> tuple[int, int, int] | None:
        """持ち主 = u.xSemaphore.xMutexHolder、待ちの先頭 = xTasksWaitingToReceive の xListEnd.pxNext（優先度順に
        並ぶので最も高い待ち手の xEventListItem の番地。TCB = 値 − xEventListItem の位置）。"""
        q = self.queue
        if q is None or obj.kind not in MUTEXES or not q.has("u.xSemaphore.xMutexHolder"):
            return None
        head = obj.handle + q.offset("xTasksWaitingToReceive") + self.list.offset("xListEnd") + self.item.offset("pxNext")
        return (obj.handle + q.offset("u.xSemaphore.xMutexHolder"), head, self.tcb.offset("xEventListItem"))

    def mutex_empty_head(self, obj: KernelObject) -> int:
        return obj.handle + self.queue.offset("xTasksWaitingToReceive") + self.list.offset("xListEnd")

    # --- まとめ ------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        tasks = self.tasks()
        current = self.current()
        return {"rtos": self.name, "scheduler": self.scheduler_state(), "tick": self.tick_count(),
                "current": self._names.get(current), "port": self.port, "port_basis": self.port_basis,
                "tasks": [t.to_dict() for t in tasks],
                "warnings": list(self.warnings)}
