"""RTX5 のカーネルオブジェクト: ミューテックス・セマフォ・イベントフラグ・メモリプール・メッセージキュー・タイマ。

仕様: docs/superpowers/specs/2026-10-18-u45-rtx5-design.md
RTX5 はオブジェクトの一覧を持たない。次の 4 つから集める（同じ番地は 1 つにまとめる）:
  - 変数: 型が osRtx*_t の大域・ファイルの static 変数（osThreadAttr_t の cb_mem に置いた静的な制御ブロック）
  - 動いているタイマの列 osRtxInfo.timer.list（next）
  - スレッドが持つミューテックスの列 mutex_list（owner_next）
  - スレッドの待ち（thread_prev を辿った先のオブジェクト。共通のメモリから確保したもの）
名前はオブジェクトの name（source "kernel"。変数の名前は aliases）→ 変数 → 番地。待ち手はオブジェクトの thread_list
（thread_next。優先度の高い順）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError
from .model import KernelObject, Task, Waiter
from .objects import typed_variables
from .structs import StructLayout

__all__ = ["KINDS", "RTX5Objects"]

KINDS = {"mutex": "osRtxMutex_t", "semaphore": "osRtxSemaphore_t", "event_flags": "osRtxEventFlags_t",
         "memory_pool": "osRtxMemoryPool_t", "message_queue": "osRtxMessageQueue_t", "timer": "osRtxTimer_t"}
MUTEX_RECURSIVE, MUTEX_PRIO_INHERIT, MUTEX_ROBUST = 0x01, 0x02, 0x08       # osMutexAttr
TIMER_RUNNING, TIMER_PERIODIC = 0x02, 0x01
MAX_ITEMS = 4096


def _layout(info, name: str) -> StructLayout | None:
    try:
        return StructLayout.of(info, name)
    except KeyError:
        return None


class RTX5Objects:
    def __init__(self, rtos):
        self.rtos = rtos
        self.mem = rtos.mem
        self.layouts = {k: layout for k, name in KINDS.items() if (layout := _layout(rtos.info, name)) is not None}

    # --- 一覧 ------------------------------------------------------------------------------------

    def objects(self, tasks: list[Task]) -> list[KernelObject]:
        self._tasks = {t.handle: t for t in tasks}
        found: dict[int, tuple[str, list[str], str]] = {}             # 番地 → (種類, 変数の式, 見つけた所)
        structs = {name: kind for kind, name in KINDS.items() if kind in self.layouts}
        structs["osRtxThread_t"] = "thread"                           # スレッドの中（mutex_list 等）は辿らない
        for f in sorted(typed_variables(self.rtos.info, self.mem, structs, exclude=frozenset({"osRtxInfo"})),
                        key=lambda f: f.pointer):                     # 変数そのものの名前を先に
            if f.family != "thread":
                found.setdefault(f.address, (f.family, [], "variable"))[1].append(f.expression)
        for handle in self._active_timers():
            found.setdefault(handle, ("timer", [], "kernel"))
        for t in tasks:
            for handle in self._owned_mutexes(t.handle):
                found.setdefault(handle, ("mutex", [], "kernel"))
            if t.wait is not None and t.wait.list and t.wait.list not in found:
                kind = self.rtos.object_kind(t.wait.list)
                if kind in self.layouts:
                    found[t.wait.list] = (kind, [], "waiter")
        objects = []
        for handle, (kind, names, source) in found.items():
            try:
                obj = self._read(handle, kind)
                own = self.rtos.object_name(handle)
            except AccessError as e:
                obj, own = KernelObject(handle, kind, "", "", notes=[f"読めない: {e}"]), None
            if own:
                obj.name, obj.source, obj.aliases = own, "kernel", sorted(set(names))
            elif names:
                obj.name, obj.source, obj.aliases = names[0], "variable", sorted(set(names[1:]) - {names[0]})
            else:
                obj.name, obj.source = f"0x{handle:08X}", source
            objects.append(obj)
        return objects

    def _active_timers(self) -> list[int]:
        os_ = self.rtos.os
        if not os_.has("timer.list") or "timer" not in self.layouts:
            return []
        return self._chain(self.rtos._os("timer.list"), self.layouts["timer"].offset("next"), "timer.list")

    def _owned_mutexes(self, thread: int) -> list[int]:
        if "mutex" not in self.layouts or not self.rtos.thread.has("mutex_list"):
            return []
        return self._chain(self.rtos._field(thread, "mutex_list"), self.layouts["mutex"].offset("owner_next"),
                           "mutex_list")

    def _chain(self, head: int, link: int, what: str) -> list[int]:
        out: list[int] = []
        while head:
            if head in out or len(out) >= MAX_ITEMS:
                self.rtos.warnings.append(f"{what} の列が壊れている（{len(out)} 個で止めた）")
                break
            out.append(head)
            head = self.mem.u32(head + link)
        return out

    def _read(self, handle: int, kind: str) -> KernelObject:
        layout = self.layouts[kind]
        fields = getattr(self, f"_{kind}")(layout, handle)
        waiters: list[Waiter] = []
        if layout.has("thread_list"):
            for thread in self._chain(self.mem.u32(handle + layout.offset("thread_list")),
                                      self.rtos.thread.offset("thread_next"), "thread_list"):
                waiters.append(self._waiter(thread))
        return KernelObject(handle, kind, "", "", fields, waiters, wait_lists=(handle,))

    def _waiter(self, thread: int) -> Waiter:
        task = self._tasks.get(thread)
        name = task.name if task is not None else self.rtos.thread_name(thread)
        priority = task.priority if task is not None else self.rtos._field(thread, "priority", True)
        reason = task.wait.reason if task is not None and task.wait is not None else "pend"
        detail = {}
        if reason == "event":
            detail = {"flags": self.rtos._field(thread, "wait_flags"),
                      "options": self.rtos._field(thread, "flags_options")}
        return Waiter(name, thread, priority, reason, detail)

    # --- 読み ------------------------------------------------------------------------------------

    def _u(self, layout: StructLayout, handle: int, path: str) -> int:
        return self.mem.uint(handle + layout.offset(path), layout.member_size(path))

    def _mutex(self, s: StructLayout, h: int) -> dict:
        owner = self._u(s, h, "owner_thread")
        attr = self._u(s, h, "attr") if s.has("attr") else 0
        fields: dict = {"available": owner == 0, "holder": None, "lock_count": self._u(s, h, "lock"),
                        "recursive": bool(attr & MUTEX_RECURSIVE), "priority_inherit": bool(attr & MUTEX_PRIO_INHERIT),
                        "robust": bool(attr & MUTEX_ROBUST)}
        if owner:
            task = self._tasks.get(owner)
            priority = task.priority if task is not None else self.rtos._field(owner, "priority", True)
            base = self.rtos._field(owner, "priority_base", True)
            fields["holder"] = {"task": task.name if task is not None else self.rtos.thread_name(owner),
                                "handle": owner, "priority": priority, "base_priority": base,
                                "inherited": priority != base}
        return fields

    def _semaphore(self, s: StructLayout, h: int) -> dict:
        return {"count": self._u(s, h, "tokens"), "max": self._u(s, h, "max_tokens")}    # FreeRTOS・Zephyr と同じ名前

    def _event_flags(self, s: StructLayout, h: int) -> dict:
        return {"flags": self._u(s, h, "event_flags")}

    def _memory_pool(self, s: StructLayout, h: int) -> dict:
        size, total, used = (self._u(s, h, f"mp_info.{m}") for m in ("block_size", "max_blocks", "used_blocks"))
        return {"block_size": size, "max_blocks": total, "used_blocks": used, "free_blocks": total - used}

    def _message_queue(self, s: StructLayout, h: int) -> dict:
        size, count, total = (self._u(s, h, p) for p in ("msg_size", "msg_count", "mp_info.max_blocks"))
        return {"msg_size": size, "count": count, "max_msgs": total, "free": total - count}

    def _timer(self, s: StructLayout, h: int) -> dict:
        active = self._u(s, h, "state") == TIMER_RUNNING
        fields: dict = {"active": active, "periodic": bool(self._u(s, h, "type") & TIMER_PERIODIC),
                        "period_ticks": self._u(s, h, "load")}
        if active:
            remaining = self._remaining(h, s)
            if remaining is not None:
                fields["remaining_ticks"] = remaining
        fields["callback"] = self._function_name(self._u(s, h, "finfo.func"))
        fields["argument"] = self._u(s, h, "finfo.arg")
        return fields

    def _remaining(self, target: int, s: StructLayout) -> int | None:
        """osRtxInfo.timer.list（tick は前との差分）を先頭から足して、target までの残りのティック。"""
        total = 0
        for handle in self._active_timers():
            total += self._u(s, handle, "tick")
            if handle == target:
                return total
        return None

    def _function_name(self, address: int) -> str | None:
        if not address:
            return None
        symbols = self.rtos.symbols
        hit = symbols.symbol_at(address & ~1) if symbols is not None else None
        if hit is None:
            return f"0x{address:08X}"
        return hit[0].name if hit[1] == 0 else f"{hit[0].name}+{hit[1]}"
