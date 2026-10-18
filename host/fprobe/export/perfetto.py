"""共通の時間軸の記録を Perfetto へ: Chrome JSON のトレースイベント（Trace Event Format）に書き出す。

Perfetto UI（ui.perfetto.dev。データはブラウザの中に留まる）・trace_processor（SQL）・chrome://tracing で開ける。

    write_trace(recording, "trace.json", t0=None, t1=None, streams=None, waveform=True, channels=None, names=None)

対応（1 つのプロセス「fprobe」の中に、トラック = スレッドを作る）:
| 記録の出来事 | Perfetto | トラック |
|---|---|---|
| rtos run（タスクの実行区間）/ gap（情報の無い区間） | スライス | タスク / タスク（情報なし） |
| rtos switch | 点（「前 → 次」） | タスク |
| rtos の生の出来事（task_in 等） | 点 | RTOS |
| swo exception（enter → exit を入れ子で組にする。return は区間を作らない） | スライス（組めない exit は点） | 割り込み |
| testpoint call（関数の入口 → 出口）| スライス | 関数（テストポイント） |
| testpoint hit（値のあるもの = 変数の印）| カウンタ「関数: 名前」 | — |
| testpoint hit（値の無いもの）・anomaly | 点 | テストポイント |
| sample word（DAP サンプラの語）| カウンタ（names で名前。無ければ番地）| — |
| aux mv（ターゲットの電圧） | カウンタ「電圧 chN (mV)」 | — |
| dap・uart | 点 | DAP / UART rx / UART tx |
| annotation mark（印）・events_lost | 全体の点 | — |
| annotation の解読（区間を持つ）| スライス | 解読 <種類> |
| host call（API の操作。所要時間から区間を作る）| スライス | 操作 <利用者> |
| ロジアナの波形 | チャンネルごとのカウンタ（0 / 1。変化だけ）と、捕捉の区間のスライス | ロジアナ |
| そのほか（target・debug・rtt・semihosting・swo の行等）| 区間を持てばスライス、無ければ点 | ストリームの名前 |
| clock（時計の対応付けの点）| 書かない | — |

- 時刻 ts は、記録の最初（origin_ticks。otherData に書く）からの µs（小数）。タイムベースの分解能を保つ。
  ホストで起きた出来事の時刻の不確かさは args の uncertainty_us。
- t0 / t1 で範囲を切る（範囲の前に始まって中に終わる区間も含める。割り込みの組は範囲の前から作る）。streams で絞る。
- 書きながら出す（大きな記録でもメモリを使わない）。Chrome JSON は並べ替えを要しない（Perfetto が読み込みで並べる）。
- 波形の捕捉の間（かたまりの間）は観測していない: カウンタは最後の値のままに見えるので、「ロジアナ」トラックの捕捉の
  スライスで観測した区間を示す。
"""

import json
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, TextIO

from ..recording import Event, Recording

__all__ = ["write_trace"]

PID = 1
TRACK_ORDER = ("タスク", "タスク（情報なし）", "割り込み", "関数（テストポイント）", "テストポイント", "RTOS",
               "ロジアナ", "DAP", "UART rx", "UART tx")
SKIPPED_STREAMS = {"clock"}


class _TraceWriter:
    """Chrome JSON の traceEvents を 1 件ずつ書く。トラック（スレッド）は初めて使うときに名前を付ける。"""

    def __init__(self, f: TextIO, hz: float, origin: int, t0: int | None, t1: int | None):
        self.f, self.hz, self.origin, self.t0, self.t1 = f, hz, origin, t0, t1
        self.tids: dict[str, int] = {}
        self.counts: Counter = Counter()
        self._first = True

    def us(self, t: float) -> float:
        return (t - self.origin) * 1e6 / self.hz

    def duration_us(self, ticks: float) -> float:
        return ticks * 1e6 / self.hz

    def in_range(self, t: float, t_end: float | None = None) -> bool:
        end = t if t_end is None else t_end
        return (self.t0 is None or (end > self.t0 if t_end is not None else t >= self.t0)) and \
            (self.t1 is None or t < self.t1)

    def write(self, event: dict) -> None:
        self.f.write(("" if self._first else ",\n") + json.dumps(event, ensure_ascii=False))
        self._first = False

    def tid(self, track: str) -> int:
        if track not in self.tids:
            tid = self.tids[track] = len(self.tids) + 1
            order = TRACK_ORDER.index(track) if track in TRACK_ORDER else len(TRACK_ORDER) + tid
            self.write({"ph": "M", "name": "thread_name", "pid": PID, "tid": tid, "args": {"name": track}})
            self.write({"ph": "M", "name": "thread_sort_index", "pid": PID, "tid": tid, "args": {"sort_index": order}})
        return self.tids[track]

    def slice(self, track: str, name: str, t: float, t_end: float, args: dict | None = None) -> None:
        event = {"ph": "X", "name": name, "pid": PID, "tid": self.tid(track), "ts": self.us(t),
                 "dur": self.duration_us(max(t_end - t, 0))}
        if args:
            event["args"] = args
        self.write(event)
        self.counts["slices"] += 1

    def instant(self, track: str | None, name: str, t: float, args: dict | None = None) -> None:
        event = {"ph": "i", "name": name, "pid": PID, "ts": self.us(t)}
        if track is None:
            event.update(s="g", tid=0)                       # 全体の点（印）
        else:
            event.update(s="t", tid=self.tid(track))
        if args:
            event["args"] = args
        self.write(event)
        self.counts["instants"] += 1

    def counter(self, name: str, t: float, value: float) -> None:
        self.write({"ph": "C", "name": name, "pid": PID, "ts": self.us(t), "args": {"value": value}})
        self.counts["counters"] += 1


def _args(w: _TraceWriter, e: Event, extra: Mapping[str, Any] | None = None, body: bool = True) -> dict:
    out: dict[str, Any] = {}
    if body and isinstance(e.body, dict):
        out.update(e.body)
    if extra:
        out.update(extra)
    if e.uncertainty is not None:
        out["uncertainty_us"] = w.duration_us(e.uncertainty)
    return out


def _describe(e: Event) -> str:
    """そのほかの出来事の名前: 種類と、本体の代表の欄。"""
    if isinstance(e.body, dict):
        for key in ("text", "message", "method", "state", "reason", "where"):
            if e.body.get(key) not in (None, ""):
                return f"{e.kind}: {e.body[key]}"
    return e.kind


def _byte(value: int) -> str:
    return f"0x{value:02X} {chr(value)!r}" if 0x20 <= value < 0x7F else f"0x{value:02X}"


def _event(w: _TraceWriter, e: Event, names: Mapping[int, str]) -> None:
    body = e.body if isinstance(e.body, dict) else {}
    s, k = e.stream, e.kind
    if s == "rtos":
        if k == "run" and e.t_end is not None:
            w.slice("タスク", body.get("task", f"0x{e.a:08X}"), e.t, e.t_end, _args(w, e, {"handle": f"0x{e.a:08X}"}))
        elif k == "gap" and e.t_end is not None:
            w.slice("タスク（情報なし）", body.get("reason", "gap"), e.t, e.t_end, _args(w, e))
        elif k == "switch":
            w.instant("タスク", f"{body.get('from', '?')} → {body.get('to', '?')}", e.t, _args(w, e))
        else:
            w.instant("RTOS", f"{k}: {body['task']}" if "task" in body else k, e.t, _args(w, e))
    elif s == "testpoint":
        where = f"{body.get('function', '?')}: {body.get('label') or body.get('line', e.a)}"
        if k == "call" and e.t_end is not None:
            w.slice("関数（テストポイント）", body.get("function", f"#{e.a}"), e.t, e.t_end, _args(w, e, {"depth": e.b}))
        elif k == "hit" and "value" in body:
            w.counter(where, e.t, body["value"])
        elif k == "anomaly":
            w.instant("テストポイント", f"anomaly: {body.get('type', '?')}", e.t, _args(w, e))
        else:
            w.instant("テストポイント", where, e.t, _args(w, e))
    elif s == "sample":
        if "ack" in body:                                            # 読めなかった語
            w.instant("サンプラ", f"読めない 0x{e.a:08X}", e.t, _args(w, e))
        else:
            w.counter(names.get(e.a, f"0x{e.a:08X}"), e.t, e.b)
    elif s == "aux":
        w.counter(f"電圧 ch{e.a} (mV)", e.t, e.b)
    elif s == "dap":
        w.instant("DAP", f"{k} 0x{e.a:08X}", e.t, _args(w, e, {"value": f"0x{e.b:08X}"}))
    elif s == "uart":
        w.instant(f"UART {k}", _byte(e.b & 0xFF), e.t, _args(w, e, {"value": e.b}))
    elif s == "annotation":
        if k == "mark":
            w.instant(None, body.get("text", "mark"), e.t, _args(w, e))
        elif e.t_end is not None:
            w.slice(f"解読 {k}", str(body.get("text") or k), e.t, e.t_end, _args(w, e))
        elif k == "events_lost":
            w.instant(None, f"イベント記録の取りこぼし {e.a} 件", e.t, _args(w, e))
        else:
            w.instant("注釈", _describe(e), e.t, _args(w, e))
    elif s == "host" and k == "call" and isinstance(body.get("seconds"), (int, float)):
        start = e.t - body["seconds"] * w.hz                          # 記録の時刻は終わったとき
        w.slice(f"操作 {body.get('client', '?')}", str(body.get("method", k)), start, e.t, _args(w, e))
    elif e.t_end is not None:
        w.slice(s, _describe(e), e.t, e.t_end, _args(w, e, {"a": e.a, "b": e.b}))
    else:
        w.instant(s, _describe(e), e.t, _args(w, e, {"a": e.a, "b": e.b}))


def _exceptions(w: _TraceWriter, events: Iterable[Event]) -> None:
    """例外トレースの enter / exit を入れ子で組にしてスライスに。範囲の前から組むので、範囲をまたぐ区間も正しい。"""
    stack: list[Event] = []

    def name(e: Event) -> str:
        return e.body.get("name") if isinstance(e.body, dict) and e.body.get("name") else f"例外 {e.a}"

    def lone(e: Event, action: str) -> None:
        if w.in_range(e.t):
            w.instant("割り込み", f"{name(e)} {action}", e.t, _args(w, e, {"number": e.a}))

    for e in events:
        action = e.body.get("action") if isinstance(e.body, dict) else None
        if action == "enter":
            stack.append(e)
        elif action == "exit":
            depth = next((i for i in range(len(stack) - 1, -1, -1) if stack[i].a == e.a), None)
            if depth is None:
                lone(e, "exit")
                continue
            for unfinished in stack[depth + 1:]:                       # 出の見えなかった入り（あふれ等）
                lone(unfinished, "enter")
            begin = stack[depth]
            del stack[depth:]
            if w.in_range(begin.t, e.t):
                unc = max(u for u in (begin.uncertainty, e.uncertainty, 0.0) if u is not None)
                args = {"number": e.a, **({"uncertainty_us": w.duration_us(unc)} if unc else {})}
                w.slice("割り込み", name(begin), begin.t, e.t, args)
    for unfinished in stack:
        lone(unfinished, "enter")


def _waveform(w: _TraceWriter, recording: Recording, channels: Iterable[int] | None) -> None:
    for base, tps, tr in recording.waveforms(w.t0, w.t1):
        first = 0 if w.t0 is None else max(0, -(-(w.t0 - base) // tps))
        last = tr.length if w.t1 is None else min(tr.length, -(-(w.t1 - base) // tps))
        if last <= first:
            continue
        w.slice("ロジアナ", "捕捉", base + first * tps, base + last * tps,
                {"channels": tr.channels, "sample_period_ns": tr.sample_period_s * 1e9})
        wanted = [ch for ch in (range(tr.channels) if channels is None else channels) if ch < tr.channels]
        i = max(bisect_right(tr.starts, first) - 1, 0)
        previous = tr.values[i] if len(tr.values) else 0
        for ch in wanted:                                              # 範囲の始まりの値
            w.counter(f"LA ch{ch}", base + first * tps, previous >> ch & 1)
        for i in range(i + 1, len(tr.starts)):
            start = tr.starts[i]
            if start >= last:
                break
            value = tr.values[i]
            changed = value ^ previous
            for ch in wanted:
                if changed >> ch & 1:
                    w.counter(f"LA ch{ch}", base + start * tps, value >> ch & 1)
            previous = value


def write_trace(recording: Recording, path: Path | str, *, t0: int | None = None, t1: int | None = None,
                streams: list[str] | None = None, waveform: bool = True, channels: list[int] | None = None,
                names: Mapping[int, str] | None = None) -> dict:
    """記録を Chrome JSON のトレースイベントに書き出す。names は DAP サンプラの語の番地 → 名前（カウンタの名前）。
    書いた件数（slices・instants・counters）とトラックの一覧を返す。"""
    hz = float(recording.meta["timebase_hz"])
    span = recording.span()
    origin = span[0] if span else 0
    names = dict(names or {})
    wanted = None if streams is None else set(streams)
    title = recording.meta.get("name") or recording.directory.name
    with Path(path).open("w", encoding="utf-8") as f:
        other = {"source": "fprobe", "recording": recording.directory.name, "timebase_hz": hz, "origin_ticks": origin,
                 "time": "ts は origin_ticks からの µs"}
        f.write('{"displayTimeUnit": "ns", "otherData": ' + json.dumps(other, ensure_ascii=False) +
                ', "traceEvents": [\n')
        w = _TraceWriter(f, hz, origin, t0, t1)
        w.write({"ph": "M", "name": "process_name", "pid": PID, "tid": 0, "args": {"name": f"fprobe {title}"}})
        for e in recording.iter_events(t0, t1, streams, overlapping=True):
            if e.stream in SKIPPED_STREAMS and (wanted is None or e.stream not in wanted):
                continue
            if e.stream == "swo" and e.kind == "exception":
                continue                                               # 組にして後で書く
            _event(w, e, names)
        if wanted is None or "swo" in wanted:
            _exceptions(w, recording.iter_events(None, t1, ["swo"], ["exception"]))
        if waveform and (wanted is None or "waveform" in wanted):
            _waveform(w, recording, channels)
        f.write("\n]}\n")
    return {"path": str(path), "format": "chrome-json", "origin_ticks": origin,
            "events": sum(w.counts.values()), **{k: w.counts[k] for k in ("slices", "instants", "counters")},
            "tracks": list(w.tids)}
