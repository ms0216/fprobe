"""FreeRTOS のカーネルオブジェクト（U08）: キュー・セマフォ・ミューテックス・タイマ・イベントグループ・ストリーム /
メッセージバッファと、ヒープ（heap_1/2/4/5）の要約。

仕様: docs/superpowers/specs/2026-10-18-u08-u09-rtos-objects-timeline-design.md の 1 節。

FreeRTOS はオブジェクトの一覧を持たないので、次を合わせる（同じ番地は 1 つ）:
キューの登録簿（registry）・タイマのリスト（kernel: 動いているタイマ）・ハンドルの変数（variable: DWARF の型で探す）・
タスクが待っているもの（waiter: 名前が無ければ番地）。構造体の位置は DWARF から（timers.c 等が無ければその種類は出ない）。

    FreeRTOSObjects(rtos).objects(rtos.tasks())    # [KernelObject]
    FreeRTOSObjects(rtos).heap()                   # HeapInfo | None
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..dwarf.access import AccessError
from ..dwarf.typesys import ArrayType, strip
from .model import HeapInfo, KernelObject, Task, Waiter
from .objects import typed_variables
from .structs import StructLayout

__all__ = ["FreeRTOSObjects"]

# 構造体・typedef の名前 → 分類（動的に作ったもののハンドルの指す先と、静的に確保した Static*_t）
STRUCTS = {"QueueDefinition": "queue", "xSTATIC_QUEUE": "queue", "StaticQueue_t": "queue",
           "StaticSemaphore_t": "queue", "tmrTimerControl": "timer", "xSTATIC_TIMER": "timer",
           "StaticTimer_t": "timer", "EventGroupDef_t": "event_group", "xSTATIC_EVENT_GROUP": "event_group",
           "StaticEventGroup_t": "event_group", "StreamBufferDef_t": "stream_buffer",
           "xSTATIC_STREAM_BUFFER": "stream_buffer", "StaticStreamBuffer_t": "stream_buffer",
           "StaticMessageBuffer_t": "stream_buffer"}
KERNEL_TABLES = frozenset({"xQueueRegistry"})              # 名前の表そのもの（登録簿として別に読む）
SOURCE_RANK = {"registry": 0, "kernel": 1, "variable": 2, "waiter": 3}
TIMER_ACTIVE, TIMER_AUTORELOAD = 0x01, 0x04                 # timers.c の tmrSTATUS_IS_*
SB_MESSAGE, SB_BATCHING = 0x01, 0x04                        # stream_buffer.c の sbFLAGS_IS_*
EVENT_CLEAR_ON_EXIT, EVENT_WAIT_ALL = 0x01, 0x04            # event_groups.c の制御のビット（最上位のバイト）
MESSAGE_LENGTH_BYTES = 4                                    # configMESSAGE_BUFFER_LENGTH_TYPE の既定（size_t）
MAX_MESSAGES = 1024
MAX_FREE_BLOCKS = 4096
HEAP_ALLOCATED_BIT = 1 << 31                                # heap_4/5 の heapBLOCK_ALLOCATED_BITMASK（32 ビット）
PORT_BYTE_ALIGNMENT = 8


def _layout(info, *names: str) -> StructLayout | None:
    try:
        return StructLayout.of(info, *names)
    except KeyError:
        return None


@dataclass
class _Candidate:
    family: str
    names: list[tuple[str, str]] = field(default_factory=list)      # (名前, 出どころ)

    def add(self, name: str | None, source: str) -> None:
        self.names.append((name or "", source))

    @property
    def source(self) -> str:
        return min((s for _, s in self.names), key=SOURCE_RANK.__getitem__)


class FreeRTOSObjects:
    def __init__(self, rtos):
        self.rtos = rtos
        self.mem = rtos.mem
        info = rtos.info
        self.queue = rtos.queue
        self.timer = _layout(info, "Timer_t", "struct tmrTimerControl")
        self.group = _layout(info, "EventGroup_t", "struct EventGroupDef_t")
        self.stream = _layout(info, "StreamBuffer_t", "struct StreamBufferDef_t")

    # --- 一覧 ------------------------------------------------------------------------------------

    def objects(self, tasks: list[Task]) -> list[KernelObject]:
        rtos = self.rtos
        self._tasks = {t.handle: t for t in tasks}
        found: dict[int, _Candidate] = {}

        def add(handle: int, family: str, name: str | None, source: str) -> None:
            if not handle or not self._readable(family):
                return
            found.setdefault(handle, _Candidate(family)).add(name, source)
        for name, handle in rtos.registry():
            add(handle, "queue", name, "registry")
        if self.timer is not None:
            for list_name in ("xActiveTimerList1", "xActiveTimerList2"):
                lst = rtos.optional_address(list_name)
                if lst is not None:
                    for _, owner in rtos.walk(lst):
                        add(owner, "timer", None, "kernel")
        for f in typed_variables(rtos.info, self.mem, STRUCTS, exclude=KERNEL_TABLES):
            add(f.address, f.family, f.expression, "variable")
        objects = [self._read(handle, c) for handle, c in found.items()]
        known = {lst for o in objects for lst in o.wait_lists}
        for lst in sorted({t.wait.list for t in tasks if t.wait is not None and t.wait.list} - known):
            objects.append(self._from_wait_list(lst, tasks))
        return objects

    def _readable(self, family: str) -> bool:
        return {"queue": self.queue, "timer": self.timer, "event_group": self.group,
                "stream_buffer": self.stream}[family] is not None

    def _read(self, handle: int, c: _Candidate) -> KernelObject:
        reader = {"queue": self._queue, "timer": self._timer, "event_group": self._event_group,
                  "stream_buffer": self._stream_buffer}[c.family]
        try:
            obj = reader(handle)
        except AccessError as e:
            obj = KernelObject(handle, c.family, "", "", notes=[f"読めない: {e}"])
        own = obj.name
        ranked = [n for n, s in sorted(c.names, key=lambda x: SOURCE_RANK[x[1]]) if n]
        registry = [n for n, s in c.names if s == "registry" and n]
        name = registry[0] if registry else own or (ranked[0] if ranked else f"0x{handle:08X}")
        obj.name, obj.source = name, c.source
        obj.aliases = sorted({n for n in ranked + ([own] if own else []) if n != name})
        return obj

    def _from_wait_list(self, lst: int, tasks: list[Task]) -> KernelObject:
        """登録簿にも変数にも無いが、タスクが待っているもの（待ちのリストの番地から持ち主を推す）。"""
        q = self.queue
        if q is not None:
            for path in ("xTasksWaitingToReceive", "xTasksWaitingToSend"):
                handle = lst - q.offset(path)
                if self._plausible_queue(handle):
                    obj = self._queue(handle)
                    obj.name, obj.source = f"0x{handle:08X}", "waiter"
                    return obj
        if self.group is not None:
            handle = lst - self.group.offset("xTasksWaitingForBits")
            if self._plausible_group(handle):
                obj = self._event_group(handle)
                obj.name, obj.source = f"0x{handle:08X}", "waiter"
                return obj
        waiters = [Waiter(t.name, t.handle, t.priority, t.wait.reason) for t in tasks
                   if t.wait is not None and t.wait.list == lst]
        return KernelObject(lst, "unknown", f"0x{lst:08X}", "waiter", waiters=waiters, wait_lists=(lst,),
                            notes=["タスクが待っているリストの番地。持ち主（キュー・イベントグループ）と確かめられない"])

    def _plausible_queue(self, h: int) -> bool:
        q = self.queue
        try:
            length = self.mem.u32(h + q.offset("uxLength"))
            messages = self.mem.u32(h + q.offset("uxMessagesWaiting"))
            item_size = self.mem.u32(h + q.offset("uxItemSize"))
            kind_ok = not q.has("ucQueueType") or self.mem.u8(h + q.offset("ucQueueType")) <= 4
            lists_ok = all(self.mem.u32(h + q.offset(f"{p}.uxNumberOfItems")) < 256
                           for p in ("xTasksWaitingToReceive", "xTasksWaitingToSend"))
        except AccessError:
            return False
        return 0 < length <= 0x10000 and messages <= length and item_size <= 0x10000 and kind_ok and lists_ok

    def _plausible_group(self, h: int) -> bool:
        g, rtos = self.group, self.rtos
        try:
            end = h + g.offset("xTasksWaitingForBits") + rtos.list.offset("xListEnd")
            marker = self.mem.uint(end + rtos.item.offset("xItemValue"), rtos.item.member_size("xItemValue"))
        except AccessError:
            return False
        return marker == rtos.tick_mask                             # vListInitialise の印（portMAX_DELAY）

    # --- 共通 ------------------------------------------------------------------------------------

    def _priority(self, handle: int) -> int | None:
        task = self._tasks.get(handle)
        if task is not None:
            return task.priority
        try:
            return self.rtos._field(handle, "uxPriority")
        except AccessError:
            return None

    def _task_name(self, handle: int) -> str:
        task = self._tasks.get(handle)
        if task is not None:
            return task.name
        try:
            return self.rtos.task_name(handle)
        except AccessError:
            return f"0x{handle:08X}"

    def _list_waiters(self, lst: int, reason: str) -> list[Waiter]:
        return [Waiter(self._task_name(owner), owner, self._priority(owner), reason)
                for _, owner in self.rtos.walk(lst)]

    def _u(self, layout: StructLayout, handle: int, path: str, signed: bool = False) -> int:
        return self.mem.uint(handle + layout.offset(path), layout.member_size(path), signed)

    def _function_name(self, address: int) -> str | None:
        if not address:
            return None
        symbols = self.rtos.symbols
        hit = symbols.symbol_at(address & ~1) if symbols is not None else None
        if hit is None:
            return f"0x{address:08X}"
        symbol, offset = hit
        return symbol.name if offset == 0 else f"{symbol.name}+{offset}"

    # --- キュー・セマフォ・ミューテックス ------------------------------------------------------------

    def _queue(self, h: int) -> KernelObject:
        q, rtos = self.queue, self.rtos
        kind = rtos._queue_kind(h)
        messages = self._u(q, h, "uxMessagesWaiting")
        length = self._u(q, h, "uxLength")
        receive, send = h + q.offset("xTasksWaitingToReceive"), h + q.offset("xTasksWaitingToSend")
        if kind in ("mutex", "recursive_mutex"):
            fields: dict = {"available": messages > 0, "holder": None}
            if q.has("u.xSemaphore.xMutexHolder"):
                holder = self._u(q, h, "u.xSemaphore.xMutexHolder")
                if holder:
                    fields["holder"] = self._holder(holder)
            if kind == "recursive_mutex" and q.has("u.xSemaphore.uxRecursiveCallCount"):
                fields["recursion"] = self._u(q, h, "u.xSemaphore.uxRecursiveCallCount")
        elif kind.endswith("semaphore"):
            fields = {"count": messages, "max": length}
        else:
            item_size = self._u(q, h, "uxItemSize")
            fields = {"length": length, "item_size": item_size, "messages": messages, "free": length - messages}
        if q.has("cRxLock") and q.has("cTxLock"):
            rx, tx = self._u(q, h, "cRxLock", True), self._u(q, h, "cTxLock", True)
            if rx != -1 or tx != -1:                                  # queueUNLOCKED 以外: 走行中に読んだ
                fields["locked"] = {"rx": rx, "tx": tx}
        waiters = self._list_waiters(receive, "receive") + self._list_waiters(send, "send")
        return KernelObject(h, kind, "", "", fields, waiters, wait_lists=(receive, send))

    def _holder(self, handle: int) -> dict:
        task = self._tasks.get(handle)
        if task is not None:
            priority, base = task.priority, task.base_priority
        else:
            priority = self._priority(handle)
            base = self.rtos._optional_field(handle, "uxBasePriority")
        out = {"task": self._task_name(handle), "handle": handle, "priority": priority, "base_priority": base}
        if priority is not None and base is not None:
            out["inherited"] = priority != base
        return out

    # --- タイマ --------------------------------------------------------------------------------

    def _timer(self, h: int) -> KernelObject:
        t, rtos = self.timer, self.rtos
        name_ptr = self._u(t, h, "pcTimerName")
        name = ""
        if name_ptr:
            try:
                name = self.mem.cstring(name_ptr, 32)
            except AccessError:
                pass
        if t.has("ucStatus"):
            status = self._u(t, h, "ucStatus")
            active, auto_reload = bool(status & TIMER_ACTIVE), bool(status & TIMER_AUTORELOAD)
        else:                                                    # V10.4 より前: uxAutoReload と、リストに入っているか
            auto_reload = bool(self._u(t, h, "uxAutoReload")) if t.has("uxAutoReload") else False
            active = bool(self._u(t, h, "xTimerListItem.pxContainer"))
        fields: dict = {"period_ticks": self._u(t, h, "xTimerPeriodInTicks"), "auto_reload": auto_reload,
                        "active": active}
        if active:
            expiry = self._u(t, h, "xTimerListItem.xItemValue")
            fields["expiry_tick"] = expiry
            tick = rtos.tick_count()
            if tick is not None:
                fields["remaining_ticks"] = (expiry - tick) & rtos.tick_mask
        fields["callback"] = self._function_name(self._u(t, h, "pxCallbackFunction"))
        fields["id"] = self._u(t, h, "pvTimerID")
        return KernelObject(h, "timer", name, "", fields)

    # --- イベントグループ ------------------------------------------------------------------------

    def _event_group(self, h: int) -> KernelObject:
        g = self.group
        size = g.member_size("uxEventBits")
        shift = 8 * (size - 1)
        control = 0xFF << shift
        bits = self._u(g, h, "uxEventBits") & ~control
        lst = h + g.offset("xTasksWaitingForBits")
        waiters = []
        value_offset = self.rtos.item.offset("xItemValue")
        for item, owner in self.rtos.walk(lst):
            value = self.mem.uint(item + value_offset, self.rtos.item.member_size("xItemValue"))
            flags = value >> shift
            waiters.append(Waiter(self._task_name(owner), owner, self._priority(owner), "bits",
                                  {"bits": value & ~control, "all": bool(flags & EVENT_WAIT_ALL),
                                   "clear_on_exit": bool(flags & EVENT_CLEAR_ON_EXIT)}))
        return KernelObject(h, "event_group", "", "", {"bits": bits}, waiters, wait_lists=(lst,))

    # --- ストリーム / メッセージバッファ ---------------------------------------------------------------

    def _stream_buffer(self, h: int) -> KernelObject:
        s = self.stream
        length = self._u(s, h, "xLength")
        head, tail = self._u(s, h, "xHead"), self._u(s, h, "xTail")
        flags = self._u(s, h, "ucFlags") if s.has("ucFlags") else 0
        used = (length + head - tail) % length if length else 0
        message = bool(flags & SB_MESSAGE)
        fields: dict = {"capacity": max(length - 1, 0), "used": used, "free": max(length - 1 - used, 0),
                        "trigger_level": self._u(s, h, "xTriggerLevelBytes")}
        notes: list[str] = []
        if flags & SB_BATCHING:
            fields["batching"] = True
        if message:
            fields.update(self._messages(self._u(s, h, "pucBuffer"), length, tail, used, notes))
        waiters = []
        for path, reason in (("xTaskWaitingToReceive", "receive"), ("xTaskWaitingToSend", "send")):
            task = self._u(s, h, path)
            if task:
                waiters.append(Waiter(self._task_name(task), task, self._priority(task), reason))
        return KernelObject(h, "message_buffer" if message else "stream_buffer", "", "", fields, waiters, notes=notes)

    def _messages(self, buffer: int, length: int, tail: int, used: int, notes: list[str]) -> dict:
        """メッセージバッファの中のメッセージ（長さの語 + 中身）を数える。長さの語の大きさは既定の size_t と仮定する。"""
        def read(pos: int, n: int) -> bytes:
            first = min(n, length - pos)
            return self.mem.read(buffer + pos, first) + (self.mem.read(buffer, n - first) if n > first else b"")
        count, first_length, pos, left = 0, None, tail, used
        while left >= MESSAGE_LENGTH_BYTES and count < MAX_MESSAGES:
            n = int.from_bytes(read(pos, MESSAGE_LENGTH_BYTES), "little")
            if n > left - MESSAGE_LENGTH_BYTES:
                notes.append("メッセージの長さが残りより長い（読みが一貫しない・長さの型が size_t でない）")
                break
            first_length = n if first_length is None else first_length
            count += 1
            pos = (pos + MESSAGE_LENGTH_BYTES + n) % length
            left -= MESSAGE_LENGTH_BYTES + n
        notes.append("メッセージの長さの語は configMESSAGE_BUFFER_LENGTH_TYPE = size_t（4 バイト）と仮定")
        return {"messages": count, "next_message": first_length}

    # --- ヒープ --------------------------------------------------------------------------------

    def heap(self) -> HeapInfo | None:
        rtos = self.rtos
        free_var = rtos.optional_address("xFreeBytesRemaining")
        next_free = rtos.optional_address("xNextFreeByte")
        heap_array = self._heap_array_size()
        if next_free is not None:                                       # heap_1: 先頭から順に切り出すだけ
            total = None if heap_array is None else heap_array - PORT_BYTE_ALIGNMENT
            used = self.mem.u32(next_free)
            return HeapInfo("heap_1", total, None if total is None else total - used,
                            None if total is None else total - used, notes=["heap_1 は解放しない（残量 = 最小残量）"])
        if free_var is None:
            return None
        minimum = rtos.optional_address("xMinimumEverFreeBytesRemaining")
        end_ptr = rtos.optional_address("pxEnd")
        scheme = "heap_2" if minimum is None else ("heap_4" if heap_array is not None else "heap_5")
        info = HeapInfo(scheme, free=self.mem.u32(free_var))
        if minimum is not None:
            info.min_free = self.mem.u32(minimum)
        if heap_array is not None:
            info.total = heap_array - PORT_BYTE_ALIGNMENT if scheme != "heap_2" else heap_array
            info.address = rtos.optional_address("ucHeap")
        else:
            info.notes.append("heap_5: 領域はアプリが渡すので総量は分からない")
        for name, attr in (("xNumberOfSuccessfulAllocations", "allocations"), ("xNumberOfSuccessfulFrees", "frees")):
            address = rtos.optional_address(name)
            if address is not None:
                setattr(info, attr, self.mem.u32(address))
        if rtos.optional_address("xHeapCanary") is not None:
            info.notes.append("configENABLE_HEAP_PROTECTOR: 空きのリストのポインタが隠されているので辿らない")
            return info
        start = rtos.optional_address("xStart")
        if start is not None:
            end = self.mem.u32(end_ptr) if end_ptr is not None else rtos.optional_address("xEnd")
            self._walk_free(info, start, end, scheme)
        return info

    def _heap_array_size(self) -> int | None:
        try:
            t = strip(self.rtos.info.global_variable("ucHeap").type)
        except KeyError:
            return None
        return t.size if isinstance(t, ArrayType) else None

    def _walk_free(self, info: HeapInfo, start: int, end: int | None, scheme: str) -> None:
        """空きのリスト（xStart から pxEnd / xEnd まで）を辿り、空きのブロックの数と最大を出す。"""
        block = self.mem.u32(start)
        if not block or not end:
            info.notes.append("ヒープはまだ初期化されていない（最初の pvPortMalloc の前）")
            return
        count, largest, seen = 0, 0, set()
        while block != end:
            if block in seen or count >= MAX_FREE_BLOCKS or block == 0:
                info.notes.append(f"空きのリストが壊れている（{count} 個で止めた）")
                break
            seen.add(block)
            size = self.mem.u32(block + 4)
            if scheme in ("heap_4", "heap_5"):
                size &= ~HEAP_ALLOCATED_BIT
            count += 1
            largest = max(largest, size)
            block = self.mem.u32(block)
        info.free_blocks, info.largest_free_block = count, largest
