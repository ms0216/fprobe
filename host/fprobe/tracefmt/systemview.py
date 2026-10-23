"""SEGGER SystemView の記録（RTT で流れるバイト・RAM のリングのスナップショット・.SVDat）の解読（ESP-02・ZEP-01・
SEG-64）。出典: SEGGER_SYSVIEW.c / _Int.h（github.com/SEGGERMicro/SystemView、V3.32 と Zephyr の 3.58）、
ESP-IDF の espytrace/sysview.py、UM08027。

パケット: ID（LEB128。24 未満は 1 バイトで長さの欄なし・引数の並びは ID ごとに決まる）、24 以上は長さ（LEB128）と
ペイロード、最後に前のパケットからの時刻の差（LEB128）。引数の整数は LEB128、文字列は長さ 1 バイト（255 以上は
FF・上位・下位）+ バイト列。タスク・タイマ・資源の ID は (値 << ID_SHIFT) + RAMBase で番地に戻す（INIT が送る）。

時刻: START で LastTxTimeStamp が 0 になり、TRACE_START・INIT の差は意味を持たない（3.58 では古い差が付く）。
絶対の時刻は SYSTIME_CYCLES の値で決め、以後は差を足す（その前の出来事は 0 から）。周期的な SYSTIME で
32 ビットの桁あふれを直す。

同期: 流れの途中から読むとき（ポストモーテム・途中から受けた RTT）は、0x00 が 10 個続く所（sync）を探し、そこから
読む。sync は NOP（ID 0・差 0）の並びとしても読めるので、同期した後は特別扱いしない。

確かでない点: OVERFLOW の DropCount は初期化からの累積（最初に落ちた 1 個は数えない）と読んだ。OS の API の
出来事（ID 32 以上）は記述ファイル（SYSVIEW_<OS>.txt）を渡せば名前と引数に解読し、無ければ "API<ID>" と生の値。
ID の 2 バイト目にコアの番号を入れる ESP の拡張は扱わない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..logfmt.printf import Words, format_c
from .model import Trace, TraceEvent, is_rtos_idle_name

__all__ = ["COMMAND_START", "COMMAND_STOP", "CHANNEL_NAME", "SyncNotFound", "SystemViewDecoder", "SystemViewError",
           "decode_systemview", "parse_description", "strip_svdat_header"]

CHANNEL_NAME = "SysView"
COMMAND_START, COMMAND_STOP = 1, 2
SYNC = b"\x00" * 10
MODULE_EVENT_OFFSET = 512

_NAMES = {0: "NOP", 1: "OVERFLOW", 2: "ISR_ENTER", 3: "ISR_EXIT", 4: "TASK_START_EXEC", 5: "TASK_STOP_EXEC",
          6: "TASK_START_READY", 7: "TASK_STOP_READY", 8: "TASK_CREATE", 9: "TASK_INFO", 10: "TRACE_START",
          11: "TRACE_STOP", 12: "SYSTIME_CYCLES", 13: "SYSTIME_US", 14: "SYSDESC", 15: "MARK_START",
          16: "MARK_STOP", 17: "IDLE", 18: "ISR_TO_SCHEDULER", 19: "TIMER_ENTER", 20: "TIMER_EXIT",
          21: "STACK_INFO", 22: "MODULEDESC", 23: "DATA_SAMPLE", 24: "INIT", 25: "NAME_RESOURCE",
          26: "PRINT_FORMATTED", 27: "NUMMODULES", 28: "END_CALL", 29: "TASK_TERMINATE", 31: "EX"}
# 長さの欄の無い出来事の引数: u = LEB128、s = 文字列
_FIXED = {0: "", 1: "u", 2: "u", 3: "", 4: "u", 5: "", 6: "u", 7: "uu", 8: "u", 9: "uus", 10: "", 11: "",
          12: "u", 13: "uu", 14: "s", 15: "u", 16: "u", 17: "", 18: "", 19: "u", 20: "", 21: "uuuu", 22: "uus",
          23: "uu"}
_EX_NAMES = {0: "MARK", 1: "NAME_MARKER", 2: "HEAP_DEFINE", 3: "HEAP_ALLOC", 4: "HEAP_ALLOC_EX", 5: "HEAP_FREE",
             6: "REGISTER_DATA", 7: "PRINT_ELF"}
_PRINT_LEVELS = {0: "info", 1: "warn", 2: "error"}


class SystemViewError(Exception):
    pass


class SyncNotFound(SystemViewError):
    pass


class _Short(Exception):
    """パケットが途中で終わった（続きを待つ）。"""


def strip_svdat_header(data: bytes) -> bytes:
    """.SVDat の先頭の ";" で始まる文字の行（ESP の書き出す見出し）を除く。"""
    while data.startswith(b";"):
        end = data.find(b"\n")
        if end < 0:
            return b""
        data = data[end + 1:]
    return data


@dataclass(frozen=True)
class ApiDescription:
    name: str
    params: list[tuple[str, str]]          # (表示の名前, 書式 %u 等)


def parse_description(text: str) -> tuple[dict[int, ApiDescription], dict[str, dict[int, str]]]:
    """SYSVIEW_<OS>.txt → (ID → 名前と引数の書式, NamedType の名前 → 値 → 文字)。"""
    events: dict[int, ApiDescription] = {}
    named: dict[str, dict[int, str]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("NamedType"):
            parts = line.split()
            if len(parts) >= 2:
                values = {}
                for item in parts[2:]:
                    key, _, value = item.partition("=")
                    if key != "*":
                        try:
                            values[int(key, 0)] = value
                        except ValueError:
                            pass
                named[parts[1]] = values
            continue
        m = re.match(r"(\d+)\s+(\S+)\s*(.*)$", line)
        if m is None:
            continue
        desc = m[3].split("|", 1)[0]
        params = []
        for p in re.finditer(r"(?:([A-Za-z_][\w ]*?)=)?%(\w+)", desc):
            params.append(((p[1] or "").strip(), p[2]))
        events[int(m[1])] = ApiDescription(m[2], params)
    return events, named


class _Reader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data, self.pos = data, pos
        self.end = len(data) if end is None else end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise _Short
        b = self.data[self.pos]
        self.pos += 1
        return b

    def varint(self) -> int:
        value = shift = 0
        while True:
            b = self.byte()
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7
            if shift > 63:
                raise SystemViewError("LEB128 が長すぎる")

    def string(self) -> str:
        n = self.byte()
        if n == 0xFF:
            n = self.byte() << 8 | self.byte()
        if self.pos + n > self.end:
            raise _Short
        text = self.data[self.pos:self.pos + n].decode("utf-8", errors="replace")
        self.pos += n
        return text

    @property
    def remaining(self) -> int:
        return self.end - self.pos


class SystemViewDecoder:
    """流れ → Trace。synced=False なら、最初の sync（0x00 × 10）までを読み飛ばす。"""

    def __init__(self, synced: bool = False, description: str | None = None, strings=None):
        self.trace = Trace("systemview")
        self.synced = synced
        self._buf = bytearray()
        self._ts = 0
        self._ts_known = False
        self._ram_base = 0
        self._shift = 0
        self._current: int | None = None
        self._isr_depth = 0
        self._drops: int | None = None
        self._resources: dict[int, str] = {}
        self._markers: dict[int, str] = {}
        self._modules: list[tuple[int, str]] = []        # (EventOffset, 記述)
        self._api, self._named = parse_description(description) if description else ({}, {})
        self._strings = strings                           # PRINT_ELF の書式（ELF の番地 → 文字列）
        self.skipped = 0                                  # 同期を探して読み飛ばしたバイト

    # --- 流れ ------------------------------------------------------------------------------

    def feed(self, data: bytes) -> list[TraceEvent]:
        self._buf += data
        if not self.synced:
            i = self._buf.find(SYNC)
            if i < 0:
                keep = len(SYNC) - 1
                self.skipped += max(0, len(self._buf) - keep)
                del self._buf[:max(0, len(self._buf) - keep)]
                return []
            self.skipped += i
            del self._buf[:i + len(SYNC)]
            self.synced = True
        out: list[TraceEvent] = []
        data = bytes(self._buf)
        pos = 0
        while pos < len(data):
            r = _Reader(data, pos)
            try:
                event = self._packet(r)
            except _Short:
                break
            except SystemViewError as e:                  # 形が崩れた: 次の sync を探す
                self.trace.errors += 1
                self.trace.notes.append(f"パケットを解読できない（{e}）: 次の sync まで読み飛ばした")
                self.synced = False
                pos += 1
                del self._buf[:pos]
                return out + self.feed(b"")
            pos = r.pos
            if event is not None:
                out.append(event)
                self.trace.events.append(event)
        del self._buf[:pos]
        return out

    def _task(self, value: int) -> int:
        return (value << self._shift) + self._ram_base

    def _packet(self, r: _Reader) -> TraceEvent | None:
        event_id = r.varint()
        if event_id < 24:
            args = [r.string() if k == "s" else r.varint() for k in _FIXED[event_id]]
            delta = r.varint()
            return self._fixed(event_id, args, delta)
        length = r.varint()
        if r.remaining < length:
            raise _Short
        body = _Reader(r.data, r.pos, r.pos + length)
        r.pos += length
        delta = r.varint()
        return self._long(event_id, body, delta)

    def _advance(self, delta: int, event_id: int) -> int:
        if event_id in (10, 24):                         # TRACE_START・INIT: 差は意味を持たない
            return self._ts
        self._ts += delta
        return self._ts

    def _event(self, ts: int, kind: str, name: str, task: int | None = None, args: dict | None = None,
               text: str | None = None) -> TraceEvent:
        return TraceEvent(ts, kind, name, task, args or {}, text)

    def _fixed(self, event_id: int, args: list, delta: int) -> TraceEvent | None:
        name = _NAMES[event_id]
        if event_id == 12:                               # SYSTIME_CYCLES: 絶対の時刻
            if not self._ts_known:                       # 最初の 1 回で絶対の時刻に合わせる（以後は差を足す）
                self._ts, self._ts_known = args[0], True
            else:
                self._ts += delta
            return self._event(self._ts, "info", name, args={"cycles": args[0]})
        ts = self._advance(delta, event_id)
        if event_id == 0:
            return None
        if event_id == 1:
            drops = args[0]
            lost = drops - self._drops if self._drops is not None and drops >= self._drops else drops
            self._drops = drops
            self.trace.lost += max(lost, 1)
            self._current, self._isr_depth = None, 0
            return self._event(ts, "overflow", name, args={"drop_count": drops})
        if event_id == 2:
            self._isr_depth += 1
            return self._event(ts, "isr_enter", name, args={"number": args[0]})
        if event_id in (3, 18):
            self._isr_depth = max(0, self._isr_depth - 1)
            return self._event(ts, "isr_exit", name, args={"to_scheduler": event_id == 18})
        if event_id == 4:
            task = self._task(args[0])
            self._current = task
            return self._event(ts, "task_in", name, task)
        if event_id == 5:
            task, self._current = self._current, None
            return self._event(ts, "task_out", name, task)
        if event_id == 6:
            return self._event(ts, "task_ready", name, self._task(args[0]))
        if event_id == 7:
            return self._event(ts, "task_block", name, self._task(args[0]), {"cause": args[1]})
        if event_id == 8:
            return self._event(ts, "task_create", name, self._task(args[0]))
        if event_id == 9:
            task = self._task(args[0])
            info = self.trace.task(task)
            info.priority, info.name = args[1], args[2]
            if is_rtos_idle_name(args[2]):
                self.trace.idle.add(task)
            return self._event(ts, "task_info", name, task, {"priority": args[1], "name": args[2]})
        if event_id in (10, 11):
            return self._event(ts, "info", name)
        if event_id == 13:
            return self._event(ts, "info", name, args={"us": args[0] | args[1] << 32})
        if event_id == 14:
            for item in args[0].split(","):
                key, _, value = item.strip().partition("=")
                if key:
                    self.trace.system[key] = value
            return self._event(ts, "info", name, text=args[0])
        if event_id in (15, 16):
            phase = "start" if event_id == 15 else "stop"
            return self._event(ts, "marker", name, args={"marker": args[0], "phase": phase},
                               text=self._markers.get(args[0]))
        if event_id == 17:
            self._current = None
            return self._event(ts, "idle", name)
        if event_id == 19:
            return self._event(ts, "other", name, args={"timer": self._task(args[0])})
        if event_id == 20:
            return self._event(ts, "other", name)
        if event_id == 21:
            task = self._task(args[0])
            info = self.trace.task(task)
            info.stack_base, info.stack_size = args[1], args[2]
            return self._event(ts, "task_info", name, task, {"stack_base": args[1], "stack_size": args[2],
                                                             "stack_used": args[3]})
        if event_id == 22:
            self._modules.append((args[1], args[2]))
            return self._event(ts, "info", name, args={"module": args[0], "offset": args[1]}, text=args[2])
        return self._event(ts, "other", name, args={"id": args[0], "value": args[1]})     # 23 DATA_SAMPLE

    def _long(self, event_id: int, body: _Reader, delta: int) -> TraceEvent | None:
        ts = self._advance(delta, event_id)
        if event_id == 24:
            freq, cpu, base, shift = (body.varint() for _ in range(4))
            self.trace.ts_hz, self.trace.cpu_hz = float(freq), float(cpu)
            self._ram_base, self._shift = base, shift
            return self._event(ts, "info", "INIT", args={"sys_freq": freq, "cpu_freq": cpu, "ram_base": base,
                                                         "id_shift": shift})
        if event_id == 25:
            resource = self._task(body.varint())
            name = body.string()
            self._resources[resource] = name
            return self._event(ts, "info", "NAME_RESOURCE", args={"resource": resource}, text=name)
        if event_id == 26:
            fmt = body.string()
            options = body.varint()
            count = body.varint()
            values = [body.varint() for _ in range(count)]
            text = format_c(fmt, Words(values)) if count else fmt
            return self._event(ts, "print", "PRINT_FORMATTED", args={"level": _PRINT_LEVELS.get(options & 3, "info")},
                               text=text)
        if event_id == 27:
            return self._event(ts, "info", "NUMMODULES", args={"modules": body.varint()})
        if event_id == 28:
            values = []
            while body.remaining:
                values.append(body.varint())
            api = self._api.get(values[0]) if values else None
            args = {"event": values[0] if values else None}
            if len(values) > 1:
                args["return"] = values[1]
            return self._event(ts, "object", "END_CALL", args=args, text=api.name if api else None)
        if event_id == 29:
            return self._event(ts, "task_delete", "TASK_TERMINATE", self._task(body.varint()))
        if event_id == 31:
            return self._extended(ts, body)
        if event_id >= MODULE_EVENT_OFFSET and self._modules:
            return self._module_event(ts, event_id, body)
        return self._api_event(ts, event_id, body)

    def _extended(self, ts: int, body: _Reader) -> TraceEvent:
        sub = body.varint()
        name = _EX_NAMES.get(sub, f"EX{sub}")
        if sub == 0:
            marker = body.varint()
            return self._event(ts, "marker", name, args={"marker": marker}, text=self._markers.get(marker))
        if sub == 1:
            marker = body.varint()
            self._markers[marker] = body.string()
            return self._event(ts, "info", name, args={"marker": marker}, text=self._markers[marker])
        if sub in (2, 3, 4, 5):
            fields = ["heap", "ptr", "size", "tag"] if sub in (3, 4) else ["heap", "base", "size", "metadata"] \
                if sub == 2 else ["heap", "ptr"]
            values = []
            while body.remaining and len(values) < len(fields):
                values.append(body.varint())
            args = dict(zip(fields, values))
            for key in ("heap", "ptr", "base"):
                if key in args:
                    args[key] = self._task(args[key])
            if "size" in args:
                args["size"] <<= self._shift
            return self._event(ts, "object", name, args=args)
        if sub == 6:
            data_id = body.varint()
            return self._event(ts, "info", name, args={"data": data_id}, text=body.string())
        if sub == 7:
            address = body.varint()
            options = body.varint()
            values = []
            while body.remaining:
                values.append(body.varint())
            fmt = self._strings(address) if self._strings else None
            text = format_c(fmt, Words(values)) if fmt is not None else None
            args = {"level": _PRINT_LEVELS.get(options & 3, "info"), "format_address": address}
            if fmt is None:
                args["values"] = values
            return self._event(ts, "print", name, args=args, text=text)
        values = []
        while body.remaining:
            values.append(body.varint())
        return self._event(ts, "other", name, args={"values": values})

    def _api_event(self, ts: int, event_id: int, body: _Reader) -> TraceEvent:
        api = self._api.get(event_id)
        if api is None:
            values = []
            while body.remaining:
                values.append(body.varint())
            return self._event(ts, "object", f"API{event_id}", args={"values": values})
        parts, args = [], {}
        for label, fmt in api.params:
            if not body.remaining:
                break
            if fmt == "s":
                value = body.string()
                shown = value
            else:
                value = body.varint()
                shown = self._show(fmt, value)
            key = label or f"arg{len(args)}"
            args[key] = value
            parts.append(f"{label}={shown}" if label else shown)
        return self._event(ts, "object", api.name, args=args, text=" ".join(parts) or None)

    def _show(self, fmt: str, value: int) -> str:
        if fmt in ("u", "D"):
            return str(value)
        if fmt in ("d", "i"):
            return str(value - (1 << 32) if value >> 31 and value < 1 << 32 else value)
        if fmt in ("x", "X"):
            return f"0x{value:X}"
        if fmt == "p":
            return f"0x{self._task(value):08X}"
        if fmt == "I":
            address = self._task(value)
            return self._resources.get(address, f"0x{address:08X}")
        if fmt == "t":
            address = self._task(value)
            info = self.trace.tasks.get(address)
            return info.name if info and info.name else f"0x{address:08X}"
        if fmt in ("b", "B"):
            return "true" if value else "false"
        if fmt in self._named:
            return self._named[fmt].get(value, str(value))
        return str(value)

    def _module_event(self, ts: int, event_id: int, body: _Reader) -> TraceEvent:
        values = []
        while body.remaining:
            values.append(body.varint())
        offset, desc = max(((o, d) for o, d in self._modules if o <= event_id), default=(MODULE_EVENT_OFFSET, ""))
        module = re.match(r"M=([^,]*)", desc)
        name = f"{module[1] if module else 'module'}+{event_id - offset}"
        return self._event(ts, "object", name, args={"values": values})


def decode_systemview(data: bytes, description: str | None = None, strings=None) -> Trace:
    """一まとまりのデータ（.SVDat・RTT のリングのスナップショット）を解読する。sync が無ければ SyncNotFound。"""
    decoder = SystemViewDecoder(synced=False, description=description, strings=strings)
    decoder.feed(strip_svdat_header(data))
    if not decoder.synced and not decoder.trace.events:
        raise SyncNotFound("sync（0x00 × 10）が無い: SystemView の記録でないか、リングが短すぎる")
    if decoder.skipped:
        decoder.trace.notes.append(f"最初の sync までの {decoder.skipped} バイトを読み飛ばした（パケットの途中から始まる）")
    if not decoder._ts_known:
        decoder.trace.notes.append("SYSTIME_CYCLES が無い: 時刻は最初の出来事からの相対")
    return decoder.trace
