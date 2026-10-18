"""Probe の道具: ヒープの解析（U27）。走査（使用量・断片化・壊れ）と、malloc / free の記録（ブレークポイント）からの
リークの候補。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・dbg・_symbols・_where / _place_of・
_record_host・_watch_stops と、BreakpointActions の _break_hooks（道具が置くブレークポイントの処理）を使う。
機器に触るのは必ず _lock の中。

記録の方法（ブレークポイント）: 確保の関数（FreeRTOS は pvPortMalloc、newlib は _malloc_r）の入口で大きさと戻り先を
読み、戻り先に一時のブレークポイントを置いて、戻ったところで返した番地（r0）を読む。解放の関数（vPortFree・
_free_r）は入口で番地を読む。calloc・realloc は中で _malloc_r / _free_r を呼ぶので確保と解放として残る（その場で
大きさを変えた realloc は見えない）。呼び出し元は戻り先（calloc 等の包みの中なら呼び出し履歴で外へ）。
当たるたびにコアを数 ms 止めるので、確保が毎秒数百回を超えるファームには向かない（ponytail: 速い頻度はトレース
フック traceMALLOC / traceFREE を ITM に流す方法で。未実装）。

仕様: docs/superpowers/specs/2026-10-18-u23-u27-long-recording-heap-design.md
"""

import collections
import time
from dataclasses import dataclass, field

from pyocd.core.target import Target

from .debug import PyocdAccess
from .heap import ALLOCATORS, AllocationTracker, SymbolTable, detect, scan

__all__ = ["HeapTools"]

TOOL = "heap_trace"
EVENT_LIMIT = 10_000
STREAM = "heap"
# アロケータ → [(関数, 確保 / 解放, 番地・大きさの入るレジスタ)]。newlib-nano も同じ関数名（_malloc_r(reent, size)）
FUNCTIONS = {"freertos": [("pvPortMalloc", "alloc", "r0"), ("vPortFree", "free", "r0")],
             "newlib": [("_malloc_r", "alloc", "r1"), ("_free_r", "free", "r1")]}
FALLBACK = {"newlib": [("malloc", "alloc", "r0"), ("free", "free", "r0")]}
WRAPPERS = frozenset({"malloc", "free", "calloc", "realloc", "_calloc_r", "_realloc_r", "_malloc_r", "_free_r",
                      "reallocf", "_reallocf_r", "memalign", "_memalign_r", "strdup", "_strdup_r", "strndup",
                      "_strndup_r", "pvPortMalloc", "vPortFree", "pvPortCalloc", "__wrap_malloc", "__wrap_free"})


@dataclass
class _Entry:
    function: str
    op: str                              # alloc / free
    register: str
    allocator: str


@dataclass
class _HeapTrace:
    entries: dict[int, _Entry]
    started: float
    tracker: AllocationTracker = field(default_factory=AllocationTracker)
    pending: dict[int, list[tuple[float, int, dict, str]]] = field(default_factory=dict)   # 戻り先 → 確保の途中
    owned: set[int] = field(default_factory=set)                                          # 置いた戻り先
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=EVENT_LIMIT))
    seq: int = 0
    active: bool = True
    notes: list[str] = field(default_factory=list)


class HeapTools:
    def _init_heap(self) -> None:
        self._heap_trace: _HeapTrace | None = None
        self._heap_last: dict[str, dict[int, int]] = {}          # アロケータ → 前回の走査の使用中 {番地: 大きさ}

    def _heap_symbols(self) -> SymbolTable:
        if self._symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load）")
        return SymbolTable(self._symbols)

    # --- 走査 ----------------------------------------------------------------------------------

    def heap_scan(self, allocator: str | None = None, regions: list[list[int]] | None = None, end: int | None = None,
                  live: bool = False, blocks: int = 0, compare: bool = False) -> dict:
        """ヒープを辿る: 使用量・空き・最大の空き・断片化・大きさの分布・壊れ（issues）。allocator を省くと ELF にある
        もの全部（freertos / newlib_nano / newlib）。止まっているときだけ（live なら走行中にも: 一貫しない）。"""
        with self._lock:
            syms = self._heap_symbols()
            names = [allocator] if allocator else detect(syms)
            if allocator is not None and allocator not in ALLOCATORS:
                raise ValueError(f"allocator は {' / '.join(ALLOCATORS)}")
            if not names:
                raise RuntimeError("ELF に既知のアロケータ（FreeRTOS heap_4/5・newlib・newlib-nano）が無い")
            target = self.target
            halted = target.get_state() == Target.State.HALTED
            if not halted and not live:
                raise RuntimeError("ターゲットが動いている: 止めてから（target_halt）。live=true で走行中にも読める"
                                   "（ブロックが読む間に変わり得る）")
            access = PyocdAccess(target)
            pairs = [(int(r[0]), int(r[1])) for r in regions] if regions else None
            trace = self._heap_trace
            heaps = []
            for name in names:
                result = scan(access.read_memory, syms, name, regions=pairs, end=end)
                d = result.to_dict(blocks)
                used = {b.pointer: b.size for b in result.blocks if b.used}
                if trace is not None:
                    d["callers"] = _callers(used, trace.tracker)
                if compare:
                    d["changes"] = _changes(self._heap_last.get(name), used)
                self._heap_last[name] = used
                heaps.append(d)
                self._record_host(STREAM, "scan", time.monotonic(), a=d["used"], b=d["free"],
                                  body={"allocator": name, "largest_free": d["largest_free"],
                                        "issues": len(d["issues"])})
            return {"heaps": heaps, "halted": halted, "consistent": halted}

    # --- malloc / free の記録 -------------------------------------------------------------------

    def heap_trace_start(self, allocators: list[str] | None = None) -> dict:
        """malloc / free の記録を始める（確保・解放の関数にブレークポイント。止めずに続ける）。記録中なら
        stream "heap" にも入る。→ `heap_trace_report`（リークの候補）/ `heap_trace_events`。"""
        with self._lock:
            if self._heap_trace is not None and self._heap_trace.active:
                raise RuntimeError("記録している（heap_trace_stop を先に）")
            syms = self._heap_symbols()
            names = allocators or detect(syms)
            entries: dict[int, _Entry] = {}
            for name in names:
                key = "newlib" if name in ("newlib", "newlib_nano") else name
                if key not in FUNCTIONS:
                    raise ValueError(f"allocators は {' / '.join(ALLOCATORS)}")
                specs = FUNCTIONS[key] if syms.address(FUNCTIONS[key][0][0]) is not None else FALLBACK.get(key, [])
                for function, op, register in specs:
                    address = syms.address(function)
                    if address is not None:
                        entries[address] = _Entry(function, op, register, key)
            if not entries:
                raise RuntimeError("確保・解放の関数（pvPortMalloc / _malloc_r / malloc 等）が ELF に無い")
            dbg = self.dbg
            taken = [a for a in entries if a in dbg.breakpoints]
            if taken:
                raise RuntimeError(f"確保・解放の関数の入口にブレークポイントがある（{', '.join(hex(a) for a in taken)}）:"
                                   " 外してから")
            placed = []
            try:
                for address in entries:
                    dbg.breakpoint(address)
                    placed.append(address)
            except Exception:
                for address in placed:
                    dbg.remove_breakpoint(address)
                raise
            trace = _HeapTrace(entries, time.monotonic())
            self._heap_trace = trace
            for address in entries:
                self._break_hooks[address] = (TOOL, self._heap_entry_hit)
            self._watch_stops(self.target.get_state() != Target.State.HALTED)
            return {"functions": [{"function": e.function, "op": e.op, "address": a} for a, e in entries.items()],
                    "notes": ["当たるたびにコアを数 ms 止める（確保の多いファームでは遅くなる）"]}

    def heap_trace_stop(self) -> dict:
        """記録を止める（ブレークポイントを外す。記録した出来事と要約は heap_trace_report で読める）。"""
        with self._lock:
            trace = self._heap_trace
            if trace is None or not trace.active:
                raise RuntimeError("記録していない（heap_trace_start を先に）")
            for address in list(trace.entries) + list(trace.owned):
                self._break_hooks.pop(address, None)
                if address in self.dbg.breakpoints:
                    self.dbg.remove_breakpoint(address)
            trace.owned.clear()
            if trace.pending:
                trace.notes.append(f"止めたときに戻っていない確保が {sum(map(len, trace.pending.values()))} 件あった")
                trace.pending.clear()
            trace.active = False
            r = trace.tracker
            return {"allocations": r.allocations, "frees": r.frees, "live": {"count": len(r.live), "bytes": r.bytes},
                    "peak_bytes": r.peak}

    def _heap_trace_detached(self) -> None:
        """すべてのブレークポイントが外された（breakpoint_clear）: 記録は止まる。"""
        trace = self._heap_trace
        if trace is not None and trace.active:
            for address in list(trace.entries) + list(trace.owned):
                self._break_hooks.pop(address, None)
            trace.active = False
            trace.notes.append("breakpoint_clear でブレークポイントが外れたので記録が止まった")

    def heap_trace_report(self, min_age_s: float = 1.0, top: int = 20) -> dict:
        """記録の要約: 確保・解放の回数、生きている確保、最大の使用量、誤り（二重の解放・失敗・解放されていない番地の
        再利用）、リークの候補（呼び出し元ごと。min_age_s より古い生きている確保。growing = 増え続ける）、使用量の推移。
        時刻は記録を始めてからの秒。"""
        with self._lock:
            trace = self._heap_trace
            if trace is None:
                raise RuntimeError("記録が無い（heap_trace_start を先に）")
            now = time.monotonic()
            out = trace.tracker.report(now, min_age_s, top)
            t0 = trace.started
            for issue in out["issues"]:
                issue["t"] = round(issue["t"] - t0, 6)
            for leak in out["leak_candidates"]:
                leak["oldest_age_s"] = round(now - leak.pop("oldest_t"), 3)
            out["series"] = [(round(t - t0, 6), b) for t, b in out["series"]]
            out.update(active=trace.active, elapsed_s=round(now - t0, 3), notes=list(trace.notes),
                       functions=sorted({e.function for e in trace.entries.values()}))
            return out

    def heap_trace_events(self, since: int = 0, limit: int = 200) -> dict:
        """記録した確保・解放の出来事（seq が since より後を古い順に limit 件）。next を次の since に。"""
        with self._lock:
            trace = self._heap_trace
            if trace is None:
                raise RuntimeError("記録が無い（heap_trace_start を先に）")
            items = [e for e in trace.events if e["seq"] > since][:limit]
            return {"events": items, "next": items[-1]["seq"] if items else since}

    # --- ブレークポイントの処理（_lock の中で。BreakpointActions._break_action から） ---------------------------

    def _heap_entry_hit(self, pc: int) -> bool | None:
        trace = self._heap_trace
        entry = trace.entries.get(pc) if trace is not None and trace.active else None
        if entry is None:
            return None
        target = self.target
        value = target.read_core_register(entry.register)
        lr = target.read_core_register("lr")
        caller = self._heap_caller(lr)
        now = time.monotonic()
        if entry.op == "free":
            trace.tracker.free(now, value, caller["where"])
            self._heap_event(trace, now, "free", value, 0, caller, entry)
            return True
        ret = lr & ~1
        trace.pending.setdefault(ret, []).append((now, value, caller, entry.allocator))
        if ret not in trace.owned and ret not in self._break_hooks:
            if ret in self.dbg.breakpoints:                       # 使う人のブレークポイント: 処理だけ足す
                self._break_hooks[ret] = (TOOL, self._heap_return_hit)
            else:
                self.dbg.breakpoint(ret)
                trace.owned.add(ret)
                self._break_hooks[ret] = (TOOL, self._heap_return_hit)
        return True

    def _heap_return_hit(self, pc: int) -> bool | None:
        trace = self._heap_trace
        waiting = trace.pending.get(pc) if trace is not None else None
        if not waiting:
            return None
        started, size, caller, allocator = waiting.pop()
        pointer = self.target.read_core_register("r0")
        trace.tracker.alloc(started, pointer, size, caller["where"])
        self._heap_event(trace, started, "alloc", pointer, size, caller, _Entry("", "alloc", "r0", allocator))
        if not waiting:
            del trace.pending[pc]
            self._break_hooks.pop(pc, None)
            if pc in trace.owned:
                trace.owned.discard(pc)
                self.dbg.remove_breakpoint(pc)
                return True
            return None                                           # 使う人のブレークポイント: ふつうの動作へ
        return True

    def _heap_caller(self, lr: int) -> dict:
        """呼び出し元の場所（戻り先の 1 つ前 = 呼び出しの命令）。calloc 等の包みの中なら呼び出し履歴で外へ。"""
        ret = lr & ~1
        syms = self._symbols
        function = syms.function_at(ret) if syms is not None else None
        if function is not None and function.name in WRAPPERS:
            try:
                for frame in self.dbg.backtrace(max_frames=8)[1:]:
                    f = syms.function_at(frame.pc)
                    if f is not None and f.name not in WRAPPERS:
                        return {"address": frame.pc, **self._place_of(max(frame.pc - 2, 0))}
            except Exception:                                     # 巻き戻せない: 戻り先のまま
                pass
        return {"address": ret, **self._place_of(max(ret - 2, 0))}

    def _heap_event(self, trace: _HeapTrace, t: float, op: str, pointer: int, size: int, caller: dict,
                    entry: _Entry) -> None:
        trace.seq += 1
        trace.events.append({"seq": trace.seq, "t": round(t - trace.started, 6), "op": op, "pointer": pointer,
                             "size": size, "caller": caller["where"], "allocator": entry.allocator})
        self._record_host(STREAM, op, t, a=pointer, b=size, body={"caller": caller["where"],
                                                                  "allocator": entry.allocator})


def _callers(used: dict[int, int], tracker: AllocationTracker) -> list[dict]:
    """走査の使用中のブロックを、記録の確保（呼び出し元）に対応付けて呼び出し元ごとに。"""
    groups: dict[str, dict] = {}
    for pointer, size in used.items():
        live = tracker.live.get(pointer)
        caller = live.caller if live is not None else "(記録の前の確保)"
        g = groups.setdefault(caller, {"caller": caller, "count": 0, "bytes": 0})
        g["count"] += 1
        g["bytes"] += size
    return sorted(groups.values(), key=lambda g: -g["bytes"])


def _changes(previous: dict[int, int] | None, now: dict[int, int], limit: int = 50) -> dict:
    """前回の走査からの使用中のブロックの差（増えたものはリークの手がかり）。"""
    if previous is None:
        return {"first": True}
    new = sorted(set(now) - set(previous))
    gone = sorted(set(previous) - set(now))
    return {"first": False, "new": [{"pointer": p, "size": now[p]} for p in new[:limit]],
            "gone": [{"pointer": p, "size": previous[p]} for p in gone[:limit]], "new_count": len(new),
            "gone_count": len(gone), "new_bytes": sum(now[p] for p in new), "gone_bytes": sum(previous[p] for p in gone)}
