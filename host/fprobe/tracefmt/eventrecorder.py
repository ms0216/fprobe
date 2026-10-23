"""Keil の Event Recorder（CMSIS-View。KEIL-01）の RAM のバッファの解読。ターゲットを止めずに読める（走行中の
読みは since で続きから）。出典: ARM-software/CMSIS-View EventRecorder/Source/EventRecorder.c（プロトコル 1.1）。

- `EventRecorderInfo`（ELF のシンボル。24 バイト）: protocol_type・version・記録の数（2 の冪）・バッファ・
  フィルタ・状態の番地・時刻の源。
- 状態（36 バイト）: state・context・CRC・record_index（次に書く通し番号）・written・dumped・ts_overflow・
  ts_freq・ts_last・init_count・signature（0xE1A5276B）。
- 記録（16 バイト）: ts・val1・val2・info。info = ID（16 ビット）・DLEN / context（bit18..16）・IRQ（19）・
  周回（23..20 = (通し番号 / 数) & 0xF）・FIRST（24）・LAST（25）・LOCKED（26）・VALID（27）・ts / val1 / val2 の
  本当の bit31（28..30）・トグル（31）。ts・val1・val2 の bit31 にはトグルが入る（書きかけの検出）。
- 複数の記録の出来事: EventRecord4（FIRST の後に ID 0x0001 の LAST）、EventRecordData の 9 バイト以上
  （FIRST の後に 0xFF01, 0xFF02, … の途中、最後は (残り << 8) | 個数 の LAST）。context で対応付ける。
- 0xFE00 は printf の出力（stdout。最大 8 文字ずつ）、0xFF00〜0xFF03 は記録の制御（INIT・START・STOP・CLOCK）。
レベル（id の bit17..16）は記録に残らないので、SCVD の定義から取る。名前・値の書式も SCVD（`Scvd.format_event`）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .model import Trace, TraceEvent, is_rtos_idle_name, unwrap32
from .scvd import Scvd, ScvdContext

__all__ = ["INFO_SIZE", "INFO_SYMBOL", "RECORD_SIZE", "STATUS_SIZE", "EventRecorderError", "decode_event_recorder",
           "parse_info", "parse_status"]

INFO_SYMBOL = "EventRecorderInfo"
INFO_SIZE, STATUS_SIZE, RECORD_SIZE = 24, 36, 16
SIGNATURE = 0xE1A5276B
_FIRST, _LAST, _LOCKED, _VALID, _IRQ = 1 << 24, 1 << 25, 1 << 26, 1 << 27, 1 << 19
_CONTROL = {0xFF00: "INIT", 0xFF01: "START", 0xFF02: "STOP", 0xFF03: "CLOCK"}
STDOUT = 0xFE00
_TS_SOURCES = {0: "DWT CYCCNT", 1: "SysTick", 2: "CMSIS-RTOS2 System Timer", 3: "User Timer (Normal Reset)",
               4: "User Timer (Power-On Reset)"}


class EventRecorderError(Exception):
    pass


@dataclass(frozen=True)
class RecorderInfo:
    protocol_type: int
    version: int
    record_count: int
    buffer: int
    filter: int
    status: int
    ts_source: int


@dataclass(frozen=True)
class RecorderStatus:
    state: int
    context: int
    record_index: int
    written: int
    dumped: int
    ts_overflow: int
    ts_freq: int
    ts_last: int
    init_count: int
    signature: int


def parse_info(data: bytes) -> RecorderInfo:
    if len(data) < INFO_SIZE:
        raise EventRecorderError("EventRecorderInfo が短い")
    ptype, _, version, count, buffer, filt, status, ts_source = struct.unpack_from("<BBHIIIIB", data)
    if ptype != 1 or count == 0 or count & (count - 1):
        raise EventRecorderError(f"EventRecorderInfo の形が違う（protocol_type {ptype}・記録の数 {count}）")
    return RecorderInfo(ptype, version, count, buffer, filt, status, ts_source)


def parse_status(data: bytes) -> RecorderStatus:
    if len(data) < STATUS_SIZE:
        raise EventRecorderError("Event Recorder の状態が短い")
    state, context, _crc, *rest = struct.unpack_from("<BBH8I", data)
    status = RecorderStatus(state, context, *rest)
    if status.signature != SIGNATURE:
        raise EventRecorderError("Event Recorder の状態の signature が無い: EventRecorderInitialize を呼んでいない")
    return status


@dataclass
class _Pending:
    id: int
    irq: bool
    ts: int
    values: list[int]
    data: bytearray
    kind: str                         # record4 / data


def decode_event_recorder(info: RecorderInfo, status: RecorderStatus, buffer: bytes, scvd: Scvd | None = None,
                          ctx: ScvdContext | None = None, since: int | None = None,
                          ts_previous: int | None = None) -> tuple[Trace, int]:
    """バッファ（記録の数 × 16 バイト）→ (Trace, 次に読む通し番号)。since を渡すとその通し番号からの続きだけ。"""
    count = info.record_count
    if len(buffer) < count * RECORD_SIZE:
        raise EventRecorderError("バッファが短い")
    trace = Trace("eventrecorder", ts_hz=float(status.ts_freq) if status.ts_freq else None)
    trace.system = {"ts_source": _TS_SOURCES.get(info.ts_source, info.ts_source), "records": count,
                    "written": status.written, "dumped": status.dumped, "running": bool(status.state),
                    "version": f"{info.version >> 8}.{info.version & 0xFF}"}
    end = status.record_index
    first = max(end - count, 0)
    if since is not None:
        if end - since > count:
            trace.lost += end - since - count
            trace.notes.append(f"前回の読みから {end - since - count} 個の記録が上書きされた（読む周期を短く）")
        first = max(first, since)
    if status.dumped:
        trace.notes.append(f"ターゲットが書けずに捨てた記録 {status.dumped} 個（ロック中の衝突）")
    pending: dict[int, _Pending] = {}
    stdout = bytearray()
    stdout_ts: int | None = None
    ts_last = ts_previous
    torn = 0
    for i in range(first, end):
        ts, v1, v2, word = struct.unpack_from("<4I", buffer, (i & (count - 1)) * RECORD_SIZE)
        if not word & _VALID or word & _LOCKED or (word >> 20 & 0xF) != (i // count) & 0xF:
            torn += 1
            continue
        tbit = word >> 31
        if (ts >> 31, v1 >> 31, v2 >> 31) != (tbit, tbit, tbit):
            torn += 1
            continue
        ts = ts & 0x7FFF_FFFF | (word >> 28 & 1) << 31
        v1 = v1 & 0x7FFF_FFFF | (word >> 29 & 1) << 31
        v2 = v2 & 0x7FFF_FFFF | (word >> 30 & 1) << 31
        ts_last = unwrap32(ts_last, ts)
        event_id, ctx_no, irq = word & 0xFFFF, word >> 16 & 7, bool(word & _IRQ)
        first_rec, last_rec = bool(word & _FIRST), bool(word & _LAST)
        raw = struct.pack("<II", v1, v2)
        done: tuple | None = None
        if first_rec and last_rec:
            length = ctx_no
            done = (event_id, irq, ts_last, [v1, v2], raw[:length] if length else None)
        elif first_rec:
            pending[ctx_no] = _Pending(event_id, irq, ts_last, [v1, v2], bytearray(raw), "?")
        elif not last_rec and event_id >> 8 == 0xFF and ctx_no in pending:
            pending[ctx_no].data += raw
            pending[ctx_no].kind = "data"
        elif last_rec and ctx_no in pending:
            p = pending.pop(ctx_no)
            if event_id == 0x0001 and p.kind == "?":
                done = (p.id, p.irq, p.ts, p.values + [v1, v2], None)
            elif 1 <= event_id >> 8 <= 8:
                rem, n = event_id >> 8, event_id & 0xFF
                data = bytes(p.data) + raw[:rem]
                if len(data) != 8 * n + rem:
                    trace.notes.append(f"0x{p.id:04X} のデータの長さが合わない（{len(data)} と {8 * n + rem}）")
                done = (p.id, p.irq, p.ts, p.values, data)
            else:
                torn += 1
        elif not first_rec and not last_rec and event_id >> 8 != 0xFF:
            done = (event_id, irq, ts_last, [v1, v2], b"")                 # EventRecordData の長さ 0
        else:
            torn += 1                                                       # 始まりを失った続き
        if done is None:
            continue
        event_id, irq, ts_ev, values, data = done
        if event_id == STDOUT:
            text = (data if data is not None else raw).decode("latin-1")
            if stdout_ts is None:
                stdout_ts = ts_ev
            stdout += text.encode("latin-1")
            while b"\n" in stdout:
                line, _, rest = bytes(stdout).partition(b"\n")
                trace.events.append(TraceEvent(stdout_ts, "print", "stdout", None, {"level": "info"},
                                               line.decode("utf-8", errors="replace").rstrip("\r")))
                stdout, stdout_ts = bytearray(rest), (ts_ev if rest else None)
            continue
        trace.events.append(_event(trace, event_id, irq, ts_ev, values, data, scvd, ctx))
    if stdout:
        trace.events.append(TraceEvent(stdout_ts, "print", "stdout", None, {"level": "info", "partial": True},
                                       stdout.decode("utf-8", errors="replace")))
    if pending:
        trace.notes.append(f"終わりの記録の無い複数の記録の出来事 {len(pending)} 個（書きかけ）")
    if torn:
        trace.lost += torn
        trace.notes.append(f"読めなかった記録 {torn} 個（書きかけ・上書き・始まりを失った続き）")
    trace.events.sort(key=lambda e: e.ts)
    return trace, end


def _event(trace: Trace, event_id: int, irq: bool, ts: int, values: list[int], data: bytes | None,
           scvd: Scvd | None, ctx: ScvdContext | None) -> TraceEvent:
    args: dict = {"id": event_id, "component": event_id >> 8, "message": event_id & 0xFF, "irq": irq,
                  "values": values}
    if data is not None:
        args["data"] = data.hex()
    if event_id in _CONTROL:
        name = _CONTROL[event_id]
        if name == "INIT" and values[1]:
            trace.ts_hz = float(values[1])
        if name == "CLOCK" and values[0]:
            trace.ts_hz = float(values[0])
        return TraceEvent(ts, "info", name, None, args)
    named = {f"val{i + 1}": v for i, v in enumerate(values + [0] * (4 - len(values)))}
    if data is not None and len(data) >= 8:
        named.update(val1=int.from_bytes(data[:4], "little"), val2=int.from_bytes(data[4:8], "little"))
    out = scvd.format_event(event_id, named, ctx) if scvd is not None else None
    if out is None:
        return TraceEvent(ts, "other", f"0x{event_id:04X}", None, args)
    args.update(level=out["level"], component_name=out["component"])
    handle = out.get("handle")
    kind = "other"
    if out.get("tracking") == "Start":
        kind = "task_create" if out.get("component") and "Thread" in out["component"] else "object"
    elif out.get("tracking") == "Stop":
        kind = "task_delete" if out.get("component") and "Thread" in out["component"] else "object"
    elif out.get("unique"):
        kind = "task_in"
    elif out.get("state") or handle is not None:
        kind = "object"
    if kind in ("task_create", "task_in", "task_delete") and handle is not None:
        info = trace.task(handle)
        if out.get("name"):
            info.name = out["name"]
            if is_rtos_idle_name(info.name):
                trace.idle.add(handle)
    if out.get("state"):
        args["state"] = out["state"]
    if out.get("name"):
        args["name"] = out["name"]
    return TraceEvent(ts, kind, out["property"], handle, args, out["value"] or None)
