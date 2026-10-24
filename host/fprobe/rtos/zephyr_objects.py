"""Zephyr のカーネルオブジェクト（U08）: k_sem・k_mutex・k_msgq・k_event・k_timer・k_queue / k_fifo / k_lifo・
k_stack・k_mem_slab・k_heap と、システムのヒープ（k_malloc の _system_heap）。

仕様: docs/superpowers/specs/2026-10-18-u08-u09-rtos-objects-timeline-design.md の 1 節。
Zephyr のオブジェクトは静的な変数（K_SEM_DEFINE 等）なので、DWARF の型で変数を探す（構造体・配列の中も）。
版・設定でメンバーが違うので、構造体にあるメンバーだけを読む。待ち手は各スレッドの base.pended_on（待ちキューの番地）と
オブジェクトの待ちキューの番地の照合で。どの変数にも無い待ちキューは番地で出す（kind "wait_q"）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError
from ..dwarf.typesys import PointerType, strip
from .model import HeapInfo, KernelObject, Task, Waiter
from .structs import StructLayout

__all__ = ["KINDS", "ZephyrObjects"]

KINDS = ("k_sem", "k_mutex", "k_msgq", "k_event", "k_timer", "k_queue", "k_fifo", "k_lifo", "k_stack", "k_mem_slab",
         "k_heap")
_WAIT_Q = {"k_fifo": "_queue.wait_q", "k_lifo": "_queue.wait_q", "k_timer": "wait_q"}
MAX_ITEMS = 4096


def _layout(info, name: str) -> StructLayout | None:
    try:
        return StructLayout.of(info, name)
    except KeyError:
        return None


class ZephyrObjects:
    def __init__(self, rtos):
        self.rtos = rtos
        self.mem = rtos.mem
        self.layouts = {k: layout for k in KINDS if (layout := _layout(rtos.info, f"struct {k}")) is not None}
        self.timeout = _layout(rtos.info, "struct _timeout")
        self.dnode = _layout(rtos.info, "struct _dnode")

    # --- 一覧 ------------------------------------------------------------------------------------

    def objects(self, tasks: list[Task]) -> list[KernelObject]:
        self._tasks = {t.handle: t for t in tasks}
        found: dict[int, tuple[str, list[str]]] = {}
        for f in sorted(self.rtos.typed_objects({k: k for k in self.layouts}),
                        key=lambda f: f.pointer):                     # 変数そのものの名前を先に
            found.setdefault(f.address, (f.family, []))[1].append(f.expression)
        objects = []
        for handle, (kind, names) in found.items():
            try:
                obj = self._read(handle, kind)
            except AccessError as e:
                obj = KernelObject(handle, kind, "", "", notes=[f"読めない: {e}"])
            obj.name, obj.source, obj.aliases = names[0], "variable", sorted(set(names[1:]) - {names[0]})
            objects.append(obj)
        known = {lst for o in objects for lst in o.wait_lists}
        for lst in sorted({t.wait.list for t in tasks if t.wait is not None and t.wait.list} - known):
            objects.append(KernelObject(lst, "wait_q", f"0x{lst:08X}", "waiter", waiters=self._waiters(lst, tasks),
                                        wait_lists=(lst,),
                                        notes=["スレッドが待っている待ちキューの番地。どのオブジェクトの変数にも無い"]))
        return objects

    def _waiters(self, wait_q: int, tasks: list[Task]) -> list[Waiter]:
        return [Waiter(t.name, t.handle, t.priority, "pend") for t in tasks
                if t.wait is not None and t.wait.list == wait_q]

    def _read(self, handle: int, kind: str) -> KernelObject:
        layout = self.layouts[kind]
        self._notes: list[str] = []                                  # 読みの途中の事実（上限で止めた 等）
        fields = getattr(self, f"_{kind}")(layout, handle)
        path = _WAIT_Q.get(kind, "wait_q")
        wait_lists: tuple[int, ...] = ()
        waiters: list[Waiter] = []
        if layout.has(path):
            wait_q = handle + layout.offset(path)
            wait_lists = (wait_q,)
            waiters = self._waiters(wait_q, list(self._tasks.values()))
        return KernelObject(handle, kind, "", "", fields, waiters, wait_lists=wait_lists, notes=self._notes)

    # --- 読み ------------------------------------------------------------------------------------

    def _u(self, layout: StructLayout, handle: int, path: str, signed: bool = False) -> int:
        return self.mem.uint(handle + layout.offset(path), layout.member_size(path), signed)

    def _members(self, layout: StructLayout, handle: int, names: dict[str, str]) -> dict:
        """構造体にあるメンバーだけ（メンバーの道 → 出す名前）。"""
        return {out: self._u(layout, handle, path) for path, out in names.items() if layout.has(path)}

    def _k_sem(self, s: StructLayout, h: int) -> dict:
        return self._members(s, h, {"count": "count", "limit": "max"})         # 上限は FreeRTOS と同じ名前 max

    def _k_mutex(self, s: StructLayout, h: int) -> dict:
        owner = self._u(s, h, "owner") if s.has("owner") else 0
        fields: dict = {"available": owner == 0, "holder": None}
        fields.update(self._members(s, h, {"lock_count": "lock_count"}))
        if owner:
            task = self._tasks.get(owner)
            priority = task.priority if task is not None else self.rtos._field(owner, "base.prio", True)
            base = self._u(s, h, "owner_orig_prio", True) if s.has("owner_orig_prio") else priority
            fields["holder"] = {"task": task.name if task is not None else self.rtos.thread_name(owner),
                                "handle": owner, "priority": priority, "base_priority": base,
                                "inherited": priority != base}
        return fields

    def _k_msgq(self, s: StructLayout, h: int) -> dict:
        fields = self._members(s, h, {"msg_size": "msg_size", "max_msgs": "max_msgs", "used_msgs": "used_msgs"})
        if "max_msgs" in fields and "used_msgs" in fields:
            fields["free"] = fields["max_msgs"] - fields["used_msgs"]
        return fields

    def _k_event(self, s: StructLayout, h: int) -> dict:
        return self._members(s, h, {"events": "events"})

    def _k_timer(self, s: StructLayout, h: int) -> dict:
        fields: dict = {}
        if s.has("period.ticks"):
            fields["period_ticks"] = self._u(s, h, "period.ticks", True)
        node = h + s.offset("timeout.node")
        active = bool(self.mem.u32(node + self._next_offset())) if self.dnode is not None else False
        fields["active"] = active
        if active:
            remaining, stopped = self._remaining(node)
            if remaining is not None:
                fields["remaining_ticks"] = remaining
            elif stopped:                                             # 上限で止めた: 残りは分からない（無いのではない）
                fields["remaining_truncated"] = True
                self._notes.append(f"timeout_list を辿る上限 {MAX_ITEMS} 個に達したので止めた（この節まで届かず、"
                                   "残りのティックは読んでいない）")
        if s.has("status"):
            fields["expiry_count"] = self._u(s, h, "status")
        if s.has("expiry_fn"):
            fields["callback"] = self._function_name(self._u(s, h, "expiry_fn"))
        if s.has("user_data"):
            fields["user_data"] = self._u(s, h, "user_data")
        return fields

    def _next_offset(self) -> int:
        return self.dnode.offset("next" if self.dnode.has("next") else "head")

    def _remaining(self, target: int) -> tuple[int | None, bool]:
        """timeout_list（期限の差分の列）を先頭から足して、target の節までの残りのティック（最後の告知からの経過は含まない）。
        (残り, 辿る上限 MAX_ITEMS で止めたか)。列の終わりまで見て無い・列が無いときは (None, False)。"""
        head = self.rtos.optional_address("timeout_list")
        if head is None or self.timeout is None or not self.timeout.has("dticks"):
            return None, False
        node_offset = self.timeout.offset("node")
        total, node, steps = 0, self.mem.u32(head + self._next_offset()), 0
        while node and node != head:
            if steps >= MAX_ITEMS:
                return None, True
            total += self.mem.uint(node - node_offset + self.timeout.offset("dticks"),
                                   self.timeout.member_size("dticks"), True)
            if node == target:
                return total, False
            node, steps = self.mem.u32(node + self._next_offset()), steps + 1
        return None, False

    def _k_queue(self, s: StructLayout, h: int, prefix: str = "") -> dict:
        path = f"{prefix}data_q.head"
        if not s.has(path):
            return {}
        count, node = 0, self.mem.u32(h + s.offset(path))
        while node and count < MAX_ITEMS:
            count += 1
            node = self.mem.u32(node) & ~3                           # sys_sfnode_t.next_and_flags（下位 2 ビットは印）
        if node:                                                     # 続きがあるのに上限で止めた: 数は全部ではない
            self._notes.append(f"データの列を辿る上限 {MAX_ITEMS} 個に達したので止めた（続きは数えていない）")
            return {"items": count, "items_truncated": True}
        return {"items": count}

    def _k_fifo(self, s: StructLayout, h: int) -> dict:
        return self._k_queue(s, h, "_queue.")

    _k_lifo = _k_fifo

    def _k_stack(self, s: StructLayout, h: int) -> dict:
        if not all(s.has(m) for m in ("base", "next", "top")):
            return {}
        pointer = strip(s.member_type("base"))                     # stack_data_t *（要素の大きさは DWARF から）
        entry = pointer.target.size if isinstance(pointer, PointerType) else None
        if not entry:
            self._notes.append("k_stack の要素（stack_data_t）の大きさが DWARF から分からない: 数を出さない")
            return {}
        base, nxt, top = (self._u(s, h, m) for m in ("base", "next", "top"))
        return {"size": (top - base) // entry, "used": (nxt - base) // entry}

    def _k_mem_slab(self, s: StructLayout, h: int) -> dict:
        names = {f"info.{m}": m for m in ("num_blocks", "block_size", "num_used", "max_used")}
        names.update({m: m for m in ("num_blocks", "block_size", "num_used", "max_used")})
        return self._members(s, h, names)

    def _k_heap(self, s: StructLayout, h: int) -> dict:
        z = self._z_heap(s, h)
        if z is None:
            return self._members(s, h, {"heap.init_bytes": "init_bytes"})
        layout, address = z
        return self._members(layout, address, {m: m for m in ("free_bytes", "allocated_bytes", "max_allocated_bytes")})

    def _z_heap(self, s: StructLayout, h: int) -> tuple[StructLayout, int] | None:
        layout = _layout(self.rtos.info, "struct z_heap")
        if layout is None or not s.has("heap.heap"):
            return None
        address = self._u(s, h, "heap.heap")
        return (layout, address) if address else None

    def _function_name(self, address: int) -> str | None:
        if not address:
            return None
        symbols = self.rtos.symbols
        hit = symbols.symbol_at(address & ~1) if symbols is not None else None
        if hit is None:
            return f"0x{address:08X}"
        return hit[0].name if hit[1] == 0 else f"{hit[0].name}+{hit[1]}"

    # --- ヒープ --------------------------------------------------------------------------------

    def heap(self) -> HeapInfo | None:
        """k_malloc のヒープ（_system_heap）。統計は CONFIG_SYS_HEAP_RUNTIME_STATS の z_heap の欄。"""
        s = self.layouts.get("k_heap")
        address = self.rtos.optional_address("_system_heap")
        if s is None or address is None:
            return None
        z = self._z_heap(s, address)
        info = HeapInfo("sys_heap", address=address)
        if z is not None and z[0].has("free_bytes") and z[0].has("allocated_bytes"):
            layout, at = z
            info.free = self._u(layout, at, "free_bytes")
            used = self._u(layout, at, "allocated_bytes")
            info.total = info.free + used
            if layout.has("max_allocated_bytes"):
                info.min_free = info.total - self._u(layout, at, "max_allocated_bytes")
        else:
            if s.has("heap.init_bytes"):
                info.total = self._u(s, address, "heap.init_bytes")
            info.notes.append("CONFIG_SYS_HEAP_RUNTIME_STATS が無い: 残量は分からない")
        return info
