"""µC/OS-III（Weston Embedded / 旧 Micrium）の認識（Cortex-M）: タスクの一覧・状態・待っているもの・スタック、タスクごとの
呼び出し履歴、カーネルのオブジェクト（ucos3_objects）。

仕様: docs/superpowers/specs/2026-10-19-u45-ucos3-design.md
構造体の位置は DWARF から（OS_TCB・OS_SEM 等。os.h の型。設定 os_cfg.h でメンバーが増減するので決め打ちしない）。
- タスクの一覧は OS_CFG_DBG_EN のときの OSTaskDbgListPtr（DbgNextPtr の列。数は OSTaskQty）。無ければ OS_TCB の変数・
  準備完了の列（OSRdyList[]）・時間の列（OSTickList）・実行中から集める（警告。動的に確保した TCB で、時間切れの無い待ち・
  停止中のものは漏れうる）。名前は NamePtr（DBG のとき）→ 変数 → 番地。
- 実行中は OSTCBCurPtr（状態が OS_TASK_STATE_RDY のとき）。
- 状態は TaskState のビット（DLY・PEND・SUSPENDED）。待つものは PendOn と PendObjPtr。止められた（SUSPENDED のビット）
  タスクは suspended で、遅延・待ちも重なっていればその待ちを wait に出す（notes に停止の入れ子の数）。
- 残りのティックは OSTickList の差分の列（TickRemain は前からの差。os_tick.c）の先頭からの和。
- 優先度は小さい方が高い（0 が最高）。元の優先度は BasePrio（ミューテックスの継承の前）。
- スタックは StkBasePtr から StkSize 語（CPU_STK）。OS_OPT_TASK_STK_CLR で作ったタスクは全体が 0（os_task.c）なので、
  0 の塗りの残りで最大の使用量、底が 0 のままでなければあふれ。消さないタスクは塗りが無い（fill = none）。
- 保存された文脈は StkPtr から: ARMv7-M の port（os_cpu_a.S）は r4〜r11、EXC_RETURN、[s16〜s31]、ハードのフレーム、
  ARMv6-M の port は r4〜r7、r8〜r11、EXC_RETURN、ハードのフレームで、どちらも cortexm の "armv7m_fp" と同じ並び。
"""

from __future__ import annotations

from ..dwarf.access import AccessError, TargetAccess
from ..dwarf.info import DebugInfo
from ..dwarf.typesys import PointerType, strip
from .base import NAME_LIMIT, KernelAwareness
from .cortexm import SavedContext, arm_baseline, read_saved_context
from .model import KernelObject, StackInfo, Task, TaskState, WaitInfo, field_location
from .stackcheck import check_stack
from .structs import StructLayout
from .ucos3_objects import KINDS, TYPE_IDS, UCOS3Objects

__all__ = ["UCOS3"]

OS_STATE_OS_RUNNING = 1
# TaskState のビットと値（os.h）
BIT_DLY, BIT_PEND, BIT_SUSPENDED = 0x1, 0x2, 0x4
STATE_RDY, STATE_DEL = 0, 255
OPT_TASK_STK_CLR = 0x0002
# µC/OS-III が os.h で定める、オブジェクト・タスクを指す変数（kernel_pointers。名前に使わない）
KERNEL_POINTERS = frozenset({"OSTCBCurPtr", "OSTCBHighRdyPtr", "OSTmrListPtr", "OSIntQInPtr", "OSIntQOutPtr",
                             "OSTaskDbgListPtr", "OSFlagDbgListPtr", "OSMemDbgListPtr", "OSMutexDbgListPtr",
                             "OSQDbgListPtr", "OSSemDbgListPtr", "OSTmrDbgListPtr"})
# PendOn（os.h の OS_TASK_PEND_ON_*）→ (共通の言葉 WaitInfo.reason, 待つものの種類)
PEND_ON = {1: ("event", "event_flags"), 2: ("receive", "task_queue"), 3: ("pend", "cond"), 4: ("receive", "mutex"),
           5: ("receive", "queue"), 6: ("receive", "semaphore"), 7: ("notification", "task_semaphore")}
OWN_WAITS = {"task_queue", "task_semaphore"}             # タスクに組み込みの待ち（待つオブジェクトは無い）


class UCOS3(KernelAwareness):
    name = "uC/OS-III"
    priority_order = -1           # 小さい方が高い（0 〜 OS_CFG_PRIO_MAX - 1）

    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None, name_limit: int = NAME_LIMIT):
        super().__init__(info, access, symbols, name_limit)
        self.tcb = StructLayout.of(info, "OS_TCB", "struct os_tcb")
        self.list = StructLayout.of(info, "OS_RDY_LIST", "struct os_rdy_list")
        self.layouts: dict[str, StructLayout] = {}
        for kind, (type_name, struct_name) in KINDS.items():
            try:
                self.layouts[kind] = StructLayout.of(info, type_name, struct_name)
            except KeyError:
                continue                                                  # 設定で外れた・使わない種類
        self.port = "armv6m" if arm_baseline(info.elf) else "armv7m"     # 積み方はどちらも armv7m_fp の並び
        self.debug_lists = self.optional_address("OSTaskDbgListPtr") is not None

    @staticmethod
    def detect(info: DebugInfo) -> bool:
        try:
            info.global_variable("OSTCBCurPtr")
            info.global_variable("OSRdyList")
            tcb = StructLayout.of(info, "OS_TCB", "struct os_tcb")
            return tcb.has("PendObjPtr") and tcb.has("StkPtr")
        except KeyError:
            return False

    # --- 読み --------------------------------------------------------------------------------------

    def _field(self, handle: int, path: str) -> int:
        return self.mem.uint(handle + self.tcb.offset(path), self.tcb.member_size(path))

    def _variable(self, name: str) -> int | None:
        """カーネルの変数の値（大きさは DWARF の型。OS_STATE は 1 バイト等）。無ければ None。"""
        try:
            var = self.info.global_variable(name)
        except KeyError:
            return None
        address = self.optional_address(name)
        if address is None:
            return None
        return self.mem.uint(address, var.type.size or 4)

    def string(self, pointer: int) -> str | None:
        if not pointer:
            return None
        try:
            return self.mem.cstring(pointer, self.name_limit) or None
        except AccessError:
            return None

    def symbol_name(self, address: int) -> str | None:
        """その番地から始まる変数の名前（無ければ None）。"""
        if self.symbols is not None:
            hit = self.symbols.symbol_at(address)
            if hit is not None and hit[1] == 0 and hit[0].kind == "object":
                return hit[0].name
        return None

    def object_kind(self, address: int) -> str | None:
        """オブジェクトの先頭の Type（"SEMA"・"MUTX" 等の 4 文字）→ 種類。Type が無い設定・分からなければ None。"""
        try:
            return TYPE_IDS.get(self.mem.read(address, 4))
        except AccessError:
            return None

    def object_name(self, address: int, kind: str) -> str | None:
        """オブジェクト・タスクの NamePtr（OS_CFG_DBG_EN のとき）。無ければ None。"""
        layout = self.tcb if kind == "task" else self.layouts.get(kind)
        if layout is None or not layout.has("NamePtr"):
            return None
        return self.string(self.mem.u32(address + layout.offset("NamePtr")))

    def object_label(self, address: int, kind: str) -> str:
        """オブジェクトの名前: 自分の名前、無ければ変数（シンボル）、無ければ番地。"""
        return self.object_name(address, kind) or self.symbol_name(address) or f"0x{address:08X}"

    def task_name(self, handle: int) -> str:
        return self.object_label(handle, "task")

    handle_name = task_name

    def current_address(self) -> int:
        """OSTCBCurPtr の番地。"""
        return self.address("OSTCBCurPtr")

    def tick_count(self) -> int | None:
        return self._variable("OSTickCtr")

    def scheduler_state(self) -> str:
        running = self._variable("OSRunning")
        if running is None:
            return "unknown"
        if running != OS_STATE_OS_RUNNING:
            return "not_started"
        return "suspended" if self._variable("OSSchedLockNestingCtr") else "running"

    def version(self) -> str | None:
        """OSDbg_VersionNbr（os_dbg.c。OS_CFG_DBG_EN のとき。30802 → "3.08.02"）。"""
        number = self._variable("OSDbg_VersionNbr")
        if not number:
            return None
        return f"{number // 10000}.{number // 100 % 100:02d}.{number % 100:02d}"

    # --- 列 ----------------------------------------------------------------------------------------

    def tick_list(self) -> dict[int, int]:
        """OSTickList の差分の列 → タスク → 残りのティック（先頭からの TickRemain の和）。"""
        if not self.tcb.has("TickNextPtr"):
            return {}
        head_address = self.optional_address("OSTickList")
        if head_address is None:
            return {}
        out: dict[int, int] = {}
        total = 0
        for tcb in self.chain(self.mem.u32(head_address), self.tcb.offset("TickNextPtr"), "OSTickList"):
            total += self._field(tcb, "TickRemain")
            out[tcb] = total
        return out

    def handles(self) -> list[int]:
        """タスクの TCB の番地。OS_CFG_DBG_EN なら一覧の列、無ければ集める（警告）。"""
        if self.debug_lists:
            return self.chain(self._variable("OSTaskDbgListPtr") or 0, self.tcb.offset("DbgNextPtr"),
                              "OSTaskDbgListPtr", self._variable("OSTaskQty"))
        self.warnings.append("OS_CFG_DBG_EN が 0: タスクの一覧が無いので、OS_TCB の変数・準備完了の列・時間の列から集めた"
                             "（動的に確保した TCB で、時間切れの無い待ち・停止中のものは漏れうる）")
        found: set[int] = set()
        current = self.current()
        if current:
            found.add(current)
        rdy = self.optional_address("OSRdyList")
        if rdy is not None:
            entries = (self.info.global_variable("OSRdyList").type.size or 0) // max(self.list.size, 1)
            for prio in range(entries):
                found.update(self.chain(self.mem.u32(rdy + prio * self.list.size + self.list.offset("HeadPtr")),
                                        self.tcb.offset("NextPtr"), f"OSRdyList[{prio}]"))
        found.update(self.tick_list())
        for f in self.typed_objects({"OS_TCB": "task", "os_tcb": "task"}, exclude=self.kernel_pointers()):
            found.add(f.address)
        return sorted(found)

    def kernel_pointers(self) -> frozenset[str]:
        """カーネルの、オブジェクト・タスクを指す変数のうち ELF にあるもの。名前に使わない。µC/OS-III が os.h で
        定める名前（KERNEL_POINTERS）だけで、名前の形（OS で始まり Ptr で終わる等）では決めない。"""
        return frozenset(v.name for v in self.info.global_variables() if v.name in KERNEL_POINTERS)

    # --- タスク ------------------------------------------------------------------------------------

    def tasks(self) -> list[Task]:
        self.warnings = []
        current = self.current()
        tick = self.tick_count()
        remaining = self.tick_list()
        tasks = [self._task(h, current, tick, remaining) for h in self.handles()]
        self._names = {t.handle: t.name for t in tasks}
        return tasks

    def _task(self, handle: int, current: int, tick: int | None, remaining: dict[int, int]) -> Task:
        state_value = self._field(handle, "TaskState")
        wait = None
        notes: list[str] = []
        running = handle == current and state_value == STATE_RDY
        if state_value == STATE_RDY:
            state = TaskState.RUNNING if running else TaskState.READY
        elif state_value == STATE_DEL:
            state = TaskState.DELETED
        else:
            state = TaskState.SUSPENDED if state_value & BIT_SUSPENDED else TaskState.BLOCKED
            if state_value & (BIT_DLY | BIT_PEND):
                wait = self._wait(handle, state_value, tick, remaining)
            else:
                wait = WaitInfo("suspended")
            if state_value & BIT_SUSPENDED and self.tcb.has("SuspendCtr"):
                notes.append(f"OSTaskSuspend で止められている（入れ子 {self._field(handle, 'SuspendCtr')}）")
        stack = self._stack(handle, running)
        if stack.overflow:
            notes.append("SP がスタックの底（StkBasePtr）より下")
        base = self._field(handle, "BasePrio") if self.tcb.has("BasePrio") else None
        task = Task(handle, self.task_name(handle), self._field(handle, "Prio"), state, stack, wait, base, notes=notes)
        task.locations["priority"] = field_location(self.tcb, handle, "Prio")
        if base is not None:
            task.locations["base_priority"] = field_location(self.tcb, handle, "BasePrio")
        return task

    def _wait(self, handle: int, state_value: int, tick: int | None, remaining: dict[int, int]) -> WaitInfo:
        if state_value & BIT_PEND:
            reason, kind = PEND_ON.get(self._field(handle, "PendOn"), ("pend", None))
        else:
            reason, kind = "delay", None
        wait = WaitInfo(reason, kind=kind)
        if state_value & BIT_DLY and handle in remaining:
            wait.remaining_ticks = remaining[handle]
            wait.wake_tick = None if tick is None else (tick + remaining[handle]) & 0xFFFF_FFFF
        if not state_value & BIT_PEND or kind in OWN_WAITS:
            return wait
        block = self._field(handle, "PendObjPtr")
        if not block:
            return wait
        wait.object = wait.list = block
        found = self.object_kind(block)
        if found is not None:
            wait.kind = found
        wait.name = self.object_label(block, wait.kind or "")
        if wait.kind == "mutex" and "mutex" in self.layouts:
            holder = self.mem.u32(block + self.layouts["mutex"].offset("OwnerTCBPtr"))
            wait.holder = self.task_name(holder) if holder else None
        return wait

    def _stack(self, handle: int, running: bool) -> StackInfo:
        if running:
            try:
                pointer: int | None = self.access.read_register("psp")
            except AccessError:
                pointer = None
        else:
            pointer = self._field(handle, "StkPtr")
        if not self.tcb.has("StkBasePtr") or not self.tcb.has("StkSize"):
            return StackInfo(0, None, pointer)                            # 設定で外れている（DBG・STK_CHK・REDZONE 無し）
        base = self._field(handle, "StkBasePtr")
        if not base:
            return StackInfo(0, None, pointer)
        end = base + self._field(handle, "StkSize") * self._stack_word()
        if not self._field(handle, "Opt") & OPT_TASK_STK_CLR:
            info = StackInfo(base, end, pointer, fill="none")             # 消していない: 塗りから使用量は分からない
            info.overflow = pointer is not None and pointer < base
            return info
        return check_stack(self.access.read_memory, base, end, pointer, 0)

    def _stack_word(self) -> int:
        """CPU_STK の大きさ（StkBasePtr が指す型）。"""
        t = strip(self.tcb.member_type("StkBasePtr"))
        return (strip(t.target).size if isinstance(t, PointerType) else None) or 4

    # --- 呼び出し履歴 ------------------------------------------------------------------------------

    def saved_context(self, key: Task | str | int) -> SavedContext | None:
        task = self.task(key)
        if task.state is TaskState.RUNNING:
            return None
        return read_saved_context(self.access.read_memory, self._field(task.handle, "StkPtr"), "armv7m_fp")

    def stop_pcs(self) -> list[int]:
        """タスクの関数の戻り先は OS_TaskReturn（OSTaskStkInit が LR に置く）: そこから先は呼び出し元ではない。"""
        address = self.function_address("OS_TaskReturn")
        return [] if address is None else [address & ~1]

    def idle_handles(self) -> set[int]:
        idle = self.optional_address("OSIdleTaskTCB")
        return set() if idle is None else {idle}

    # --- カーネルのオブジェクト ------------------------------------------------------------------------

    def objects(self, kinds: list[str] | None = None, tasks: list[Task] | None = None) -> list[KernelObject]:
        """semaphore・mutex・queue・event_flags・memory_pool・timer・cond（ucos3_objects）。"""
        tasks = self.tasks() if tasks is None else tasks
        return self.select(UCOS3Objects(self).objects(tasks), kinds)

    def handle_priority(self, handle: int) -> int:
        return self._field(handle, "BasePrio" if self.tcb.has("BasePrio") else "Prio")

    def saved_sp_address(self, handle: int) -> int:
        return handle + self.tcb.offset("StkPtr")

    def mutex_words(self, obj: KernelObject) -> tuple[int, int, int] | None:
        """持ち主 = OwnerTCBPtr、待ちの先頭 = PendList.HeadPtr（優先度順。TCB そのものを指すので差は 0）。"""
        if obj.kind != "mutex" or "mutex" not in self.layouts:
            return None
        layout = self.layouts["mutex"]
        return obj.handle + layout.offset("OwnerTCBPtr"), obj.handle + layout.offset("PendList.HeadPtr"), 0

    def mutex_empty_head(self, obj: KernelObject) -> int:
        return 0

    def snapshot(self) -> dict:
        tasks = self.tasks()
        current = self.current()
        return {"rtos": self.name, "version": self.version(), "port": self.port, "scheduler": self.scheduler_state(),
                "tick": self.tick_count(), "current": self._names.get(current) if current else None,
                "tasks": [t.to_dict() for t in tasks], "warnings": list(self.warnings)}
