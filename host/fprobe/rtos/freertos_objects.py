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
from pathlib import PurePath

from ..dwarf.access import AccessError
from ..dwarf.typesys import ArrayType, strip
from .base import stopped_note
from .model import HeapInfo, KernelObject, Task, Waiter
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
# メッセージの長さの語の型 configMESSAGE_BUFFER_LENGTH_TYPE（設定で変わる。マクロなので DWARF に名前は無い）を持つ
# stream_buffer.c の変数（関数 → 変数の名前。FreeRTOS-Kernel 10.x〜11.x のソース）。その DWARF の型の大きさを使う
MESSAGE_LENGTH_VARIABLES = {"prvReadMessageFromBuffer": "xTempNextMessageLength",
                            "xStreamBufferNextMessageLengthBytes": "xTempReturn",
                            "prvWriteMessageToBuffer": "xMessageLength"}
MAX_MESSAGES = 1024
MAX_FREE_BLOCKS = 4096
HEAP_ALLOCATED_BIT = 1 << 31                                # heap_4/5 の heapBLOCK_ALLOCATED_BITMASK（32 ビット）
PORT_BYTE_ALIGNMENT = 8                                     # fprobe.heap が使う（ここでは総量から引かない）


def _local_variable(die, name: str):
    """関数の DIE の中（入れ子のブロックも）の名前 name の変数の DIE。無ければ None。"""
    for child in die.iter_children():
        if child.tag == "DW_TAG_variable":
            attr = child.attributes.get("DW_AT_name")
            if attr is not None and (attr.value.decode(errors="replace") if isinstance(attr.value, bytes)
                                     else str(attr.value)) == name:
                return child
        elif child.tag == "DW_TAG_lexical_block":
            found = _local_variable(child, name)
            if found is not None:
                return found
    return None


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
        for f in rtos.typed_objects(STRUCTS, exclude=KERNEL_TABLES):
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
        """登録簿にも変数にも無いが、タスクが待っているもの。待ちリストの番地と待っているタスクの事実だけを返す
        （持ち主がキューかイベントグループかを、値の見た目で推定しない。総点検 T9）。"""
        waiters = [Waiter(t.name, t.handle, t.priority, t.wait.reason) for t in tasks
                   if t.wait is not None and t.wait.list == lst]
        return KernelObject(lst, "unknown", f"0x{lst:08X}", "waiter", waiters=waiters, wait_lists=(lst,),
                            notes=["タスクが待っているリストの番地。登録簿・変数に無いので持ち主は分からない"])

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
                name = self.mem.cstring(name_ptr, self.rtos.name_limit)
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
            width, origin = self._message_length_bytes()
            fields.update(message_length_bytes=width, message_length_from=origin)
            if width is None:
                fields.update(messages=None, next_message=None)
                notes.append("メッセージの長さの語の大きさ（configMESSAGE_BUFFER_LENGTH_TYPE）が ELF の DWARF から分からない"
                             f"（stream_buffer.c の {', '.join(f'{f}.{v}' for f, v in MESSAGE_LENGTH_VARIABLES.items())} が無い）:"
                             " メッセージを数えない")
            else:
                fields.update(self._messages(self._u(s, h, "pucBuffer"), length, tail, used, width, notes))
        waiters = []
        for path, reason in (("xTaskWaitingToReceive", "receive"), ("xTaskWaitingToSend", "send")):
            task = self._u(s, h, path)
            if task:
                waiters.append(Waiter(self._task_name(task), task, self._priority(task), reason))
        return KernelObject(h, "message_buffer" if message else "stream_buffer", "", "", fields, waiters, notes=notes)

    def _message_length_bytes(self) -> tuple[int | None, str | None]:
        """configMESSAGE_BUFFER_LENGTH_TYPE の大きさ（バイト）と、それを読んだ変数。分からなければ (None, None)。"""
        cached = getattr(self, "_length_type", None)
        if cached is not None:
            return cached
        found: tuple[int | None, str | None] = (None, None)
        wanted = dict(MESSAGE_LENGTH_VARIABLES)
        for function in self.rtos.info.functions():
            variable = wanted.get(function.name)
            die = _local_variable(function.die, variable) if variable else None
            size = strip(self.rtos.info.types.type_of(die)).size if die is not None else None
            if size:
                found = (size, f"configMESSAGE_BUFFER_LENGTH_TYPE = DWARF の {function.name}.{variable} の型")
                break
        self._length_type = found
        return found

    def _messages(self, buffer: int, length: int, tail: int, used: int, width: int, notes: list[str]) -> dict:
        """メッセージバッファの中のメッセージ（長さの語 width バイト + 中身）を数える。"""
        def read(pos: int, n: int) -> bytes:
            first = min(n, length - pos)
            return self.mem.read(buffer + pos, first) + (self.mem.read(buffer, n - first) if n > first else b"")
        count, first_length, pos, left = 0, None, tail, used
        while left >= width and count < MAX_MESSAGES:
            n = int.from_bytes(read(pos, width), "little")
            if n > left - width:
                notes.append("メッセージの長さが残りより長い（読みが一貫しない）")
                break
            first_length = n if first_length is None else first_length
            count += 1
            pos = (pos + width + n) % length
            left -= width + n
        out: dict = {"messages": count, "next_message": first_length}
        if count >= MAX_MESSAGES and left >= width:   # 続きがあるのに上限で止めた: 数は全部ではない
            out["messages_truncated"] = True
            notes.append(f"メッセージを数える上限 {MAX_MESSAGES} 個に達したので止めた（残り {left} バイトは数えていない）")
        return out

    # --- ヒープ --------------------------------------------------------------------------------

    def heap(self) -> HeapInfo | None:
        """FreeRTOS の MemMang の変数（xFreeBytesRemaining・xNextFreeByte・xMinimumEverFreeBytesRemaining・ucHeap 等）
        のうち ELF にあるものを読む。方式（scheme）は変数の有無から推定せず、変数を定めた源のファイルの名前。
        総量は ucHeap の大きさ（configTOTAL_HEAP_SIZE）。整列（portBYTE_ALIGNMENT は port で違い ELF に無い）と管理の
        見出しの分、使える量はそれより少ない。ucHeap が無ければ（領域をアプリが渡す）総量は None。"""
        rtos = self.rtos
        free_var = rtos.optional_address("xFreeBytesRemaining")
        next_free = rtos.optional_address("xNextFreeByte")
        heap_array = self._heap_array_size()
        if next_free is not None:                                       # 切り出した量だけを数える（heap_1）
            source = self._source("xNextFreeByte")
            return HeapInfo(self._scheme(source), heap_array, source=source, handed_out=self.mem.u32(next_free),
                            address=rtos.optional_address("ucHeap"),
                            notes=["残量の変数が無い（xNextFreeByte = 切り出した量だけ）。解放はしない方式"])
        if free_var is None:
            return None
        minimum = rtos.optional_address("xMinimumEverFreeBytesRemaining")
        end_ptr = rtos.optional_address("pxEnd")
        source = self._source("xFreeBytesRemaining")
        info = HeapInfo(self._scheme(source), free=self.mem.u32(free_var), source=source)
        if minimum is not None:
            info.min_free = self.mem.u32(minimum)
        if heap_array is not None:
            info.total = heap_array
            info.address = rtos.optional_address("ucHeap")
        else:
            info.notes.append("ucHeap が無い（領域はアプリが渡す）: 総量は分からない")
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
            self._walk_free(info, start, end)
        return info

    def _source(self, name: str) -> str | None:
        """変数を定めた源のファイルの名前（DWARF の decl_file。分からなければ None）。"""
        try:
            path = self.rtos.info.global_variable(name).decl_file
        except KeyError:
            return None
        return None if not path else PurePath(path.replace("\\", "/")).name

    @staticmethod
    def _scheme(source: str | None) -> str | None:
        return None if source is None else PurePath(source).stem

    def _heap_array_size(self) -> int | None:
        try:
            t = strip(self.rtos.info.global_variable("ucHeap").type)
        except KeyError:
            return None
        return t.size if isinstance(t, ArrayType) else None

    def _walk_free(self, info: HeapInfo, start: int, end: int | None) -> None:
        """空きのリスト（xStart から pxEnd / xEnd まで）を辿り、空きのブロックの数と最大を出す。大きさの語の最上位の
        ビット（heapBLOCK_ALLOCATED_BITMASK。空きのブロックでは 0）は除いて読む。"""
        block = self.mem.u32(start)
        if not block or not end:
            info.notes.append("ヒープはまだ初期化されていない（最初の pvPortMalloc の前）")
            return
        count, largest, seen = 0, 0, set()
        while block != end:
            if block in seen or count >= MAX_FREE_BLOCKS or block == 0:
                reason = "cycle" if block in seen else "null" if block == 0 else "limit"
                info.notes.append(stopped_note("空きのリスト", count, reason, MAX_FREE_BLOCKS))
                break
            seen.add(block)
            size = self.mem.u32(block + 4) & ~HEAP_ALLOCATED_BIT
            count += 1
            largest = max(largest, size)
            block = self.mem.u32(block)
        info.free_blocks, info.largest_free_block = count, largest
