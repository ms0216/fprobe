"""テストポイントの解析: Hit の列 → 関数の遷移（呼び出しの木と時間軸）・関数ごとの時間・2 点間の時間・
カバレッジ（関数・C0・C1）・長時間の要約・異常の印。

`Analyzer` は逐次（feed を何度でも）でメモリが一定（呼び出しの一覧・変数の値の列は keep_* のときだけ持つ）。
仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 5 節。

    an = Analyzer(ProbeTable.load("testpoints.json"), hz=100e6)
    an.feed(decoded.items)
    report = an.finish()
    print(render_calls(report.calls, report.hz))
"""

import math
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .probes import BLOCK_KINDS, Probe, ProbeTable
from .trace import Hit, Item, Loss

__all__ = ["Analyzer", "Anomaly", "Bucket", "Call", "CallTreeNode", "Coverage", "FunctionStats", "IntervalStats",
           "Report", "Stats", "VariableStats", "analyze", "intervals", "render_calls"]


class Stats:
    """回数・合計・最小・最大・平均・標準偏差（母集団、Welford）。値は時刻の単位（整数）。"""

    def __init__(self):
        self.count = 0
        self.total = 0
        self.min: int | None = None
        self.max: int | None = None
        self.max_at: int | None = None       # いちばん大きかった値の時刻（呼び出しの始め。分かるとき）
        self._mean = 0.0
        self._m2 = 0.0

    def add(self, x: int, at: int | None = None) -> None:
        self.count += 1
        self.total += x
        self.min = x if self.min is None else min(self.min, x)
        if self.max is None or x > self.max:
            self.max, self.max_at = x, at
        delta = x - self._mean
        self._mean += delta / self.count
        self._m2 += delta * (x - self._mean)

    @property
    def mean(self) -> float | None:
        return self._mean if self.count else None

    @property
    def stdev(self) -> float | None:
        return math.sqrt(self._m2 / self.count) if self.count else None

    def to_dict(self, hz: float) -> dict:
        """秒で。"""
        if not self.count:
            return {"count": 0}
        return {"count": self.count, "total_s": self.total / hz, "min_s": self.min / hz, "max_s": self.max / hz,
                "max_at": self.max_at, "mean_s": self.mean / hz, "stdev_s": self.stdev / hz}


@dataclass(frozen=True)
class Call:
    function: str
    t0: int
    t1: int
    depth: int                  # 0 = 一番外
    self_ticks: int             # 子を除いた時間
    entry_id: int
    exit_id: int | None         # 出口の印（None = 出口を見ずに閉じた）
    complete: bool = True       # False: 出口が無いまま閉じた（missing_exit・loss）

    @property
    def ticks(self) -> int:
        return self.t1 - self.t0


@dataclass(frozen=True)
class Anomaly:
    t: int
    type: str                   # long_call / missing_exit / exit_without_entry / unknown_id / silence / no_exit / loss
    probe_id: int | None
    function: str | None
    detail: str


@dataclass
class FunctionStats:
    name: str
    calls: int = 0                                  # 入口の回数
    inclusive: Stats = field(default_factory=Stats)  # 子を含む時間（出口まで見た呼び出し）
    exclusive: Stats = field(default_factory=Stats)  # 自分だけの時間
    total_inclusive: int = 0                        # 一番外側の呼び出しだけの合計（再帰を二重に数えない）

    def to_dict(self, hz: float) -> dict:
        return {"name": self.name, "calls": self.calls, "inclusive": self.inclusive.to_dict(hz),
                "exclusive": self.exclusive.to_dict(hz), "total_inclusive_s": self.total_inclusive / hz}


@dataclass
class CallTreeNode:
    """呼び出しの経路ごとの集計（呼び出しの文脈の木）。"""
    function: str
    count: int = 0
    inclusive: int = 0
    exclusive: int = 0
    children: dict[str, "CallTreeNode"] = field(default_factory=dict)

    def child(self, name: str) -> "CallTreeNode":
        node = self.children.get(name)
        if node is None:
            node = self.children[name] = CallTreeNode(name)
        return node

    def to_dict(self, hz: float) -> dict:
        return {"function": self.function, "count": self.count, "inclusive_s": self.inclusive / hz,
                "exclusive_s": self.exclusive / hz,
                "children": [c.to_dict(hz) for c in sorted(self.children.values(), key=lambda n: -n.inclusive)]}


@dataclass
class Bucket:
    index: int
    t0: int
    hits: int = 0
    calls: int = 0
    anomalies: int = 0
    functions: Counter = field(default_factory=Counter)     # 関数ごとの入口の回数


@dataclass
class VariableStats:
    probe_id: int
    variable: str
    function: str
    count: int = 0
    last: int | None = None
    min: int | None = None
    max: int | None = None
    values: list[tuple[int, int]] = field(default_factory=list)   # (時刻, 値)（keep_values のとき）


@dataclass
class Coverage:
    functions_total: int
    functions_hit: int
    c0_total: int
    c0_hit: int
    c1_total: int
    c1_hit: int
    decisions: list[dict]
    uncovered: list[Probe]

    @staticmethod
    def _ratio(hit: int, total: int) -> float | None:
        return hit / total if total else None

    @property
    def function_ratio(self) -> float | None:
        return self._ratio(self.functions_hit, self.functions_total)

    @property
    def c0(self) -> float | None:
        return self._ratio(self.c0_hit, self.c0_total)

    @property
    def c1(self) -> float | None:
        return self._ratio(self.c1_hit, self.c1_total)

    def to_dict(self) -> dict:
        return {"functions": [self.functions_hit, self.functions_total], "c0": [self.c0_hit, self.c0_total],
                "c1": [self.c1_hit, self.c1_total], "function_ratio": self.function_ratio, "c0_ratio": self.c0,
                "c1_ratio": self.c1, "decisions": self.decisions,
                "uncovered": [{"id": p.id, "where": p.where, "function": p.function, "kind": p.kind, "label": p.label,
                               "implicit": p.implicit} for p in self.uncovered]}


@dataclass
class Report:
    hz: float
    hits: int
    losses: int
    t_first: int | None
    t_last: int | None
    functions: dict[str, FunctionStats]
    tree: CallTreeNode
    calls: list[Call] | None
    anomalies: list[Anomaly]
    buckets: list[Bucket]
    bucket_ticks: int
    coverage: Coverage
    variables: dict[int, VariableStats]
    open_frames: list[tuple[str, int]]              # 終わりの時点で出口を見ていない呼び出し (関数, 入口の時刻)
    hit_counts: Counter

    def to_dict(self, top: int | None = 20) -> dict:
        """JSON にできる要約（MCP・AI 向け）。関数は子を含む時間の合計の多い順に top 個。"""
        funcs = sorted(self.functions.values(), key=lambda f: -f.total_inclusive)
        return {
            "hz": self.hz, "hits": self.hits, "losses": self.losses,
            "span_s": None if self.t_first is None else (self.t_last - self.t_first) / self.hz,
            "functions": [f.to_dict(self.hz) for f in (funcs if top is None else funcs[:top])],
            "coverage": self.coverage.to_dict(),
            "anomalies": [{"t_s": a.t / self.hz, "type": a.type, "id": a.probe_id, "function": a.function,
                           "detail": a.detail} for a in self.anomalies],
            "buckets": [{"t_s": b.t0 / self.hz, "hits": b.hits, "calls": b.calls, "anomalies": b.anomalies}
                        for b in self.buckets],
            "variables": [{"id": v.probe_id, "variable": v.variable, "function": v.function, "count": v.count,
                           "last": v.last, "min": v.min, "max": v.max} for v in self.variables.values()],
            "open": [{"function": f, "t0_s": t / self.hz} for f, t in self.open_frames],
            "tree": self.tree.to_dict(self.hz),
        }


class _Frame:
    __slots__ = ("name", "entry_id", "t0", "child", "node")

    def __init__(self, name: str, entry_id: int, t0: int, node: CallTreeNode):
        self.name, self.entry_id, self.t0, self.child, self.node = name, entry_id, t0, 0, node


class Analyzer:
    """Hit / Loss を逐次に受けて解析する。

    long_call: 使う側が long_s（秒）を明示したときだけ、それより長い呼び出し（経験則の「いつもより長い」は付けない。
    いちばん長い呼び出しの長さと時刻は統計の max_s・max_at で事実として返す）。
    silence_s: これより長く何も来なければ silence。open_s: 終わりの時点でこれより長く開いた入口は no_exit。
    on_call / on_hit / on_anomaly: 流しながら記録に書くためのコールバック（record.RecorderSink）。
    """

    def __init__(self, table: ProbeTable, hz: float, *, bucket_s: float = 1.0, keep_calls: bool = True,
                 keep_values: bool = True, long_s: float | None = None,
                 silence_s: float | None = None, open_s: float | None = None, max_depth: int = 64,
                 on_call: Callable[[Call], None] | None = None,
                 on_hit: Callable[[Hit, Probe, str], None] | None = None,
                 on_anomaly: Callable[[Anomaly], None] | None = None):
        self.table, self.hz = table, hz
        self.names = table.function_names()
        self.bucket_ticks = max(1, round(bucket_s * hz))
        self.keep_calls, self.keep_values = keep_calls, keep_values
        self.long_ticks = None if long_s is None else long_s * hz
        self.silence_ticks = None if silence_s is None else silence_s * hz
        self.open_ticks = None if open_s is None else open_s * hz
        self.max_depth = max_depth
        self.on_call, self.on_hit, self.on_anomaly = on_call, on_hit, on_anomaly
        self.stack: list[_Frame] = []
        self.root = CallTreeNode("")
        self.functions: dict[str, FunctionStats] = {}
        self.active: Counter = Counter()
        self.calls: list[Call] = []
        self.anomalies: list[Anomaly] = []
        self.buckets: dict[int, Bucket] = {}
        self.variables: dict[int, VariableStats] = {}
        self.hit_counts: Counter = Counter()
        self.hits = self.losses = 0
        self.t_first: int | None = None
        self.t_last: int | None = None

    # --- 入口 ---

    def feed(self, items: Iterable[Item]) -> None:
        for item in items:
            if isinstance(item, Hit):
                self._hit(item)
            elif isinstance(item, Loss):
                self._loss(item)
            else:
                raise TypeError(item)

    def _name(self, probe: Probe) -> str:
        return self.names[(probe.file, probe.function)]

    def _bucket(self, t: int) -> Bucket:
        index = (t - self.t_first) // self.bucket_ticks
        b = self.buckets.get(index)
        if b is None:
            b = self.buckets[index] = Bucket(index, self.t_first + index * self.bucket_ticks)
        return b

    def _time(self, t: int) -> None:
        if self.t_first is None:
            self.t_first = t
        if self.silence_ticks is not None and self.t_last is not None and t - self.t_last > self.silence_ticks:
            self._anomaly(self.t_last, "silence", None, None, f"{(t - self.t_last) / self.hz:.6g} 秒 何も来ない")
        self.t_last = t if self.t_last is None else max(self.t_last, t)

    def _anomaly(self, t: int, kind: str, probe_id: int | None, function: str | None, detail: str) -> None:
        a = Anomaly(t, kind, probe_id, function, detail)
        self.anomalies.append(a)
        if self.t_first is not None:
            self._bucket(t).anomalies += 1
        if self.on_anomaly:
            self.on_anomaly(a)

    def _hit(self, h: Hit) -> None:
        self._time(h.t)
        self.hits += 1
        self._bucket(h.t).hits += 1
        probe = self.table.get(h.id)
        if probe is None:
            self._anomaly(h.t, "unknown_id", h.id, None, f"対応表に無い ID {h.id}")
            return
        self.hit_counts[h.id] += 1
        name = self._name(probe)
        if probe.kind == "entry":
            self._enter(h, name)
        elif probe.kind == "exit":
            self._exit(h, name)
        else:
            if probe.kind == "var" and h.value is not None:
                self._variable(h, probe, name)
            if self.on_hit:
                self.on_hit(h, probe, name)

    def _loss(self, loss: Loss) -> None:
        self._time(loss.t)
        self.losses += 1
        self._anomaly(loss.t, "loss", None, None, loss.reason)
        while self.stack:                     # 取りこぼしの後は呼び出しの状態が分からない: 開いた枠を閉じる
            self._close(self.stack.pop(), loss.t, None, complete=False)

    # --- 呼び出し ---

    def _enter(self, h: Hit, name: str) -> None:
        parent = self.stack[-1].node if self.stack else self.root
        node = parent.child(name) if len(self.stack) < self.max_depth else parent
        self.stack.append(_Frame(name, h.id, h.t, node))
        self.active[name] += 1
        stats = self.functions.get(name)
        if stats is None:
            stats = self.functions[name] = FunctionStats(name)
        stats.calls += 1
        b = self._bucket(h.t)
        b.calls += 1
        b.functions[name] += 1

    def _exit(self, h: Hit, name: str) -> None:
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k].name == name:
                break
        else:
            self._anomaly(h.t, "exit_without_entry", h.id, name, "入口を見ていない出口")
            return
        while len(self.stack) > k + 1:
            frame = self.stack.pop()
            self._anomaly(h.t, "missing_exit", frame.entry_id, frame.name,
                          f"{name} の出口の時点で {frame.name} の出口が無い")
            self._close(frame, h.t, None, complete=False)
        self._close(self.stack.pop(), h.t, h.id, complete=True)

    def _close(self, frame: _Frame, t1: int, exit_id: int | None, *, complete: bool) -> None:
        ticks = t1 - frame.t0
        own = ticks - frame.child
        if self.stack:
            self.stack[-1].child += ticks
        stats = self.functions[frame.name]
        if complete:
            inc = stats.inclusive
            if self.long_ticks is not None and ticks > self.long_ticks:
                self._anomaly(frame.t0, "long_call", frame.entry_id, frame.name,
                              f"{ticks / self.hz:.6g} 秒（指定 {self.long_ticks / self.hz:.6g} 秒より長い）")
            inc.add(ticks, frame.t0)
            stats.exclusive.add(own)
        if self.active[frame.name] == 1:
            stats.total_inclusive += ticks
        self.active[frame.name] -= 1
        frame.node.count += 1
        frame.node.inclusive += ticks
        frame.node.exclusive += own
        call = Call(frame.name, frame.t0, t1, len(self.stack), own, frame.entry_id, exit_id, complete)
        if self.keep_calls:
            self.calls.append(call)
        if self.on_call:
            self.on_call(call)

    def _variable(self, h: Hit, probe: Probe, name: str) -> None:
        v = self.variables.get(h.id)
        if v is None:
            v = self.variables[h.id] = VariableStats(h.id, probe.variable or "", name)
        v.count += 1
        v.last = h.value
        v.min = h.value if v.min is None else min(v.min, h.value)
        v.max = h.value if v.max is None else max(v.max, h.value)
        if self.keep_values:
            v.values.append((h.t, h.value))

    # --- 終わり ---

    def coverage(self) -> Coverage:
        probes = self.table.probes.values()
        hit = self.hit_counts
        entries: dict[str, bool] = {}
        for p in probes:
            if p.kind == "entry":
                entries[self._name(p)] = entries.get(self._name(p), False) or hit[p.id] > 0
        c0 = [p for p in probes if p.kind in BLOCK_KINDS and not p.implicit]
        decisions = []
        c1_total = c1_hit = 0
        for first, group in self.table.decisions().items():
            outcomes = [{"id": p.id, "label": p.label, "kind": p.kind, "implicit": p.implicit, "count": hit[p.id]}
                        for p in group]
            covered = sum(1 for o in outcomes if o["count"])
            c1_total += len(outcomes)
            c1_hit += covered
            decisions.append({"id": first, "where": group[0].where, "function": self._name(group[0]),
                              "outcomes": outcomes, "covered": covered})
        uncovered = sorted((p for p in probes
                            if hit[p.id] == 0 and ((p.kind in BLOCK_KINDS and not p.implicit) or
                                                   (p.decision is not None))),
                           key=lambda p: (p.file, p.line, p.id))
        return Coverage(len(entries), sum(entries.values()), len(c0), sum(1 for p in c0 if hit[p.id]),
                        c1_total, c1_hit, decisions, uncovered)

    def finish(self, t_end: int | None = None, *, final: bool = True) -> Report:
        """終わりの時点の要約。開いたままの呼び出しは閉じない（open_frames。open_s を超えたら no_exit）。
        final = False は取っている最中の途中の要約: no_exit を付けない（状態を変えないので何度呼んでもよい）。"""
        t_end = self.t_last if t_end is None else t_end
        open_frames = [(f.name, f.t0) for f in self.stack]
        if final and self.open_ticks is not None and t_end is not None:
            for f in self.stack:
                if t_end - f.t0 > self.open_ticks:
                    self._anomaly(f.t0, "no_exit", f.entry_id, f.name,
                                  f"{(t_end - f.t0) / self.hz:.6g} 秒 出口が無い")
        return Report(self.hz, self.hits, self.losses, self.t_first, self.t_last, self.functions, self.root,
                      self.calls if self.keep_calls else None, list(self.anomalies),
                      [self.buckets[k] for k in sorted(self.buckets)], self.bucket_ticks, self.coverage(),
                      self.variables, open_frames, Counter(self.hit_counts))


def analyze(items: Iterable[Item], table: ProbeTable, hz: float, **options) -> Report:
    an = Analyzer(table, hz, **options)
    an.feed(items)
    return an.finish()


# --- 2 点間の時間 -----------------------------------------------------------------------------

@dataclass
class IntervalStats:
    start_id: int
    end_id: int
    hz: float
    pairs: list[tuple[int, int]]          # (開始の時刻, 時間)
    stats: Stats

    def to_dict(self) -> dict:
        return {"start": self.start_id, "end": self.end_id, **self.stats.to_dict(self.hz),
                "pairs": [(t / self.hz, d / self.hz) for t, d in self.pairs]}


def intervals(items: Iterable[Item], start_id: int, end_id: int, hz: float) -> IntervalStats:
    """start_id の後で最初の end_id までの時間。終わりの前に次の開始が来たら、その開始からやり直す。
    Loss の後は開始を捨てる（間に何があったか分からない）。"""
    pairs: list[tuple[int, int]] = []
    stats = Stats()
    started: int | None = None
    for item in items:
        if isinstance(item, Loss):
            started = None
        elif item.id == end_id and started is not None:
            pairs.append((started, item.t - started))
            stats.add(item.t - started)
            started = None
        elif item.id == start_id:
            started = item.t
    return IntervalStats(start_id, end_id, hz, pairs, stats)


# --- 時間軸の文字列 ---------------------------------------------------------------------------

def render_calls(calls: Iterable[Call], hz: float, *, limit: int | None = 200, origin: int | None = None) -> str:
    """呼び出しを始まりの順に、深さで字下げして 1 行ずつ（関数の遷移の時間軸）。時間は µs。"""
    ordered = sorted(calls, key=lambda c: (c.t0, c.depth))
    if not ordered:
        return ""
    origin = ordered[0].t0 if origin is None else origin
    lines = []
    for c in ordered if limit is None else ordered[:limit]:
        mark = "" if c.complete else "  [出口なし]"
        lines.append(f"{(c.t0 - origin) / hz * 1e6:12.3f} us  {'  ' * c.depth}{c.function}  "
                     f"{c.ticks / hz * 1e6:.3f} us（自分 {c.self_ticks / hz * 1e6:.3f}）{mark}")
    if limit is not None and len(ordered) > limit:
        lines.append(f"…（ほか {len(ordered) - limit} 件）")
    return "\n".join(lines)
