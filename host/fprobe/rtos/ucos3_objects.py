"""µC/OS-III のカーネルオブジェクト: semaphore・mutex・queue・event_flags・memory_pool・timer・cond。

仕様: docs/superpowers/specs/2026-10-19-u45-ucos3-design.md
見つけ方は 3 つを合わせる（同じ番地は 1 つ）:
- OS_CFG_DBG_EN のときの種類ごとの一覧（OSSemDbgListPtr 等、DbgNextPtr の列。数は OSSemQty 等）
- 型がオブジェクトの構造体か、それを指すポインタの大域・ファイルの static 変数（OS_CFG_DBG_EN が 0 でも見つかる）
- タスクが待っているオブジェクト（PendObjPtr）
先頭の Type（"SEMA"・"MUTX" 等。OS_CFG_OBJ_TYPE_CHK_EN か DBG のとき）が種類と合わないものは、一覧のものは警告して外し、
変数のもの（まだ作っていない・消した）は黙って外す。名前は NamePtr（source "kernel"。変数の名前は aliases）→ 変数 → 番地。
待ち手は PendList（HeadPtr から PendNextPtr。優先度順）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError
from .model import KernelObject, Task, Waiter
from .objects import typed_variables
from .structs import StructLayout

__all__ = ["KINDS", "TYPE_IDS", "UCOS3Objects"]

# 種類 → (typedef の名前, 構造体の名前)
KINDS = {"semaphore": ("OS_SEM", "struct os_sem"), "mutex": ("OS_MUTEX", "struct os_mutex"),
         "queue": ("OS_Q", "struct os_q"), "event_flags": ("OS_FLAG_GRP", "struct os_flag_grp"),
         "memory_pool": ("OS_MEM", "struct os_mem"), "timer": ("OS_TMR", "struct os_tmr"),
         "cond": ("OS_COND", "struct os_cond")}
# 先頭の Type（CPU_TYPE_CREATE。リトルエンディアンでメモリに文字の順に並ぶ）→ 種類
TYPE_IDS = {b"SEMA": "semaphore", b"MUTX": "mutex", b"QUEU": "queue", b"FLAG": "event_flags", b"MEM ": "memory_pool",
            b"TMR ": "timer", b"COND": "cond"}
KIND_TYPES = {kind: ident for ident, kind in TYPE_IDS.items()}
# OS_CFG_DBG_EN のときの一覧の変数と数の変数
DEBUG_LISTS = {"semaphore": ("OSSemDbgListPtr", "OSSemQty"), "mutex": ("OSMutexDbgListPtr", "OSMutexQty"),
               "queue": ("OSQDbgListPtr", "OSQQty"), "event_flags": ("OSFlagDbgListPtr", "OSFlagQty"),
               "memory_pool": ("OSMemDbgListPtr", "OSMemQty"), "timer": ("OSTmrDbgListPtr", "OSTmrQty")}
# OS_OPT_PEND_FLAG_*（os.h）
FLAG_CLR_ALL, FLAG_CLR_ANY, FLAG_SET_ALL, FLAG_SET_ANY, FLAG_CONSUME = 0x1, 0x2, 0x4, 0x8, 0x100
OPT_TMR_PERIODIC = 2
TMR_STATES = {0: "unused", 1: "stopped", 2: "running", 3: "completed", 4: "timeout"}
TMR_RUNNING = 2


class UCOS3Objects:
    def __init__(self, rtos):
        self.rtos = rtos
        self.mem = rtos.mem

    # --- 一覧 ------------------------------------------------------------------------------------

    def objects(self, tasks: list[Task]) -> list[KernelObject]:
        self._tasks = {t.handle: t for t in tasks}
        layouts = self.rtos.layouts
        found: dict[int, str] = {}                                        # 番地 → 種類（見つけた順）
        for kind, (head_name, count_name) in DEBUG_LISTS.items():
            layout = layouts.get(kind)
            head = self.rtos._variable(head_name)
            if layout is None or head is None or not layout.has("DbgNextPtr"):
                continue
            for handle in self.rtos.chain(head, layout.offset("DbgNextPtr"), head_name,
                                          self.rtos._variable(count_name)):
                if self._typed(handle, kind):
                    found.setdefault(handle, kind)
                else:
                    self.rtos.warnings.append(f"{head_name} の 0x{handle:08X} は Type が {kind} でない")
        names: dict[int, list[str]] = {}                                  # 番地 → 変数の式（変数そのものを先に）
        structs = {}
        for kind, (type_name, struct_name) in KINDS.items():
            if kind in layouts:
                structs[type_name] = structs[struct_name.removeprefix("struct ")] = kind
        for f in sorted(typed_variables(self.rtos.info, self.mem, structs, exclude=self.rtos.kernel_pointers()),
                        key=lambda f: f.pointer):
            if self._typed(f.address, f.family):
                names.setdefault(f.address, []).append(f.expression)
                found.setdefault(f.address, f.family)
        for task in tasks:
            w = task.wait
            if w is not None and w.object and w.kind in layouts and self._typed(w.object, w.kind):
                found.setdefault(w.object, w.kind)
        return [self._named(handle, kind, layouts[kind], names.get(handle, [])) for handle, kind in found.items()]

    def _typed(self, handle: int, kind: str) -> bool:
        """先頭の Type が種類と合うか（Type の無い設定では確かめられないので True）。"""
        if not self.rtos.layouts[kind].has("Type"):
            return True
        try:
            return self.mem.read(handle, 4) == KIND_TYPES[kind]
        except AccessError:
            return False

    def _named(self, handle: int, kind: str, layout: StructLayout, names: list[str]) -> KernelObject:
        try:
            obj = self._read(handle, kind, layout)
            own = self.rtos.object_name(handle, kind)
        except AccessError as e:
            obj, own = KernelObject(handle, kind, "", "", notes=[f"読めない: {e}"]), None
        if own:
            obj.name, obj.source, obj.aliases = own, "kernel", sorted(set(names))
        elif names:
            obj.name, obj.source, obj.aliases = names[0], "variable", sorted(set(names[1:]) - {names[0]})
        else:
            obj.name, obj.source = self.rtos.symbol_name(handle) or f"0x{handle:08X}", "waiter"
        return obj

    def _read(self, handle: int, kind: str, layout: StructLayout) -> KernelObject:
        fields = getattr(self, f"_{kind}")(layout, handle)
        waiters: list[Waiter] = []
        if layout.has("PendList.HeadPtr"):
            head = self._u(layout, handle, "PendList.HeadPtr")
            count = self._u(layout, handle, "PendList.NbrEntries") if layout.has("PendList.NbrEntries") else None
            link = self.rtos.tcb.offset("PendNextPtr")
            waiters = [self._waiter(t) for t in self.rtos.chain(head, link, f"{kind} の待ち", count)]
        return KernelObject(handle, kind, "", "", fields, waiters, wait_lists=(handle,))

    def _waiter(self, tcb: int) -> Waiter:
        task = self._tasks.get(tcb)
        name = task.name if task is not None else self.rtos.task_name(tcb)
        priority = task.priority if task is not None else self.rtos._field(tcb, "Prio")
        reason = task.wait.reason if task is not None and task.wait is not None else "pend"
        detail: dict = {}
        if reason == "event" and self.rtos.tcb.has("FlagsPend"):
            option = self.rtos._field(tcb, "FlagsOpt")
            detail = {"flags": self.rtos._field(tcb, "FlagsPend"), "all": bool(option & (FLAG_SET_ALL | FLAG_CLR_ALL)),
                      "clear": bool(option & FLAG_CONSUME)}                # clear = 立ったビットを消す（CONSUME）
            if option & (FLAG_CLR_ALL | FLAG_CLR_ANY):
                detail["on_clear"] = True                                  # ビットが消えるのを待つ
        return Waiter(name, tcb, priority, reason, detail)

    # --- 読み ------------------------------------------------------------------------------------

    def _u(self, layout: StructLayout, handle: int, path: str) -> int:
        return self.mem.uint(handle + layout.offset(path), layout.member_size(path))

    def _semaphore(self, s: StructLayout, h: int) -> dict:
        return {"count": self._u(s, h, "Ctr")}                            # µC/OS-III のセマフォに最大は無い

    def _mutex(self, s: StructLayout, h: int) -> dict:
        owner = self._u(s, h, "OwnerTCBPtr")
        fields: dict = {"available": owner == 0, "holder": None, "lock_count": self._u(s, h, "OwnerNestingCtr"),
                        "priority_inherit": True}                         # µC/OS-III のミューテックスは必ず継承する
        if owner:
            task = self._tasks.get(owner)
            priority = task.priority if task is not None else self.rtos._field(owner, "Prio")
            base = self.rtos.handle_priority(owner)
            fields["holder"] = {"task": task.name if task is not None else self.rtos.task_name(owner),
                                "handle": owner, "priority": priority, "base_priority": base,
                                "inherited": priority != base}
        return fields

    def _queue(self, s: StructLayout, h: int) -> dict:
        fields = {"count": self._u(s, h, "MsgQ.NbrEntries"), "max_msgs": self._u(s, h, "MsgQ.NbrEntriesSize")}
        if s.has("MsgQ.NbrEntriesMax"):
            fields["peak"] = self._u(s, h, "MsgQ.NbrEntriesMax")          # これまでの最大（DBG のとき）
        return fields

    def _event_flags(self, s: StructLayout, h: int) -> dict:
        return {"flags": self._u(s, h, "Flags")}

    def _memory_pool(self, s: StructLayout, h: int) -> dict:
        total, free = self._u(s, h, "NbrMax"), self._u(s, h, "NbrFree")
        return {"block_size": self._u(s, h, "BlkSize"), "max_blocks": total, "used_blocks": total - free,
                "free_blocks": free}

    def _cond(self, s: StructLayout, h: int) -> dict:
        mutex = self._u(s, h, "Mutex")
        return {"mutex": self.rtos.object_label(mutex, "mutex") if mutex else None}

    def _timer(self, s: StructLayout, h: int) -> dict:
        state = self._u(s, h, "State")
        period = self._u(s, h, "Period")
        fields: dict = {"state": TMR_STATES.get(state, str(state)), "active": state == TMR_RUNNING,
                        "periodic": self._u(s, h, "Opt") == OPT_TMR_PERIODIC, "period_ticks": period,
                        "delay_ticks": self._u(s, h, "Dly")}
        if state == TMR_RUNNING:
            remaining = self.timer_remaining(s, h)
            if remaining is not None:
                fields["remaining_ticks"] = remaining
        fields["callback"] = self._function_name(self._u(s, h, "CallbackPtr"))
        fields["argument"] = self._u(s, h, "CallbackPtrArg")
        return fields

    def timer_remaining(self, s: StructLayout, h: int) -> int | None:
        """動いているタイマの残りの OS のティック。OSTmrRemainGet と同じく OSTmrListPtr の差分（Remain）を自分まで足し、
        時間のタスクがまだ反映していない経過（OSTickCtr − OSTmrTaskTickBase）を引く。列に無ければ None。"""
        head = self.rtos._variable("OSTmrListPtr")
        if head is None:
            return None
        total = 0
        for node in self.rtos.chain(head, s.offset("NextPtr"), "OSTmrListPtr"):
            total += self._u(s, node, "Remain")
            if node == h:
                tick, base = self.rtos.tick_count(), self.rtos._variable("OSTmrTaskTickBase")
                elapsed = (tick - base) & 0xFFFF_FFFF if tick is not None and base is not None else 0
                return max(total - elapsed, 0)
        return None

    def _function_name(self, address: int) -> str | None:
        if not address:
            return None
        symbols = self.rtos.symbols
        hit = symbols.symbol_at(address & ~1) if symbols is not None else None
        if hit is None:
            return f"0x{address:08X}"
        return hit[0].name if hit[1] == 0 else f"{hit[0].name}+{hit[1]}"
