"""試験用: 止まった FreeRTOS の偽のメモリを組み立てる（freertos_demo.elf の像 + .bss を 0 + ucHeap の中の TCB・リスト）。

構造体の位置は ELF の DWARF の型から引く（コンパイラの決めた形が正）。リストの繋ぎ方は FreeRTOS の list.c と同じ
（vListInitialise・vListInsertEnd）。タスクの文脈は PendSV（GCC/ARM_CM4F の xPortPendSVHandler）と同じ積み方:
ハードのフレーム → [s16〜s31] → r4〜r11・EXC_RETURN。
"""

from __future__ import annotations

import io
from pathlib import Path

from dwarf_stack import StackBuilder
from elftools.elf.elffile import ELFFile

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo
from fprobe.rtos.structs import StructLayout
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "freertos_demo"
ELF = FIRMWARE / "freertos_demo.elf"
FILL = 0xA5
PORT_MAX_DELAY = 0xFFFF_FFFF
QUEUE_TYPES = {"queue": 0, "mutex": 1, "counting_semaphore": 2, "binary_semaphore": 3}

_ALIASES = {"fp": "r11", "ip": "r12", "sb": "r9", "sl": "r10"}       # capstone の別名 → r 番号
_CORE = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc"]


class TaskStackBuilder(StackBuilder):
    """StackBuilder に、FreeRTOS の関数の前置きにある `str rX, [sp, #-4]!`（前置の書き戻し）と別名（fp = r11）を足す。"""

    def _prologue(self, insn) -> bool:
        from capstone import arm
        ops = insn.operands
        m = insn.mnemonic
        if m in ("push", "push.w") or (m == "stmdb" and insn.reg_name(ops[0].reg) == "sp"):
            names = [_ALIASES.get(n, n) for n in (insn.reg_name(op.reg) for op in ops if op.type == arm.ARM_OP_REG)]
            names = names[1:] if m == "stmdb" else names
            sp = self._sp(self._sp() - 4 * len(names))
            for i, name in enumerate(sorted(names, key=_CORE.index)):
                self.memory.write_u32(sp + 4 * i, self.regs[name])
            return True
        if insn.mnemonic in ("str", "str.w") and len(ops) == 2 and ops[1].type == arm.ARM_OP_MEM \
                and insn.reg_name(ops[1].mem.base) == "sp":
            name = insn.reg_name(ops[0].reg)
            name = _ALIASES.get(name, name)
            if insn.writeback:
                address = self._sp(self._sp() + ops[1].mem.disp)
            else:
                address = self._sp() + ops[1].mem.disp
            self.memory.write_u32(address, self.regs[name])
            return True
        return super()._prologue(insn)


_INFO: DebugInfo | None = None
_SYMBOLS: Symbols | None = None


def shared() -> tuple[DebugInfo, Symbols]:
    """ELF の DWARF とシンボル（読むのに時間がかかるので試験の間で共有する。読むだけ）。"""
    global _INFO, _SYMBOLS
    if _INFO is None:
        _INFO, _SYMBOLS = DebugInfo(ELF), Symbols(ELF)
    return _INFO, _SYMBOLS


class FreeRTOSBuilder:
    def __init__(self):
        self.info, self.symbols = shared()
        self.memory = ImageAccess.from_symbols(self.symbols)
        elf = ELFFile(io.BytesIO(ELF.read_bytes()))
        bss = elf.get_section_by_name(".bss")
        self.memory.write_memory(bss["sh_addr"], bytes(bss["sh_size"]))
        self.tcb = StructLayout.of(self.info, "TCB_t")
        self.list = StructLayout.of(self.info, "List_t")
        self.item = StructLayout.of(self.info, "ListItem_t")
        self.queue_t = StructLayout.of(self.info, "Queue_t")
        self.heap = self.var("ucHeap")
        self.next = (self.heap + 7) & ~7
        self.priorities = 5
        for p in range(self.priorities):
            self.list_init(self.ready(p))
        for name in ("xDelayedTaskList1", "xDelayedTaskList2", "xPendingReadyList", "xSuspendedTaskList",
                     "xTasksWaitingTermination"):
            self.list_init(self.var(name))
        self.u32(self.var("pxDelayedTaskList"), self.var("xDelayedTaskList1"))
        self.u32(self.var("pxOverflowDelayedTaskList"), self.var("xDelayedTaskList2"))
        self.u32(self.var("xSchedulerRunning"), 1)
        self.registry: list[tuple[str, int]] = []
        self.count = 0

    # --- 小さな道具 ---------------------------------------------------------------------

    def var(self, name: str) -> int:
        return self.info.global_variable(name).place().address

    def ready(self, priority: int) -> int:
        return self.var("pxReadyTasksLists") + priority * self.list.size

    def u32(self, address: int, value: int) -> None:
        self.memory.write_u32(address, value)

    def read32(self, address: int) -> int:
        return int.from_bytes(self.memory.read_memory(address, 4), "little")

    def alloc(self, size: int) -> int:
        address = self.next
        self.next = (self.next + size + 7) & ~7
        assert self.next <= self.heap + 20 * 1024, "偽のヒープがあふれた"
        return address

    def string(self, text: str) -> int:
        address = self.alloc(len(text) + 1)
        self.memory.write_memory(address, text.encode() + b"\0")
        return address

    # --- リスト（list.c と同じ） ---------------------------------------------------------------

    def list_init(self, lst: int) -> None:
        end = lst + self.list.offset("xListEnd")
        self.u32(lst + self.list.offset("uxNumberOfItems"), 0)
        self.u32(lst + self.list.offset("pxIndex"), end)
        self.u32(end + self.item.offset("xItemValue"), 0xFFFF_FFFF)
        self.u32(end + self.item.offset("pxNext"), end)
        self.u32(end + self.item.offset("pxPrevious"), end)

    def insert_end(self, lst: int, item: int, value: int | None = None) -> None:
        index = self.read32(lst + self.list.offset("pxIndex"))
        previous = self.read32(index + self.item.offset("pxPrevious"))
        if value is not None:
            self.u32(item + self.item.offset("xItemValue"), value)
        self.u32(item + self.item.offset("pxNext"), index)
        self.u32(item + self.item.offset("pxPrevious"), previous)
        self.u32(previous + self.item.offset("pxNext"), item)
        self.u32(index + self.item.offset("pxPrevious"), item)
        self.u32(item + self.item.offset("pxContainer"), lst)
        n = lst + self.list.offset("uxNumberOfItems")
        self.u32(n, self.read32(n) + 1)

    # --- タスク --------------------------------------------------------------------------

    def task(self, name: str, priority: int, stack_words: int = 128, touched: int = 64, fill: bool = True) -> int:
        """タスクを作る（prvInitialiseNewTask と同じ欄）。スタックの上から touched バイトを使ったことにする。"""
        stack = self.alloc(stack_words * 4)
        if fill:
            self.memory.write_memory(stack, bytes([FILL]) * (stack_words * 4))
        handle = self.alloc(self.tcb.size)
        self.memory.write_memory(handle, bytes(self.tcb.size))
        end_word = (stack + (stack_words - 1) * 4) & ~7
        self.u32(handle + self.tcb.offset("pxStack"), stack)
        self.u32(handle + self.tcb.offset("pxEndOfStack"), end_word)
        self.memory.write_memory(handle + self.tcb.offset("pcTaskName"), name.encode() + b"\0")
        self.u32(handle + self.tcb.offset("uxPriority"), priority)
        self.u32(handle + self.tcb.offset("uxBasePriority"), priority)
        self.count += 1
        self.u32(handle + self.tcb.offset("uxTCBNumber"), self.count)
        for item in ("xStateListItem", "xEventListItem"):
            self.u32(handle + self.tcb.offset(f"{item}.pvOwner"), handle)
        self.u32(handle + self.tcb.offset("xEventListItem.xItemValue"), self.priorities - priority)
        top = end_word + 4
        self.memory.write_memory(top - touched, bytes(range(touched)) if touched <= 256 else bytes(touched))
        self.u32(handle + self.tcb.offset("pxTopOfStack"), top - touched)
        return handle

    def stack_end(self, handle: int) -> int:
        return self.read32(handle + self.tcb.offset("pxEndOfStack")) + 4

    def put_state(self, handle: int, lst: int, value: int | None = None) -> None:
        self.insert_end(lst, handle + self.tcb.offset("xStateListItem"), value)

    def put_event(self, handle: int, lst: int) -> None:
        self.insert_end(lst, handle + self.tcb.offset("xEventListItem"))

    def set_current(self, handle: int) -> None:
        self.u32(self.var("pxCurrentTCB"), handle)

    def set_tick(self, tick: int) -> None:
        self.u32(self.var("xTickCount"), tick)

    def notify_waiting(self, handle: int) -> None:
        self.memory.write_memory(handle + self.tcb.offset("ucNotifyState"), b"\x01")

    # --- キュー -------------------------------------------------------------------------

    def queue(self, name: str | None, kind: str = "queue", length: int = 4, item_size: int = 4,
              holder: int = 0) -> int:
        q = self.alloc(self.queue_t.size)
        self.memory.write_memory(q, bytes(self.queue_t.size))
        storage = self.alloc(length * item_size) if item_size else 0
        self.u32(q + self.queue_t.offset("pcHead"), 0 if kind == "mutex" else storage or q)
        self.u32(q + self.queue_t.offset("uxLength"), length)
        self.u32(q + self.queue_t.offset("uxItemSize"), item_size)
        self.memory.write_memory(q + self.queue_t.offset("ucQueueType"), bytes([QUEUE_TYPES[kind]]))
        if kind == "mutex":
            self.u32(q + self.queue_t.offset("u.xSemaphore.xMutexHolder"), holder)
        for lst in ("xTasksWaitingToSend", "xTasksWaitingToReceive"):
            self.list_init(q + self.queue_t.offset(lst))
        if name is not None:
            entry = self.var("xQueueRegistry") + 8 * len(self.registry)
            self.u32(entry, self.string(name))
            self.u32(entry + 4, q)
            self.registry.append((name, q))
        return q

    def set_handle(self, variable: str, handle: int) -> None:
        """アプリのハンドルの変数（cmd_queue 等）に番地を書く（xQueueCreate の戻り値を入れたのと同じ）。"""
        self.u32(self.var(variable), handle)

    def take_count(self, q: int, messages: int) -> None:
        self.u32(q + self.queue_t.offset("uxMessagesWaiting"), messages)

    # --- タイマ・イベントグループ・ストリームバッファ（timers.c・event_groups.c・stream_buffer.c と同じ欄） -------

    def timer_lists(self) -> None:
        for name in ("xActiveTimerList1", "xActiveTimerList2"):
            self.list_init(self.var(name))
        self.u32(self.var("pxCurrentTimerList"), self.var("xActiveTimerList1"))
        self.u32(self.var("pxOverflowTimerList"), self.var("xActiveTimerList2"))

    def timer(self, name: str, period: int, auto_reload: bool = True, expiry: int | None = None,
              callback: str = "tick_timer_callback", timer_id: int = 0, overflow: bool = False) -> int:
        """タイマを作る（prvInitialiseNewTimer）。expiry を渡すと動いている（タイマのリストに入れる）。"""
        layout = StructLayout.of(self.info, "Timer_t")
        t = self.alloc(layout.size)
        self.memory.write_memory(t, bytes(layout.size))
        self.u32(t + layout.offset("pcTimerName"), self.string(name))
        self.u32(t + layout.offset("xTimerPeriodInTicks"), period)
        self.u32(t + layout.offset("pvTimerID"), timer_id)
        self.u32(t + layout.offset("pxCallbackFunction"), self.symbols.symbol(callback).address | 1)
        self.u32(t + layout.offset("xTimerListItem.pvOwner"), t)
        status = (0x04 if auto_reload else 0) | (0x01 if expiry is not None else 0)
        self.memory.write_memory(t + layout.offset("ucStatus"), bytes([status]))
        if expiry is not None:
            lst = self.var("xActiveTimerList2" if overflow else "xActiveTimerList1")
            self.insert_end(lst, t + layout.offset("xTimerListItem"), expiry)
        return t

    def event_group(self, bits: int = 0) -> int:
        layout = StructLayout.of(self.info, "EventGroup_t")
        g = self.alloc(layout.size)
        self.memory.write_memory(g, bytes(layout.size))
        self.u32(g + layout.offset("uxEventBits"), bits)
        self.list_init(g + layout.offset("xTasksWaitingForBits"))
        return g

    def wait_bits(self, handle: int, group: int, bits: int, wait_all: bool = False, clear: bool = False) -> None:
        """xEventGroupWaitBits と同じく、イベントリストの項目の値に待つビットと制御のビットを入れて待たせる。"""
        layout = StructLayout.of(self.info, "EventGroup_t")
        value = bits | (0x0100_0000 if clear else 0) | (0x0400_0000 if wait_all else 0)
        self.insert_end(group + layout.offset("xTasksWaitingForBits"), handle + self.tcb.offset("xEventListItem"),
                        value)

    def stream_buffer(self, size: int, data: bytes = b"", trigger: int = 1, message: bool = False,
                      tail: int = 0) -> int:
        """xStreamBufferGenericCreate と同じく、大きさ + 1 の領域。data を tail から（折り返して）入れておく。"""
        layout = StructLayout.of(self.info, "StreamBuffer_t")
        s = self.alloc(layout.size)
        self.memory.write_memory(s, bytes(layout.size))
        length = size + 1
        storage = self.alloc(length)
        for i, b in enumerate(data):
            self.memory.write_memory(storage + (tail + i) % length, bytes([b]))
        self.u32(s + layout.offset("xLength"), length)
        self.u32(s + layout.offset("xTail"), tail)
        self.u32(s + layout.offset("xHead"), (tail + len(data)) % length)
        self.u32(s + layout.offset("xTriggerLevelBytes"), trigger)
        self.u32(s + layout.offset("pucBuffer"), storage)
        self.memory.write_memory(s + layout.offset("ucFlags"), bytes([1 if message else 0]))
        return s

    def stream_waiter(self, s: int, handle: int, receive: bool = True) -> None:
        layout = StructLayout.of(self.info, "StreamBuffer_t")
        self.u32(s + layout.offset("xTaskWaitingToReceive" if receive else "xTaskWaitingToSend"), handle)

    # --- ヒープ（heap_4） -------------------------------------------------------------------

    def heap_state(self, free: int, minimum: int, allocations: int, frees: int,
                   blocks: list[tuple[int, int]] | None = None) -> None:
        """heap_4 の変数。blocks（(ucHeap からの位置, 大きさ)）を空きのリストに繋ぐ（無ければ未初期化 = 0 のまま）。"""
        self.u32(self.var("xFreeBytesRemaining"), free)
        self.u32(self.var("xMinimumEverFreeBytesRemaining"), minimum)
        self.u32(self.var("xNumberOfSuccessfulAllocations"), allocations)
        self.u32(self.var("xNumberOfSuccessfulFrees"), frees)
        if blocks is None:
            return
        heap = self.var("ucHeap")
        end = heap + 20 * 1024 - 8
        self.u32(self.var("pxEnd"), end)
        self.u32(end, 0)
        self.u32(end + 4, 0)
        previous = self.var("xStart")
        for offset, size in blocks:
            block = heap + offset
            self.u32(previous, block)
            self.u32(block + 4, size)
            previous = block
        self.u32(previous, end)

    def receivers(self, q: int) -> int:
        return q + self.queue_t.offset("xTasksWaitingToReceive")

    def senders(self, q: int) -> int:
        return q + self.queue_t.offset("xTasksWaitingToSend")

    # --- 文脈 ---------------------------------------------------------------------------

    def stack_builder(self, handle: int) -> StackBuilder:
        """このタスクのスタック（PSP）で関数の呼び出しをなぞる StackBuilder（メモリは共有）。"""
        b = TaskStackBuilder(self.symbols, top=0x2001_0000)          # MSP: SRAM の 64 KB の末尾（ヒープと重ならない）
        b.memory = self.memory
        b.use_psp(self.stack_end(handle))
        return b

    def switch_out(self, handle: int, b: StackBuilder, pc: int, fp: list[int] | None = None,
                   fp_high: list[int] | None = None) -> int:
        """PendSV と同じく文脈を積み、pxTopOfStack に書く。fp を渡すと FP の文脈（EXC_RETURN 0xFFFFFFED）。"""
        regs = b.regs
        sp = regs["sp"]
        extended = fp is not None
        size = 0x68 if extended else 0x20
        frame = (sp - size) & ~7
        xpsr = 0x0100_0000 | ((1 << 9) if sp - size != frame else 0)
        words = [regs[r] for r in ("r0", "r1", "r2", "r3", "r12", "lr")] + [pc, xpsr]
        if extended:
            words += fp + [0, 0]
        for i, w in enumerate(words):
            self.u32(frame + 4 * i, w)
        at = frame
        if extended:
            at -= 64
            for i, w in enumerate(fp_high or [0] * 16):
                self.u32(at + 4 * i, w)
        at -= 36
        for i, r in enumerate([f"r{n}" for n in range(4, 12)]):
            self.u32(at + 4 * i, regs[r])
        self.u32(at + 32, 0xFFFF_FFED if extended else 0xFFFF_FFFD)
        self.u32(handle + self.tcb.offset("pxTopOfStack"), at)
        return at

    def initial_context(self, handle: int, function: str, parameter: int = 0) -> int:
        """pxPortInitialiseStack と同じ、一度も動いていないタスクの文脈。"""
        top = self.stack_end(handle) - 4
        exit_error = self.symbols.symbol("prvTaskExitError").address | 1
        frame = [parameter, 0, 0, 0, 0, exit_error, self.symbols.symbol(function).address, 0x0100_0000]
        frame_at = top - 4 * len(frame)
        for i, w in enumerate(frame):
            self.u32(frame_at + 4 * i, w)
        at = frame_at - 36
        for i in range(8):
            self.u32(at + 4 * i, 0)
        self.u32(at + 32, 0xFFFF_FFFD)
        self.u32(handle + self.tcb.offset("pxTopOfStack"), at)
        return at
