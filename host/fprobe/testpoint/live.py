"""テストポイントを流しながら解析する（API・MCP の道具 `testpoint_*` の中身。機器に依らない）。

受け口（ロジアナ・SWO・RTT）が解読した Hit / Loss を `LiveAnalysis` に何度でも渡す。解析は逐次でメモリが一定
（呼び出しの一覧は最近の calls_limit 件だけ、変数は最新・最小・最大だけ）なので、何時間でも流せる。
記録中なら、時刻の写し（`TimeMap`。源の時刻 → デバイスのタイムベース）を決めてから、stream "testpoint" に書く
（record.RecorderSink）。

- `TimeMap`: GPIO はロジアナのサンプル番号（捕捉のトリガ時刻か、流し始めの時刻から。確か）、ITM は SWO のバイトに
  FPGA が付けた時刻（タイムベースそのもの）、RTT はターゲットの CYCCNT（ホストの時計で写すので不確かさが付く）。
- `TimedSwo`: デバイスの時刻付きの SWO のバイト → (パケットの先頭のバイトの時刻, ITM パケット)。逐次。
- `report_dict` / `calls_dict` / `coverage_dict`: 道具の答え（JSON）。時刻は解析の最初の印からの秒（t_s）と、
  写しがあればタイムベースの時刻（t。記録・タイムラインのその時刻へ跳ぶ鍵）。

仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 7 節（組み込み）
"""

import threading
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass

from ..itm import ItmDecoder as ItmPackets
from ..itm import Packet
from ..recording import Recorder
from .analysis import Analyzer, Anomaly, Call, CallTreeNode, Coverage, Report, render_calls
from .probes import Probe, ProbeTable
from .record import RecorderSink
from .trace import Hit, Item

__all__ = ["CALLS_LIMIT", "LiveAnalysis", "TimeMap", "TimedSwo", "calls_dict", "coverage_dict", "report_dict"]

CALLS_LIMIT = 1 << 16                   # 持っておく最近の呼び出しの数（古いものから捨てる。統計は全部）


@dataclass(frozen=True)
class TimeMap:
    """源の時刻（整数、1 秒 = hz）→ タイムベース: ticks = offset + round(t × timebase_hz / hz)。
    uncertainty はタイムベースの単位（0 = 確か）。"""
    hz: float
    timebase_hz: float
    offset: int = 0
    uncertainty: float = 0.0

    def ticks(self, t: int) -> int:
        return self.offset + round(t * self.timebase_hz / self.hz)

    def to_dict(self) -> dict:
        return {"timebase_hz": self.timebase_hz, "offset": self.offset, "uncertainty_ticks": self.uncertainty}


class TimedSwo:
    """デバイスの時刻付きの SWO のバイト (時刻, バイト) → (パケットの先頭のバイトの時刻, パケット)。逐次（パケットの
    途中で分けて渡してよい）。解読し終えたバイトの時刻は捨てる（長時間でもメモリが一定）。"""

    def __init__(self):
        self._decoder = ItmPackets()
        self._times: list[int] = []
        self._base = 0                          # _times[0] のバイトの通し番号

    def feed(self, events: Iterable[tuple[int, int]]) -> list[tuple[int, Packet]]:
        data = bytearray()
        for t, b in events:
            self._times.append(t)
            data.append(b)
        packets = self._decoder.feed(bytes(data))
        if not self._times:
            return []
        last = len(self._times) - 1
        out = [(self._times[min(max(p.offset - self._base, 0), last)], p) for p in packets]
        if packets:
            drop = min(max(packets[-1].offset - self._base, 0), last)
            del self._times[:drop]
            self._base += drop
        return out


class LiveAnalysis:
    """Hit / Loss を流しながら解析する（スレッド安全: 受け口のスレッドと道具の呼び出しが同時に触ってよい）。

    recorder があれば、anchor で時刻の写しを決めた後から記録に書く（決める前に feed すると誤り）。
    options は Analyzer へ（bucket_s・silence_s・open_s・long_s 等）。"""

    def __init__(self, table: ProbeTable, hz: float, *, recorder: Recorder | None = None,
                 calls_limit: int = CALLS_LIMIT, **options):
        self.table, self.hz, self.recorder = table, hz, recorder
        self.time_map: TimeMap | None = None
        self._sink: RecorderSink | None = None
        self._calls: deque[Call] = deque(maxlen=calls_limit)
        self._calls_seen = 0
        self._final: Report | None = None
        self._lock = threading.Lock()
        self._analyzer = Analyzer(table, hz, keep_calls=False, keep_values=False, on_call=self._on_call,
                                  on_hit=self._on_hit, on_anomaly=self._on_anomaly, **options)

    # --- 受け取る -------------------------------------------------------------------------------

    def anchor(self, time_map: TimeMap) -> None:
        """時刻の写しを決める（最初の 1 回だけ。2 回目以降は無視する: 記録の時刻が途中で変わらないように）。"""
        with self._lock:
            if self.time_map is not None:
                return
            self.time_map = time_map
            if self.recorder is not None:
                self._sink = RecorderSink(self.recorder, self.hz, time_map.offset, time_map.uncertainty or None)

    def feed(self, items: Iterable[Item]) -> None:
        items = list(items)
        if not items:
            return
        with self._lock:
            if self._final is not None:
                raise RuntimeError("解析を終えた後に受け取った")
            if self.recorder is not None and self._sink is None:
                raise RuntimeError("記録の時刻の写しが決まっていない（anchor を先に）")
            self._analyzer.feed(items)

    def _on_call(self, call: Call) -> None:
        self._calls.append(call)
        self._calls_seen += 1
        if self._sink is not None:
            self._sink.call(call)

    def _on_hit(self, hit: Hit, probe: Probe, function: str) -> None:
        if self._sink is not None:
            self._sink.hit(hit, probe, function)

    def _on_anomaly(self, anomaly: Anomaly) -> None:
        if self._sink is not None:
            self._sink.anomaly(anomaly)

    # --- 読む -----------------------------------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._final is not None

    @property
    def hits(self) -> int:
        return self._analyzer.hits

    @property
    def losses(self) -> int:
        return self._analyzer.losses

    def report(self, final: bool = False) -> Report:
        """要約。final = False は途中の要約（何度でも）、True で終える（開いたままの入口に no_exit を付け、記録の
        meta に要約を書いて flush する。以後は同じ要約を返し、feed は誤り）。"""
        with self._lock:
            if self._final is not None:
                return self._final
            report = self._analyzer.finish(final=final)
            if final:
                self._final = report
                if self.recorder is not None:
                    self.recorder.update_meta(testpoint={"probes": len(self.table), "hz": self.hz,
                                                         "coverage": {"c0": report.coverage.c0,
                                                                      "c1": report.coverage.c1}})
                    self.recorder.flush()
            return report

    def calls(self) -> tuple[list[Call], int]:
        """(最近の呼び出し（終わった順）, 上限を超えて捨てた数)。"""
        with self._lock:
            return list(self._calls), self._calls_seen - len(self._calls)


# --- 道具の答え（JSON） ---------------------------------------------------------------------------

def _origin(report: Report) -> int:
    return report.t_first or 0


def _when(t: int, report: Report, time_map: TimeMap | None) -> dict:
    out = {"t_s": (t - _origin(report)) / report.hz}
    if time_map is not None:
        out["t"] = time_map.ticks(t)
    return out


def _tree(node: CallTreeNode, hz: float, depth: int) -> dict:
    out = {"function": node.function, "count": node.count, "inclusive_s": node.inclusive / hz,
           "exclusive_s": node.exclusive / hz, "children": []}
    children = sorted(node.children.values(), key=lambda n: -n.inclusive)
    if depth > 0:
        out["children"] = [_tree(c, hz, depth - 1) for c in children]
    elif children:
        out["children_omitted"] = len(children)
    return out


def report_dict(report: Report, time_map: TimeMap | None, *, top: int = 20, anomalies: int = 100,
                buckets: int = 120, tree_depth: int = 8) -> dict:
    """解析の要約: 関数（子を含む時間の合計の多い順に top 個）・カバレッジの割合・異常（種類ごとの数と先頭 anomalies 件）・
    時間の区切り（最後の buckets 個）・変数・開いたままの呼び出し・呼び出しの木（深さ tree_depth まで）。"""
    hz = report.hz
    funcs = sorted(report.functions.values(), key=lambda f: -f.total_inclusive)
    cov = report.coverage
    out = {
        "hz": hz, "hits": report.hits, "losses": report.losses,
        "span_s": None if report.t_first is None else (report.t_last - report.t_first) / hz,
        "functions_total": len(funcs), "functions": [f.to_dict(hz) for f in funcs[:top]],
        "coverage": {"functions": [cov.functions_hit, cov.functions_total], "c0": [cov.c0_hit, cov.c0_total],
                     "c1": [cov.c1_hit, cov.c1_total], "function_ratio": cov.function_ratio, "c0_ratio": cov.c0,
                     "c1_ratio": cov.c1, "uncovered_total": len(cov.uncovered)},
        "anomaly_counts": dict(Counter(a.type for a in report.anomalies)),
        "anomalies_total": len(report.anomalies),
        "anomalies": [{**_when(a.t, report, time_map), "type": a.type, "id": a.probe_id, "function": a.function,
                       "detail": a.detail} for a in report.anomalies[:anomalies]],
        "bucket_s": report.bucket_ticks / hz, "buckets_total": len(report.buckets),
        "buckets": [{**_when(b.t0, report, time_map), "hits": b.hits, "calls": b.calls, "anomalies": b.anomalies}
                    for b in (report.buckets[-buckets:] if buckets else [])],
        "variables": [{"id": v.probe_id, "variable": v.variable, "function": v.function, "count": v.count,
                       "last": v.last, "min": v.min, "max": v.max} for v in report.variables.values()],
        "open": [{"function": f, **_when(t, report, time_map)} for f, t in report.open_frames],
        "tree": _tree(report.tree, hz, tree_depth),
    }
    if time_map is not None:
        out["time_map"] = time_map.to_dict()
    return out


def calls_dict(calls: list[Call], dropped: int, report: Report, time_map: TimeMap | None, *,
               t0_s: float | None = None, t1_s: float | None = None, function: str | None = None,
               limit: int = 200) -> dict:
    """呼び出しを始まりの順に（関数の遷移の時間軸）。t0_s ≤ 始まり < t1_s（解析の最初の印からの秒）・関数で絞り、
    先頭 limit 件と、深さで字下げした文字列（text）。"""
    hz, origin = report.hz, _origin(report)
    lo = None if t0_s is None else origin + t0_s * hz
    hi = None if t1_s is None else origin + t1_s * hz
    chosen = sorted((c for c in calls if (lo is None or c.t0 >= lo) and (hi is None or c.t0 < hi)
                     and (function is None or c.function == function)), key=lambda c: (c.t0, c.depth))
    shown = chosen[:limit]
    return {"total": len(chosen), "truncated": len(chosen) > limit, "dropped": dropped,
            "calls": [{"function": c.function, **_when(c.t0, report, time_map), "duration_us": c.ticks / hz * 1e6,
                       "self_us": c.self_ticks / hz * 1e6, "depth": c.depth, "complete": c.complete,
                       "entry_id": c.entry_id, "exit_id": c.exit_id} for c in shown],
            "text": render_calls(shown, hz, limit=None, origin=origin)}


def coverage_dict(coverage: Coverage, *, file: str | None = None, function: str | None = None,
                  limit: int = 500) -> dict:
    """カバレッジ（関数・C0・C1）と、通らなかった印（ファイル・行。AI が試験を足す手がかり）・分岐の組ごとの結果。
    file / function で絞る（割合は全体のまま）。"""
    def chosen(where: str, name: str) -> bool:
        return (file is None or where.rsplit(":", 1)[0] == file) and (function is None or name == function)
    d = coverage.to_dict()
    uncovered = [u for u in d["uncovered"] if chosen(u["where"], u["function"])]
    decisions = [x for x in d["decisions"] if chosen(x["where"], x["function"])]
    return {"functions": d["functions"], "c0": d["c0"], "c1": d["c1"], "function_ratio": d["function_ratio"],
            "c0_ratio": d["c0_ratio"], "c1_ratio": d["c1_ratio"], "uncovered_total": len(uncovered),
            "uncovered": uncovered[:limit], "decisions_total": len(decisions), "decisions": decisions[:limit]}
