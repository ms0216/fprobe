"""ThreadX のカーネルオブジェクト: mutex・semaphore・queue・event_flags・block_pool・byte_pool・timer。

仕様: docs/superpowers/specs/2026-10-18-u45-threadx-design.md
ThreadX は種類ごとに「作られた」環を持つ（_tx_mutex_created_ptr 等、*_created_next。数は *_created_count）ので、
そこから全部が見つかる。名前はオブジェクトの *_name（source "kernel"。変数の名前は aliases）→ 変数 → 番地。
待ち手は *_suspension_list の環（tx_thread_suspended_next。*_suspended_count 個）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError
from .model import KernelObject, Task, Waiter
from .structs import StructLayout

__all__ = ["IDS", "KINDS", "ThreadXObjects"]

# 種類 → (型の名前, メンバーの接頭辞)。作られた環の変数は _tx_<種類>_created_ptr・_tx_<種類>_created_count
KINDS = {"mutex": ("TX_MUTEX", "tx_mutex"), "semaphore": ("TX_SEMAPHORE", "tx_semaphore"),
         "queue": ("TX_QUEUE", "tx_queue"), "event_flags": ("TX_EVENT_FLAGS_GROUP", "tx_event_flags_group"),
         "block_pool": ("TX_BLOCK_POOL", "tx_block_pool"), "byte_pool": ("TX_BYTE_POOL", "tx_byte_pool"),
         "timer": ("TX_TIMER", "tx_timer")}
# 制御ブロックの先頭の id（tx_<種類>.h の TX_*_ID）→ 種類
IDS = {0x4D55_5445: "mutex", 0x5345_4D41: "semaphore", 0x5155_4555: "queue", 0x4456_444E: "event_flags",
       0x424C_4F43: "block_pool", 0x4259_5445: "byte_pool", 0x4154_494D: "timer"}
KIND_IDS = {kind: ident for ident, kind in IDS.items()}
TX_AND, TX_CLEAR = 0x2, 0x1               # tx_event_flags_get の get_option（TX_AND・TX_AND_CLEAR・TX_OR_CLEAR）


# ThreadX が定める、カーネルのオブジェクトを指す変数（tx_thread.h・tx_timer.h・各 tx_*.h の *_created_ptr）。
# オブジェクトの別名（aliases）に使わない。名前の接頭辞（_tx_）では決めない。
KERNEL_VARIABLES = frozenset(
    {"_tx_thread_current_ptr", "_tx_thread_execute_ptr", "_tx_thread_priority_list", "_tx_timer_current_ptr",
     "_tx_timer_expired_timer_ptr", "_tx_timer_list", "_tx_timer_list_start", "_tx_timer_list_end", "_tx_timer_thread"}
    | {f"_tx_{kind}_created_ptr" for kind in ("thread", "queue", "semaphore", "mutex", "event_flags", "block_pool",
                                              "byte_pool", "timer")})


class ThreadXObjects:
    def __init__(self, rtos):
        self.rtos = rtos
        self.mem = rtos.mem

    def kernel_variables(self) -> frozenset[str]:
        """KERNEL_VARIABLES のうち ELF にあるもの。"""
        return frozenset(v.name for v in self.rtos.info.global_variables() if v.name in KERNEL_VARIABLES)

    # --- 一覧 ------------------------------------------------------------------------------------

    def objects(self, tasks: list[Task]) -> list[KernelObject]:
        self._tasks = {t.handle: t for t in tasks}
        names: dict[int, list[str]] = {}                                  # 番地 → 変数の式（変数そのものを先に）
        structs = {type_name: kind for kind, (type_name, _) in KINDS.items() if kind in self.rtos.layouts}
        structs["TX_THREAD"] = "thread"
        for f in sorted(self.rtos.typed_objects(structs, exclude=self.kernel_variables()), key=lambda f: f.pointer):
            if f.family != "thread":
                names.setdefault(f.address, []).append(f.expression)
        objects = []
        for kind, (layout, prefix) in self.rtos.layouts.items():
            head = self.rtos._variable(f"_tx_{kind}_created_ptr")
            if not head:
                continue
            count = self.rtos._variable(f"_tx_{kind}_created_count")
            for handle in self.rtos.ring(head, layout.offset(f"{prefix}_created_next"), f"{kind} の作られた環",
                                         KIND_IDS[kind], count):
                objects.append(self._named(handle, kind, layout, prefix, names.get(handle, [])))
        return objects

    def _named(self, handle: int, kind: str, layout: StructLayout, prefix: str, names: list[str]) -> KernelObject:
        try:
            obj = self._read(handle, kind, layout, prefix)
            own = self.rtos.object_name(handle, kind)
        except AccessError as e:
            obj, own = KernelObject(handle, kind, "", "", notes=[f"読めない: {e}"]), None
        if own:
            obj.name, obj.source, obj.aliases = own, "kernel", sorted(set(names))
        elif names:
            obj.name, obj.source, obj.aliases = names[0], "variable", sorted(set(names[1:]) - {names[0]})
        else:
            obj.name, obj.source = f"0x{handle:08X}", "kernel"
        return obj

    def _read(self, handle: int, kind: str, layout: StructLayout, prefix: str) -> KernelObject:
        fields = getattr(self, f"_{kind}")(layout, prefix, handle)
        waiters: list[Waiter] = []
        if layout.has(f"{prefix}_suspension_list"):
            head = self._u(layout, handle, f"{prefix}_suspension_list")
            count = self._u(layout, handle, f"{prefix}_suspended_count")
            link = self.rtos.thread.offset("tx_thread_suspended_next")
            waiters = [self._waiter(t) for t in self.rtos.ring(head, link, f"{kind} の待ち", None, count)]
        return KernelObject(handle, kind, "", "", fields, waiters, wait_lists=(handle,))

    def _waiter(self, thread: int) -> Waiter:
        task = self._tasks.get(thread)
        name = task.name if task is not None else self.rtos.thread_name(thread)
        priority = task.priority if task is not None else self.rtos._field(thread, "tx_thread_priority")
        reason = task.wait.reason if task is not None and task.wait is not None else "pend"
        detail: dict = {}
        if reason == "event":
            option = self.rtos._field(thread, "tx_thread_suspend_option")
            detail = {"flags": self.rtos._field(thread, "tx_thread_suspend_info"), "all": bool(option & TX_AND),
                      "clear": bool(option & TX_CLEAR)}
        elif task is not None and task.wait is not None and task.wait.kind == "byte_pool":
            detail = {"request": self.rtos._field(thread, "tx_thread_suspend_info")}       # 確保したいバイト数
        return Waiter(name, thread, priority, reason, detail)

    # --- 読み ------------------------------------------------------------------------------------

    def _u(self, layout: StructLayout, handle: int, path: str) -> int:
        return self.mem.uint(handle + layout.offset(path), layout.member_size(path))

    def _mutex(self, s: StructLayout, p: str, h: int) -> dict:
        owner = self._u(s, h, f"{p}_owner")
        fields: dict = {"available": owner == 0, "holder": None, "lock_count": self._u(s, h, f"{p}_ownership_count"),
                        "priority_inherit": bool(self._u(s, h, f"{p}_inherit"))}
        if owner:
            task = self._tasks.get(owner)
            priority = task.priority if task is not None else self.rtos._field(owner, "tx_thread_priority")
            base = self.rtos._field(owner, "tx_thread_user_priority")
            fields["holder"] = {"task": task.name if task is not None else self.rtos.thread_name(owner),
                                "handle": owner, "priority": priority, "base_priority": base,
                                "inherited": priority != base}
        return fields

    def _semaphore(self, s: StructLayout, p: str, h: int) -> dict:
        return {"count": self._u(s, h, f"{p}_count")}                     # ThreadX のセマフォに最大は無い

    def _queue(self, s: StructLayout, p: str, h: int) -> dict:
        words, capacity, count = (self._u(s, h, f"{p}_{m}") for m in ("message_size", "capacity", "enqueued"))
        return {"msg_size": 4 * words, "count": count, "max_msgs": capacity,          # 大きさは語（ULONG）の数
                "free": self._u(s, h, f"{p}_available_storage")}

    def _event_flags(self, s: StructLayout, p: str, h: int) -> dict:
        fields = {"flags": self._u(s, h, f"{p}_current")}
        delayed = self._u(s, h, f"{p}_delayed_clear") if s.has(f"{p}_delayed_clear") else 0
        if delayed:
            fields["delayed_clear"] = delayed                             # 待ち手の検索中に消すビット
        return fields

    def _block_pool(self, s: StructLayout, p: str, h: int) -> dict:
        total, available = self._u(s, h, f"{p}_total"), self._u(s, h, f"{p}_available")
        return {"block_size": self._u(s, h, f"{p}_block_size"), "max_blocks": total, "used_blocks": total - available,
                "free_blocks": available}

    def _byte_pool(self, s: StructLayout, p: str, h: int) -> dict:
        size, free = self._u(s, h, f"{p}_size"), self._u(s, h, f"{p}_available")
        return {"size": size, "free": free, "used": size - free, "fragments": self._u(s, h, f"{p}_fragments")}

    def _timer(self, s: StructLayout, p: str, h: int) -> dict:
        internal = h + s.offset(f"{p}_internal")
        t = self.rtos.timer
        remaining = self.rtos.timer_remaining(internal)
        period = self.mem.u32(internal + t.offset("tx_timer_internal_re_initialize_ticks"))
        fields: dict = {"active": remaining is not None, "periodic": period != 0, "period_ticks": period}
        if remaining is not None:
            fields["remaining_ticks"] = remaining
        fields["callback"] = self._function_name(self.mem.u32(internal + t.offset("tx_timer_internal_timeout_function")))
        fields["argument"] = self.mem.u32(internal + t.offset("tx_timer_internal_timeout_param"))
        return fields

    def _function_name(self, address: int) -> str | None:
        if not address:
            return None
        symbols = self.rtos.symbols
        hit = symbols.symbol_at(address & ~1) if symbols is not None else None
        if hit is None:
            return f"0x{address:08X}"
        return hit[0].name if hit[1] == 0 else f"{hit[0].name}+{hit[1]}"
