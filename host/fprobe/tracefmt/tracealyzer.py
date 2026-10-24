"""Percepio Tracealyzer の記録（TraceRecorder 4.6 以降の PSF のストリーミング。TZ-10・FRT-05）の解読（できる範囲）。
出典: github.com/percepio/TraceRecorderSource v4.11（trcStreamingRecorder.c・trcEvent.c・trcEntryTable.h・
trcTimestamp.h・trcPrint.c・trcISR.c・trcTask.c・trcObject.c・kernelports/FreeRTOS/include/trcKernelPort.h）。

流れ（RTT の上り "TzData"。ホストが下り "TzCtrl" に開始のコマンド `01 01 00 00 00 00 FD FF` を書く）:
1. 見出し 32 バイト: "PSF\\0"（0x50534600。ターゲットのエンディアン）・版・platform（FreeRTOS = 0x1AA1）・options
   （bit3 = 64 ビットの語）・コアの数・…
2. 時刻の情報: type・period・frequency（語）・wraparounds・osTickHz・latestTimestamp・osTickCount。
3. エントリの表: [個数, 名前の欄の大きさ, 状態の数] の語、続いて使っている枠ごとに {番地（語）, 状態（語 × 数）,
   options（u32）, 名前}。タスク・ISR・文字列の名前はここと、後の OBJ_NAME の出来事で分かる。
4. 出来事: u16 の ID（bit11..0 = コード、bit15..12 = 続く語の数）・u16 の通し番号（下位 16 ビット。複数コアは
   bit15..12 がコア）・u32 の時刻、続いて語。文字列などのデータは語の倍数に詰めて語の数に含む。

確かでない点: 出来事のコードは FreeRTOS のポートのもの（platform 0x1AA1）。ほかの RTOS のポートはコードが違う
（その場合は名前を付けずに "code 0x.." と生の値で出す）。ISR の拡張の形（ISR_BEGIN_EXT）は扱わない。時刻は
増えるタイマ（type 1・5）を前提に 32 ビットで続け、減るタイマ・OS のタイマの形（type 2・3・4・6）は生の値。
RAM のスナップショット（RingBuffer のストリームポートの TraceRingBuffer_t。エントリの表が全部の枠で、出来事は
コアごとの head / tail のリング）と、4.5 までのスナップショット（RecorderDataType）は読まない（形が流れと違い、
細部を確かめられていない）。
"""

from __future__ import annotations

import struct

from ..logfmt.printf import Words, format_c
from .model import Trace, TraceEvent

__all__ = ["CHANNEL_NAME", "COMMAND_START", "COMMAND_STOP", "PSF_MAGIC", "TracealyzerDecoder", "TracealyzerError",
           "decode_tracealyzer"]

CHANNEL_NAME, CONTROL_NAME = "TzData", "TzCtrl"
PSF_MAGIC = 0x50534600
COMMAND_START = bytes([1, 1, 0, 0, 0, 0, 0xFD, 0xFF])
COMMAND_STOP = bytes([1, 0, 0, 0, 0, 0, 0xFE, 0xFF])
FREERTOS = 0x1AA1
_CODES = {0x01: "TRACE_START", 0x02: "TS_CONFIG", 0x03: "OBJ_NAME", 0x04: "TASK_PRIORITY", 0x05: "TASK_PRIO_INHERIT",
          0x06: "TASK_PRIO_DISINHERIT", 0x07: "DEFINE_ISR", 0x10: "TASK_CREATE", 0x11: "QUEUE_CREATE",
          0x12: "SEMAPHORE_BINARY_CREATE", 0x13: "MUTEX_CREATE", 0x14: "TIMER_CREATE", 0x15: "EVENTGROUP_CREATE",
          0x16: "SEMAPHORE_COUNTING_CREATE", 0x17: "MUTEX_RECURSIVE_CREATE", 0x18: "STREAMBUFFER_CREATE",
          0x19: "MESSAGEBUFFER_CREATE", 0x20: "TASK_DELETE", 0x21: "QUEUE_DELETE", 0x22: "SEMAPHORE_DELETE",
          0x23: "MUTEX_DELETE", 0x24: "TIMER_DELETE", 0x25: "EVENTGROUP_DELETE", 0x28: "STREAMBUFFER_DELETE",
          0x29: "MESSAGEBUFFER_DELETE", 0x30: "TASK_READY", 0x31: "NEW_TIME", 0x32: "NEW_TIME_SCHEDULER_SUSPENDED",
          0x33: "ISR_BEGIN", 0x34: "ISR_RESUME", 0x35: "TS_BEGIN", 0x36: "TS_RESUME", 0x37: "TASK_ACTIVATE",
          0x38: "MALLOC", 0x39: "FREE", 0x3A: "LOWPOWER_BEGIN", 0x3B: "LOWPOWER_END", 0x50: "QUEUE_SEND",
          0x51: "SEMAPHORE_GIVE", 0x52: "MUTEX_GIVE", 0x56: "QUEUE_SEND_BLOCK", 0x57: "SEMAPHORE_GIVE_BLOCK",
          0x58: "MUTEX_GIVE_BLOCK", 0x59: "QUEUE_SEND_FROMISR", 0x5A: "SEMAPHORE_GIVE_FROMISR", 0x60: "QUEUE_RECEIVE",
          0x61: "SEMAPHORE_TAKE", 0x62: "MUTEX_TAKE", 0x66: "QUEUE_RECEIVE_BLOCK", 0x67: "SEMAPHORE_TAKE_BLOCK",
          0x68: "MUTEX_TAKE_BLOCK", 0x69: "QUEUE_RECEIVE_FROMISR", 0x6A: "SEMAPHORE_TAKE_FROMISR", 0x70: "QUEUE_PEEK",
          0x79: "TASK_DELAY_UNTIL", 0x7A: "TASK_DELAY", 0x7B: "TASK_SUSPEND", 0x7C: "TASK_RESUME",
          0x7D: "TASK_RESUME_FROMISR", 0xA0: "TIMER_START", 0xA1: "TIMER_RESET", 0xA2: "TIMER_STOP",
          0xA3: "TIMER_CHANGEPERIOD", 0xB0: "EVENTGROUP_SYNC", 0xB1: "EVENTGROUP_WAITBITS",
          0xB2: "EVENTGROUP_CLEARBITS", 0xB4: "EVENTGROUP_SETBITS", 0xE9: "MALLOC_FAILED", 0xEA: "FREE_FAILED",
          0xEB: "UNUSED_STACK"}
# カーネルオブジェクト（キュー・セマフォ・ミューテックス・タイマ・イベントグループ・ストリーム / メッセージバッファ）の
# 作成・削除・操作のコード（上の表の名前で決めた集合。番号の範囲で決めない: NEW_TIME・MALLOC・LOWPOWER・UNUSED_STACK
# 等はオブジェクトの操作でない）
_OBJECT_CODES = frozenset(c for c, n in _CODES.items() if n.split("_")[0] in (
    "QUEUE", "SEMAPHORE", "MUTEX", "TIMER", "EVENTGROUP", "STREAMBUFFER", "MESSAGEBUFFER"))
_USER, _USER_FIXED = 0x90, 0x98
_BLOCKING = {0x56, 0x57, 0x58, 0x66, 0x67, 0x68, 0x79, 0x7A, 0x7B, 0xB1}


class TracealyzerError(Exception):
    pass


class TracealyzerDecoder:
    """流れ → Trace。strings は compact print の番地 → 文字列（ELF）。"""

    def __init__(self, strings=None):
        self.trace = Trace("tracealyzer")
        self._buf = bytearray()
        self._stage = "header"
        self._order = "<"
        self._word = 4
        self._strings = strings
        self._names: dict[int, str] = {}                 # エントリ・オブジェクト → 名前
        self._isrs: dict[int, str] = {}
        self._ts: int | None = None
        self._counts: dict[int, int] = {}                # コア → 前の通し番号
        self._current: int | None = None
        self._isr_depth = 0
        self.platform: int | None = None
        self.cores = 1

    def feed(self, data: bytes) -> list[TraceEvent]:
        self._buf += data
        out: list[TraceEvent] = []
        while True:
            progressed = getattr(self, f"_{self._stage}")(out)
            if not progressed:
                return out

    # --- 見出し・時刻・表 ----------------------------------------------------------------------

    def _header(self, out) -> bool:
        if len(self._buf) < 32:
            return False
        magic_le = struct.unpack_from("<I", self._buf)[0]
        if magic_le == PSF_MAGIC:
            self._order = "<"
        elif struct.unpack_from(">I", self._buf)[0] == PSF_MAGIC:
            self._order = ">"
        else:
            raise TracealyzerError("PSF の見出し（\"PSF\\0\"）が無い: 記録の最初から読んでいない")
        _, version, platform, options, cores = struct.unpack_from(self._order + "IHHII", self._buf)
        self._word = 8 if options & 0x8 else 4
        self.platform, self.cores = platform, max(1, cores & 0xFF)
        cfg = bytes(self._buf[24:32]).split(b"\0", 1)[0].decode("ascii", errors="replace")
        self.trace.system.update(version=version, platform=f"0x{platform:04X}", platform_cfg=cfg, cores=self.cores)
        if platform != FREERTOS:
            self.trace.notes.append(f"platform 0x{platform:04X} は FreeRTOS（0x1AA1）でない: 出来事のコードの名前は"
                                    "FreeRTOS のポートのもの（違うことがある）")
        del self._buf[:32]
        self._stage = "timestamp"
        return True

    def _timestamp(self, out) -> bool:
        w = self._word
        size = 8 + w + 16
        if len(self._buf) < size:
            return False
        kind, period = struct.unpack_from(self._order + "II", self._buf)
        freq = int.from_bytes(self._buf[8:8 + w], "little" if self._order == "<" else "big")
        os_tick_hz = struct.unpack_from(self._order + "I", self._buf, 12 + w)[0]
        self.trace.ts_hz = float(freq) if freq else None
        self.trace.system.update(timer_type=kind, timer_period=period, os_tick_hz=os_tick_hz)
        if kind not in (1, 5):
            self.trace.notes.append(f"時刻の形 {kind}（減るタイマ・OS のタイマ）: 時刻は生の値で、続け方は確かでない")
        del self._buf[:size]
        self._stage = "table"
        return True

    def _table(self, out) -> bool:
        w = self._word
        if len(self._buf) < 3 * w:
            return False
        count, symbol_size, states = (self._uint(self._buf, i * w, w, self._order) for i in range(3))
        entry = w + w * states + 4 + symbol_size
        if states > 16 or symbol_size > 256 or count > 4096:
            raise TracealyzerError(f"エントリの表の値が読む範囲の外（states {states}・symbol_size {symbol_size}・count {count}。"
                                   "読む範囲は states ≤ 16・symbol_size ≤ 256・count ≤ 4096）")
        total = 3 * w + count * entry
        if len(self._buf) < total:
            return False
        for i in range(count):
            off = 3 * w + i * entry
            address = self._uint(self._buf, off, w, self._order)
            name = bytes(self._buf[off + w + w * states + 4:off + entry]).split(b"\0", 1)[0]
            if name:
                self._names[address] = name.decode("utf-8", errors="replace")
            if states:
                priority = self._uint(self._buf, off + w, w, self._order)
                self.trace.system.setdefault("entries", {})[f"0x{address:08X}"] = {
                    "name": self._names.get(address), "state0": priority}
        del self._buf[:total]
        self._stage = "events"
        return True

    @staticmethod
    def _uint(buf, off: int, size: int, order: str = "<") -> int:
        return int.from_bytes(buf[off:off + size], "little" if order == "<" else "big")

    # --- 出来事 --------------------------------------------------------------------------------

    def _events(self, out) -> bool:
        if len(self._buf) < 8:
            return False
        event_id, count, ts = struct.unpack_from(self._order + "HHI", self._buf)
        n = event_id >> 12
        size = 8 + n * self._word
        if len(self._buf) < size:
            return False
        words = [self._uint(self._buf, 8 + i * self._word, self._word, self._order) for i in range(n)]
        raw = bytes(self._buf[8:size])
        del self._buf[:size]
        code = event_id & 0xFFF
        core = count >> 12 if self.cores > 1 else 0
        seq = count & (0xFFF if self.cores > 1 else 0xFFFF)
        modulus = 0x1000 if self.cores > 1 else 0x10000
        previous = self._counts.get(core)
        if previous is not None:
            gap = (seq - previous - 1) % modulus
            if gap:
                self.trace.lost += gap
                out.append(self._add(TraceEvent(self._ts or ts, "overflow", "lost", None, {"missing": gap})))
                self._current, self._isr_depth = None, 0
        self._counts[core] = seq
        self._ts = self.trace.unwrap(self._ts, ts)
        out.extend(self._add(e) for e in self._decode(code, words, raw, self._ts))
        return True

    def _add(self, event: TraceEvent) -> TraceEvent:
        self.trace.events.append(event)
        return event

    def _named(self, task: int):
        """タスクの一覧に入れ、名前（エントリの表・OBJ_NAME）が分かれば付ける。"""
        info = self.trace.task(task)
        if info.name is None and task in self._names:
            info.name = self._names[task]
        return info

    def _text(self, raw: bytes, fixed: int) -> str:
        return raw[fixed * self._word:].split(b"\0", 1)[0].decode("utf-8", errors="replace")

    def _decode(self, code: int, words: list[int], raw: bytes, ts: int) -> list[TraceEvent]:
        name = _CODES.get(code, f"code 0x{code:03X}")
        if code == 0x01:
            current = words[0] if words else None
            self._current = current or None
            if current:
                self._named(current)
            return [TraceEvent(ts, "info", name, None, {"current": words})] + \
                ([TraceEvent(ts, "task_in", name, current)] if current else [])
        if code == 0x03 and words:
            target = words[0]
            text = self._text(raw, 1)
            self._names[target] = text
            if target in self.trace.tasks:
                self.trace.tasks[target].name = text
            return [TraceEvent(ts, "info", name, target, {}, text)]
        if code == 0x04 and len(words) >= 2:
            self.trace.task(words[0]).priority = words[1]
            return [TraceEvent(ts, "task_info", name, words[0], {"priority": words[1]})]
        if code == 0x07 and len(words) >= 2:
            self._isrs[words[0]] = self._text(raw, 2)
            return [TraceEvent(ts, "info", name, None, {"isr": words[0], "priority": words[1]}, self._isrs[words[0]])]
        if code == 0x10 and words:
            info = self._named(words[0])
            if len(words) > 1:
                info.priority = words[1]
            return [TraceEvent(ts, "task_create", name, words[0], {"priority": words[1] if len(words) > 1 else None})]
        if code == 0x20 and words:
            return [TraceEvent(ts, "task_delete", name, words[0])]
        if code == 0x30 and words:
            return [TraceEvent(ts, "task_ready", name, words[0])]
        if code == 0x33 and words:
            self._isr_depth += 1
            return [TraceEvent(ts, "isr_enter", name, None, {"isr": words[0], "name": self._isrs.get(words[0])})]
        if code == 0x34:
            out = []
            if self._isr_depth > 1:
                self._isr_depth -= 1
                out.append(TraceEvent(ts, "isr_exit", name, None, {"resume": words[0] if words else None}))
            return out
        if code == 0x37 and words:
            out = []
            while self._isr_depth:
                self._isr_depth -= 1
                out.append(TraceEvent(ts, "isr_exit", name, None, {"to_task": words[0]}))
            task = words[0]
            if task != self._current:
                self._current = task
                self._named(task)
                out.append(TraceEvent(ts, "task_in", name, task, {"priority": words[1]} if len(words) > 1 else {}))
            return out
        if _USER < code < _USER_FIXED and words:
            nargs = code - _USER - 1
            fmt = self._text(raw, 1 + nargs)
            text = format_c(fmt, Words(words[1:1 + nargs]), pointer_bits=8 * self._word)
            return [TraceEvent(ts, "print", "USER_EVENT", None,
                                {"channel": self._names.get(words[0], f"0x{words[0]:08X}")}, text)]
        if _USER_FIXED <= code <= _USER_FIXED + 4 and len(words) >= 2:
            channel = self._strings(words[0]) if self._strings else None
            fmt = self._strings(words[1]) if self._strings else None
            args = {"channel": channel or f"0x{words[0]:08X}"}
            if fmt is None:
                args.update(format_address=words[1], values=words[2:])
            return [TraceEvent(ts, "print", "USER_EVENT_COMPACT", None, args,
                               format_c(fmt, Words(words[2:]), pointer_bits=8 * self._word) if fmt is not None else None)]
        if code in _BLOCKING:
            return [TraceEvent(ts, "task_block", name, self._current, {"values": words})]
        kind = "object" if code in _OBJECT_CODES else "other"
        args = {"values": words}
        if words and words[0] in self._names:
            args["object"] = self._names[words[0]]
        return [TraceEvent(ts, kind, name, None, args)]


def decode_tracealyzer(data: bytes, strings=None) -> Trace:
    decoder = TracealyzerDecoder(strings)
    decoder.feed(data)
    if decoder._stage != "events":
        raise TracealyzerError("記録の始め（見出し・時刻・エントリの表）が途中で終わっている")
    if decoder._buf:
        decoder.trace.notes.append(f"末尾の {len(decoder._buf)} バイトは出来事の途中で終わっている")
    return decoder.trace
