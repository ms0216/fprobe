"""結果の直前に多い出来事（原因の判定はしない）: 記録の変数（MCU のメモリ・FPGA の信号・ピン）の条件で、結果の直前に候補が来る割合と偶然との比。

変数は名前で指す: MCU のメモリ = 記録の情報 sample.names の名前か番地（0x…）、FPGA の信号 = fpga.signals の名前、
ピン = 記録の情報 pins（既定の名前）・channel_names の名前か ch<N>。同じ名前が複数の種類にあれば mcu. / fpga. / pin. で区別する。
条件 = 項を and でつないだもの。項: <変数> changed / rose / fell（瞬間）・<変数>=<値>（その時の値）。
仕様: docs/superpowers/specs/2026-10-21-system-analysis-design.md §3
"""

import math
import re
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any

from .fpga_signals import FpgaSignals, Signal

try:                                                      # あれば多い瞬間（ピンのクロック等）の照合を速く（fast の依存）
    import numpy as _np
except ImportError:                                       # pragma: no cover - numpy の無い環境
    _np = None

__all__ = ["Condition", "ConditionError", "ConditionWatcher", "Term", "correlate", "durations", "parse_condition"]

EDGES = ("changed", "rose", "fell")
KINDS = ("mcu", "fpga", "pin")
EXAMPLES = 5
AUTO_LIMIT = 20                       # 候補を自動で作ったときに返す数


class ConditionError(ValueError):
    """条件が読めない・変数が無い・名前が曖昧。"""


@dataclass(frozen=True)
class Term:
    name: str
    op: str                      # changed / rose / fell / =
    value: str | None = None


@dataclass(frozen=True)
class Condition:
    text: str
    terms: tuple[Term, ...]


def parse_condition(text: str, need_edge: bool = True) -> Condition:
    """「restart changed and state=BUSY」→ 項の並び。瞬間の項（changed / rose / fell）が 1 つも無ければ誤り。
    空白・= を含む名前は "…" で囲む（"Error count" changed）。ponytail: 名前の中の " and " は区切りとみなす。"""
    parts = [p.strip() for p in re.split(r"\s+(?:and|かつ)\s+|\s*&&\s*", text.strip(), flags=re.I) if p.strip()]
    terms = []
    for p in parts:
        m = re.fullmatch(r'(?:"([^"]+)"|(\S+))\s+(changed|rose|fell|が変化)', p)
        if m:
            terms.append(Term(m[1] or m[2], "changed" if m[3] == "が変化" else m[3]))
            continue
        m = re.fullmatch(r'(?:"([^"]+)"|([^\s="]+))\s*=\s*(?:"([^"]+)"|(\S+))', p)
        if not m:
            raise ConditionError(f"項 {p!r} が読めない（<変数> changed / rose / fell か <変数>=<値>。空白・= を含む名前は \"…\" で囲む）")
        terms.append(Term(m[1] or m[2], "=", m[3] or m[4]))
    if need_edge and not any(t.op in EDGES for t in terms):
        raise ConditionError(f"条件 {text!r} に瞬間の項（changed / rose / fell）が無い")
    return Condition(text.strip(), tuple(terms))


@dataclass
class _Series:
    """変数の値の並び（時刻順。変わった所と最初の値だけ）。"""
    times: list[int]
    values: list[int]
    period: float | None = None          # 元の読みの間隔（タイムベース。MCU のメモリの読み・ピンのサンプル）
    enum: dict[str, int] | None = None
    gaps: list[dict] = field(default_factory=list)   # 値の無い区間・読めなかった語（_gaps が名前を付けて返す）
    reads: dict | None = None            # MCU のメモリの読みの事実（回数・間隔・いちばん長い空白）

    def at(self, t: int) -> int | None:
        i = bisect_right(self.times, t) - 1
        return self.values[i] if i >= 0 else None

    def instants(self, op: str) -> list[int]:
        memo = self.__dict__.setdefault("_instants", {})
        if op not in memo:
            memo[op] = self._instants_of(op)
        return memo[op]

    def arrays(self) -> tuple:
        """numpy の (時刻, 値)。一度だけ作って使い回す（候補ごとに作り直さない）。"""
        memo = self.__dict__.setdefault("_arrays", None)
        if memo is None:
            memo = self.__dict__["_arrays"] = (_np.asarray(self.times, dtype=_np.int64),
                                               _np.asarray(self.values, dtype=_np.int64))
        return memo

    def _instants_of(self, op: str) -> list[int]:
        times, values = self.times, self.values
        if op == "changed":                               # 並びは変わった所だけなので、最初の値の後は全部
            return times[1:]
        if op == "rose":
            return [t for t, a, b in zip(times[1:], values, values[1:]) if not a and b]
        return [t for t, a, b in zip(times[1:], values, values[1:]) if a and not b]


class _Variables:
    """記録の変数を名前で引き、使うものだけ値の並びにする。"""

    def __init__(self, rec: Any, t0: int | None, t1: int | None, seeds: dict | None = None,
                 gap_s: float | None = None):
        self.rec, self.t0, self.t1 = rec, t0, t1
        self.gap_s = gap_s                    # 使う側が明示したときだけ、これより長い読みの空白を欠けにする
        self.seeds = seeds or {}              # 変数の正体 → 範囲の前の最後の (時刻, 値)（少しずつ調べるとき）
        meta = rec.meta
        self.mcu = {name: int(a, 16) for a, name in (meta.get("sample.names") or {}).items()}
        self.fpga: dict[str, tuple[int, Signal]] = {}
        signals = FpgaSignals.from_dict(meta.get("fpga.signals"))
        for g, sigs in signals.groups.items():
            for s in sigs:
                self.fpga[s.name] = (g, s)
        self.pins = {str(n): ch for names in (meta.get("pins"), meta.get("channel_names"))
                     for ch, n in enumerate(names or [])}
        self._cache: dict[tuple[str, Any], _Series] = {}
        self._groups: dict[int, list[tuple[int, int]]] = {}
        self.hidden = 0                       # 範囲の中の標本型の見えない変化の数
        self.fpga_lost: list[dict] = []       # 範囲の中の FPGA の観測の欠落（stream fpga の gap。全部の組で共通）

    def resolve(self, name: str) -> tuple[str, Any, str]:
        """名前 → (種類, 鍵, 表示の名前)。"""
        kind, _, bare = name.partition(".") if name.split(".", 1)[0] in KINDS else ("", "", name)
        found = []
        if kind in ("", "mcu"):
            if bare in self.mcu:
                found.append(("mcu", self.mcu[bare]))
            elif re.fullmatch(r"0x[0-9a-fA-F]+", bare):
                found.append(("mcu", int(bare, 16)))
        if kind in ("", "fpga") and bare in self.fpga:
            found.append(("fpga", self.fpga[bare]))
        if kind in ("", "pin"):
            if bare in self.pins:
                found.append(("pin", self.pins[bare]))
            elif re.fullmatch(r"ch\d+", bare):
                found.append(("pin", int(bare[2:])))
        if not found:
            raise ConditionError(f"変数 {name!r} が記録に無い（MCU のメモリの名前・番地、FPGA の信号、ピンの名前・ch<N>）")
        if len(found) > 1:
            raise ConditionError(f"{name!r} は複数ある: " + " / ".join(f"{k}.{bare}" for k, _ in found) + " で区別する")
        return found[0][0], found[0][1], name

    def ident(self, name: str) -> tuple:
        """変数の正体（種類と番地・信号の名前・ch。同じ変数を別の書き方で指しても同じ）。"""
        kind, key, _ = self.resolve(name)
        return kind, key[1].name if kind == "fpga" else key

    def series(self, name: str) -> _Series:
        kind, key, _ = self.resolve(name)
        cached = self._cache.get((kind, key if kind != "fpga" else key[1].name))
        if cached is not None:
            return cached
        s = {"mcu": self._mcu, "fpga": self._fpga, "pin": self._pin}[kind](key)
        seed = self.seeds.get((kind, key[1].name if kind == "fpga" else key))
        if seed is not None and (not s.times or s.times[0] > seed[0]) and (not s.values or s.values[0] != seed[1]):
            s.times.insert(0, seed[0])                             # 前の範囲の最後の値: 範囲の最初の変化を落とさない
            s.values.insert(0, seed[1])
        self._cache[(kind, key if kind != "fpga" else key[1].name)] = s
        return s

    def _mcu(self, address: int) -> _Series:
        times, values, raw, failed = [], [], [], []
        for e in self.rec.iter_events(self.t0, self.t1, ["sample"]):
            if e.a != address:
                continue
            if isinstance(e.body, dict) and e.body.get("ack", 1) != 1:
                failed.append(e.t)
                continue
            raw.append(e.t)
            if not values or values[-1] != e.b:
                times.append(e.t)
                values.append(e.b)
        intervals = [b - a for a, b in zip(raw, raw[1:])]
        period = statistics.median(intervals) if intervals else None
        gaps = []
        if self.gap_s is not None:                   # 使う側が明示した gap_s より長い空白（範囲の端から最初・最後の読みまでも）
            hz = self.rec.meta["timebase_hz"]
            edges = [self.t0, *raw, self.t1] if self.t0 is not None and self.t1 is not None else raw
            gaps = [{"reason": "reads_missing", "t0": a, "t1": b} for a, b in zip(edges, edges[1:])
                    if b - a > self.gap_s * hz]
            s = _Series(times, values)
            for g in gaps:                           # 前後の値: 欠けの間に動いた量（回数は分からない）
                g["before"], g["after"] = s.at(g["t0"]), s.at(g["t1"])
        if failed:
            gaps.append({"reason": "read_failed", "t0": failed[0], "t1": failed[-1], "count": len(failed)})
        longest = max(zip(raw, raw[1:]), key=lambda ab: ab[1] - ab[0]) if intervals else None
        reads = {"count": len(raw), "failed": len(failed), "intervals": intervals,
                 "longest": list(longest) if longest else None}
        return _Series(times, values, period, gaps=gaps, reads=reads)

    def _group(self, group: int) -> list[tuple[int, int]]:
        """組の値の記録 (時刻, 組の値)。組ごとに 1 回だけ読む（信号ごとに読み直さない）。"""
        if group not in self._groups:
            rows, hidden, lost = [], 0, []
            for e in self.rec.iter_events(self.t0, self.t1, ["fpga"]):
                if e.kind == "hidden":
                    hidden += 1
                elif e.kind == "gap":
                    lost.append({"reason": "fpga_lost", "t0": e.t, "t1": e.t, "count": e.a})
                elif e.kind.endswith("_untimed"):              # 時刻の決まらない記録（t は受けた時 = 上限）: 欠けとして
                    lost.append({"reason": "fpga_untimed", "t0": e.t, "t1": e.t, "upper_bound": True,
                                 "count": e.a if e.kind == "gap_untimed" else 0, "kind": e.kind})
                elif e.kind == "value" and e.a == group:
                    rows.append((e.t, e.b))
            self._groups[group] = rows
            self.hidden = hidden
            self.fpga_lost = lost
        return self._groups[group]

    def _fpga(self, key: tuple[int, Signal]) -> _Series:
        group, sig = key
        times, values = [], []
        for t, raw in self._group(group):
            v = sig.extract(raw)
            if not values or values[-1] != v:
                times.append(t)
                values.append(v)
        return _Series(times, values, None, {name: v for v, name in sig.enum.items()})

    def _pin(self, channel: int) -> _Series:
        """かたまりごとの変化点の索引（Waveform。読み手が覚えておく = 時間軸の表示と共有）から、範囲の中の値と変化。"""
        times, values = [], []
        period = None
        lo, hi = self.t0, self.t1
        covered = []
        for chunk, present in self.rec.wave_chunks(lo, hi):
            if not present or channel >= chunk.channels:
                continue
            wave, tps = self.rec.waveform(chunk), chunk.ticks_per_sample
            covered.append((chunk.t0, chunk.t0 + round(chunk.length * tps)))
            period = tps
            s0 = max(0, math.floor((lo - chunk.t0) / tps))
            s1 = min(chunk.length, math.ceil((hi - chunk.t0) / tps))
            if s1 <= s0:
                continue
            level = wave.level_at(channel, s0)
            if not values or values[-1] != level:
                times.append(chunk.t0 + round(s0 * tps))
                values.append(level)
            edges = list(wave.edges_between(channel, s0, s1))
            if not edges:
                continue
            if _np is not None:                                # 数百万の変化（ピンのクロック等）もまとめて（丸めは同じ偶数丸め）
                times.extend((chunk.t0 + _np.rint(_np.asarray(edges, dtype=_np.float64) * tps)).astype(_np.int64).tolist())
            else:
                times.extend(chunk.t0 + round(s * tps) for s in edges)
            n = len(edges)
            values.extend(([level ^ 1, level] * ((n + 1) // 2))[:n])    # 変化のたびに反転
            level ^= n & 1
        gaps, at = [], lo                                      # 波形の無い区間（流す前・後・かたまりの間）
        for a, b in sorted(covered):
            if a - at > (period or 0):                         # 1 サンプルより短い隙間は端の丸め
                gaps.append({"reason": "pins_missing", "t0": at, "t1": a})
            at = max(at, b)
        if hi - at > (period or 0):
            gaps.append({"reason": "pins_missing", "t0": at, "t1": hi})
        return _Series(times, values, period, gaps=gaps)

def _target(series: _Series, text: str, name: str) -> int:
    if series.enum and text in series.enum:
        return series.enum[text]
    try:
        return int(text, 0)
    except ValueError:
        raise ConditionError(f"{name} の値 {text!r} が数でも列挙の名前でもない") from None


def _instants(cond: Condition, vars_: _Variables) -> list[int]:
    """条件が成り立った時刻（瞬間の項のどれかが起きた時刻で、ほかの項も満たすもの）。"""
    edges = [(t, vars_.series(t.name)) for t in cond.terms if t.op in EDGES]
    states = [(vars_.series(t.name), _target(vars_.series(t.name), t.value, t.name)) for t in cond.terms if t.op == "="]
    lists = sorted((s.instants(t.op) for t, s in edges), key=len)
    first, others = lists[0], [set(x) for x in lists[1:]]            # 瞬間の項が 1 つなら集合を作らない
    if others:
        first = [t for t in first if all(t in st for st in others)]
    return _holding(first, states) if states else first


_ARRAYS: dict[int, tuple[list[int], object]] = {}


def _array_of(values: list[int]):
    """瞬間の並び（_Series.instants の覚えたリスト）の numpy の配列。同じリストは一度だけ作る（直近の数個だけ覚える）。"""
    key = id(values)
    hit = _ARRAYS.get(key)
    if hit is not None and hit[0] is values:
        return hit[1]
    if len(_ARRAYS) > 8:
        _ARRAYS.clear()
    arr = _np.asarray(values, dtype=_np.int64)
    _ARRAYS[key] = (values, arr)
    return arr


def _holding(instants: list[int], states: list[tuple["_Series", int]]) -> list[int]:
    """時刻順の instants のうち、その時刻の値がどの (並び, 値) とも一致するもの（並びの最後の変化 ≤ t の値）。
    瞬間が数百万でも（ピンのクロック等）、1 つずつ二分探索しない: numpy があればまとめて、無ければ 1 回の走査で。"""
    if _np is not None:
        arr = _array_of(instants)
        keep = _np.ones(len(arr), dtype=bool)
        for series, value in states:
            st, vals = series.arrays()
            idx = _np.searchsorted(st, arr, side="right") - 1
            valid = idx >= 0
            keep &= valid & (vals[_np.where(valid, idx, 0)] == value)
        return arr[keep].tolist()
    out = []
    pos = [-1] * len(states)
    for t in instants:
        ok = True
        for k, (series, value) in enumerate(states):
            j, times = pos[k], series.times
            while j + 1 < len(times) and times[j + 1] <= t:
                j += 1
            pos[k] = j
            if j < 0 or series.values[j] != value:
                ok = False
        if ok:
            out.append(t)
    return out


def _range(rec: Any, t0: int | None, t1: int | None) -> tuple[int, int]:
    span = rec.span()
    if span is None:
        raise ConditionError("記録が空")
    return (span[0] if t0 is None else max(span[0], t0)), (span[1] + 1 if t1 is None else min(span[1] + 1, t1))


def correlate(rec: Any, effect: str, causes: list[str] | None, window_s: float | None = None, t0: int | None = None,
              t1: int | None = None, examples: int = EXAMPLES, limit: int = AUTO_LIMIT, gap_s: float | None = None,
              max_coverage: float | None = None) -> dict:
    """結果の条件 effect の各時刻について、窓 window_s の中で直前に起きた候補（causes の各条件）を探す。返すのは
    測った事実だけ（経験則・仮定による判定や順位づけをしない。CLAUDE.md）: 候補ごとに preceded（直前に候補があった
    結果の割合）・coverage（記録のうち、候補の後 window 以内に当たる時間の割合。先行率とどう比べるかは使う側が決める）・
    followed（後に結果が来た候補の割合）・遅れ（秒）・例（時刻。タイムベース）。並びは preceded の大きい順、同じなら
    条件の文字の順。causes が None なら記録の変数から候補を作る（_candidates。auto = True、一度も起きない候補は除き、
    上位 limit 個。max_coverage を明示すれば coverage がそれを超える候補を除く）。gap_s を明示すれば、それより長い
    MCU のメモリの読みの空白を欠け（gaps）にし、欠けにかかる結果を判定から外す。"""
    if window_s is None:
        raise ValueError("window_s（結果の直前を見る窓の秒）を渡す（既定の値は持たない）")
    if not window_s > 0:
        raise ValueError("window_s は正")
    lo, hi = _range(rec, t0, t1)
    hz = rec.meta["timebase_hz"]
    window = window_s * hz
    vars_ = _Variables(rec, lo, hi, gap_s=gap_s)
    eff_cond = parse_condition(effect)
    auto = causes is None
    if auto:
        causes = _candidates(vars_, eff_cond)
    cause_conds = list({c.text: c for c in map(parse_condition, causes)}.values())   # 同じ式は 1 つ（画面の一覧のキー）
    eff = _instants(eff_cond, vars_)
    # 結果の変数の欠けの直後の読みで見えた変化は、本当はいつ（何回）起きたか分からない: どの候補でも判定に入れない
    eff_holes = _holes(vars_, [t.name for t in eff_cond.terms])
    eff_known = [e for e in eff if not _overlaps(eff_holes, e - 1, e)]
    out_causes = []
    for cond in cause_conds:
        cs = _instants(cond, vars_)
        holes = _holes(vars_, [t.name for t in cond.terms])
        # 窓が候補の変数の欠けにかかる結果は、候補が先に来たか分からない: 分母に入れない
        judged = [e for e in eff_known if not _overlaps(holes, e - window, e)]
        delays, pairs = [], []
        for e in judged:
            i = bisect_left(cs, e) - 1                          # 結果より前（同じ時刻は前後が分からないので数えない）
            if i >= 0 and e - cs[i] <= window:
                delays.append((e - cs[i]) / hz)
                if len(pairs) < examples:
                    pairs.append({"effect_t": e, "cause_t": cs[i]})
        followed = _followed(cs, eff, window)
        preceded = len(delays) / len(judged) if judged else None
        out_causes.append({
            "condition": cond.text, "count": len(cs), "judged": len(judged), "preceded": preceded,
            "coverage": _coverage(cs, window, lo, hi, holes),
            "followed": followed / len(cs) if cs else None,
            "delay_s": _quantiles(delays), "examples": pairs, "_times": tuple(cs)})
    out_causes = _rank(out_causes)
    if auto:
        out_causes = [c for c in out_causes if c["count"]
                      and (max_coverage is None or c["coverage"] is None or c["coverage"] <= max_coverage)]
        out_causes = _merge_same(out_causes)[:limit]
    for c in out_causes:
        c.pop("_times", None)
    if auto:                                                # 返す候補だけの変数（sampling・no_values・warnings）
        cause_conds = [parse_condition(c["condition"]) for c in out_causes]
    names = {t.name for c in [eff_cond, *cause_conds] for t in c.terms}
    sampling = {n: (s.period / hz if s.period else None) for n in sorted(names) for s in [vars_.series(n)]}
    # 名前はあるが範囲の中に値が 1 件も無い変数（止まった watch・記録していないピン等）: 0 回を原因の無さと誤らせない
    no_values = sorted(n for n in names if not vars_.series(n).times)
    gaps = _gaps(rec, vars_, names, lo, hi)
    reads = {n: _reads_facts(vars_.series(n).reads, hz) for n in sorted(names) if vars_.series(n).reads}
    warnings = []
    if gaps:
        span, end = 0, lo                                  # 重なった欠けは 1 回だけ数える（gaps は t0 の順）
        for g in gaps:
            span += max(0, g["t1"] - max(g["t0"], end))
            end = max(end, g["t1"])
        span /= hz
        per = {}                                               # 変数ごとの欠けの長さ（使った変数で合計が変わるので）
        for g in gaps:
            if g.get("variable"):
                per[g["variable"]] = per.get(g["variable"], 0) + (g["t1"] - g["t0"]) / hz
        detail = "・".join(f"{n} {v * 1000:g} ms" for n, v in per.items())
        warnings.append(f"データに欠けが {len(gaps)} 件（重なりを除いた長さ {span * 1000:g} ms。{detail}{'。' if detail else ''}gaps）。読みの空白・ピンの無い"
                        "区間にかかる結果は judged から外した。ほかの欠け（FPGA の欠落・取り込みの失敗等）は時刻を示すだけ"
                        + ("" if gap_s is not None else "（MCU のメモリの読みの空白は gap_s を渡したときだけ欠けにする。"
                                                       "読みの間隔は reads）"))
    # MCU のメモリの結果は範囲の最初と最後の値の差も（変化の回数 count と違えば、1 回の読みで何回分も動いている）
    delta = {t.name: s.values[-1] - s.values[0] for t in eff_cond.terms
             if vars_.resolve(t.name)[0] == "mcu" and (s := vars_.series(t.name)).values}
    return {"effect": {"condition": eff_cond.text, "count": len(eff), "delta": delta}, "window_s": window_s, "range": [lo, hi],
            "timebase_hz": hz, "auto": auto, "no_values": no_values, "warnings": warnings,
            "causes": out_causes, "sampling": sampling, "reads": reads, "fpga_hidden": vars_.hidden, "gaps": gaps,
            "gap_s": gap_s}


def _reads_facts(reads: dict, hz: float) -> dict:
    """MCU のメモリの読みの事実: 回数・読めなかった回数・間隔（秒の分位）・いちばん長い空白（時刻）。"""
    return {"count": reads["count"], "failed": reads["failed"],
            "interval_s": _quantiles([i / hz for i in reads["intervals"]]), "longest": reads["longest"]}


def _followed(cs: list[int], eff: list[int], window: float) -> int:
    """後 window 以内（c < e <= c + window）に結果が来た候補の数。結果ごとの [e − window, e) に入る候補を二分探索で数える
    （候補が数百万回でも結果の数に比例する手間。重なった区間は 1 回だけ数える）。"""
    count, end = 0, None
    for e in eff:
        a = bisect_left(cs, e - window)
        if end is not None:
            a = max(a, end)
        b = bisect_left(cs, e)
        if b > a:
            count += b - a
        end = max(end or 0, b)
    return count


def _coverage(times: list[int], window: float, lo: int, hi: int, holes: list[tuple[int, int]]) -> float | None:
    """記録 [lo, hi) のうち候補の変数を見ていた時間（holes を除く）で、ある候補の後 (c, c + window] に当たる時間の割合。"""
    spans: list[tuple[float, float]] = []
    start = end = None
    if _np is not None and len(times) > 1:                # 数百万の瞬間でも: 窓の重なりのつなぎをまとめて
        c = _array_of(times).astype(_np.float64)
        a, b = _np.maximum(lo, c), _np.minimum(hi, c + window)
        keep = b > a
        a, b = a[keep], b[keep]
        if len(a):
            reach = _np.maximum.accumulate(b)             # それまでの窓の終わりの最大
            new = _np.empty(len(a), dtype=bool)
            new[0] = True
            new[1:] = a[1:] > reach[:-1]                  # 前の窓の終わりより後に始まる = 新しい区間
            first = _np.flatnonzero(new)
            last = _np.append(first[1:] - 1, len(a) - 1)
            spans = list(zip(a[first].tolist(), reach[last].tolist()))
        times = []
    for c in times:                                       # 時刻順: 窓の重なりをつないで区間にする
        a, b = max(lo, c), min(hi, c + window)
        if b <= a:
            continue
        if end is not None and a <= end:
            end = max(end, b)
        else:
            if end is not None:
                spans.append((start, end))
            start, end = a, b
    if end is not None:
        spans.append((start, end))

    def minus_holes(a: float, b: float) -> float:
        return (b - a) - sum(max(0.0, min(b, y) - max(a, x)) for x, y in holes)

    seen = minus_holes(lo, hi)
    return sum(minus_holes(a, b) for a, b in spans) / seen if seen > 0 else None


def _holes(vars_: "_Variables", names: list[str]) -> list[tuple[int, int]]:
    """変数の値が無い区間（読みの空白・ピンの無い区間）を重なりをまとめて時刻順に。"""
    spans = sorted((g["t0"], g["t1"]) for n in names for g in vars_.series(n).gaps if g["t1"] > g["t0"])
    out: list[tuple[int, int]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _overlaps(holes: list[tuple[int, int]], a: float, b: float) -> bool:
    i = bisect_left(holes, (b,)) - 1                          # b より前に始まる最後の欠け
    return i >= 0 and holes[i][1] > a


def _gaps(rec: Any, vars_: "_Variables", names: set[str], lo: int, hi: int) -> list[dict]:
    """使った変数の欠け（MCU の読みの空白・読めなかった語・ピンの無い区間・FPGA の欠落）と、記録の取り込みの失敗・
    長期の記録の間引き。時刻順。黙って数え落とさないため（reason は画面で訳す）。"""
    out, fpga = [], False
    for n in sorted(names):
        kind = vars_.resolve(n)[0]
        fpga |= kind == "fpga"
        out += [{**g, "variable": n} for g in vars_.series(n).gaps]
    if fpga:
        out += vars_.fpga_lost
    out += [{"reason": "events_error", "t0": e.t, "t1": e.t, "error": (e.body or {}).get("error")}
            for e in rec.iter_events(lo, hi, ["annotation"], kinds=["events_error"])]
    out += [{"reason": "recording_dropped", "t0": max(lo, g["t0"]), "t1": min(hi, g["t1"])}
            for g in rec.coverage(lo, hi)["gaps"]]
    return sorted(out, key=lambda g: (g["t0"], g["reason"]))


def _rank(causes: list[dict]) -> list[dict]:
    """先行率の大きい順、同じなら条件の文字の順（事実だけで。経験則の順位づけをしない）。先行率が無いものは後ろ。"""
    return sorted(causes, key=lambda c: (c["preceded"] is None, -(c["preceded"] or 0), c["condition"]))


def _merge_same(causes: list[dict]) -> list[dict]:
    """成立する時刻（_times）がすべて同じ候補を 1 つに: 項の多いもの（説明が多い。同じなら短いもの）を残し、ほかを
    same に（自動の候補の重複。例 "restart changed and state=BUSY" と "restart changed and addr=CTRL"・"restart rose"
    がこのデータでは同じ出来事）。順は保つ。"""
    groups: dict[tuple, list[dict]] = {}
    for c in causes:
        groups.setdefault(c["_times"], []).append(c)
    out = []
    for group in groups.values():
        keep = min(group, key=lambda c: (-len(parse_condition(c["condition"]).terms), len(c["condition"])))
        rest = [c["condition"] for c in group if c is not keep]
        item = {k: v for k, v in keep.items() if k != "_times"}
        if rest:
            item["same"] = rest
        out.append(item)
    return out


class ConditionWatcher:
    """記録中に少しずつ条件の時刻を探す（recording.trigger）。範囲ごとに scan し、前の範囲の最後の値を持ち越すので、
    一度に調べたのと同じ時刻が出る（範囲の境の変化も落とさない）。"""

    def __init__(self, condition: str):
        self.condition = parse_condition(condition)
        self._seeds: dict = {}

    def scan(self, rec: Any, t0: int, t1: int) -> list[int]:
        """[t0, t1) で条件が成り立った時刻。"""
        if t1 <= t0:
            return []
        vars_ = _Variables(rec, t0, t1, self._seeds)
        found = [t for t in _instants(self.condition, vars_) if t >= t0]
        for term in self.condition.terms:
            s = vars_.series(term.name)
            if s.times:
                self._seeds[vars_.ident(term.name)] = (s.times[-1], s.values[-1])
        return found


def _quote(name: str) -> str:
    return f'"{name}"' if re.search(r'[\s="]', name) else name


def _candidates(vars_: "_Variables", effect: Condition) -> list[str]:
    """記録の変数から候補の条件を作る: 変化（1 ビット・ピンは rose / fell）、列挙の信号の値に入った瞬間、
    「変化 かつ 列挙の信号 = 値」の組、ほかの MCU のメモリの変化。範囲の中で変化しない変数と、結果の変数は使わない
    （頻度による除外はしない。絞るのは使う側が max_coverage で明示する）。"""
    excluded = {vars_.ident(t.name) for t in effect.terms}
    named: list[tuple[str, str, _Series]] = []                     # (条件での名前, 種類, 値の並び)
    for kind, names in (("mcu", vars_.mcu), ("fpga", vars_.fpga), ("pin", vars_.pins)):
        for name in names:
            label = name if _unique(vars_, name) else f"{kind}.{name}"
            ident = vars_.ident(label)
            if ident in excluded:                                  # 結果の変数・同じ変数の別の名前（ピンの ch<N> 等）
                continue
            excluded.add(ident)
            series = vars_.series(label)
            if len(series.times) > 1:
                named.append((_quote(label), kind, series))
    edges, states = [], []
    for label, kind, series in named:
        if series.enum:
            states += [(label, name) for name, v in series.enum.items() if v in set(series.values)]
        binary = set(series.values) <= {0, 1}
        edges += [f"{label} rose", f"{label} fell"] if binary and kind != "mcu" else [f"{label} changed"]
    edge_labels = [label for label, _, _ in named]
    out = list(edges)
    out += [f"{label} changed and {label}={_quote(name)}" for label, name in states if label in edge_labels]
    for label in edge_labels:
        out += [f"{label} changed and {other}={_quote(name)}" for other, name in states if other != label]
    return [c for c in dict.fromkeys(out) if _parses(c)]


def _parses(condition: str) -> bool:
    """書けない名前（" を含む・" and " を含む等）の候補は作らない（自動の候補で全体を誤りにしない）。"""
    try:
        parse_condition(condition)
        return True
    except ConditionError:
        return False


def _unique(vars_: "_Variables", name: str) -> bool:
    try:
        vars_.resolve(name)
        return True
    except ConditionError:
        return False


def durations(rec: Any, name: str, t0: int | None = None, t1: int | None = None, limit: int = 20,
              gap_s: float | None = None) -> dict:
    """変数 name の値ごとの回数・続いた時間（両端の見えている区間だけ。秒の最小・中央値・90 % 点・最大）・範囲の中で
    その値だった時間の割合（share。最初の値を見てから範囲の終わりまでのうち）。値は列挙の名前か数の文字。続いた時間の
    合計の大きい順に limit 個。例: FPGA の state が BUSY のまま続く時間と、MCU の待ち時間を比べる。"""
    lo, hi = _range(rec, t0, t1)
    hz = rec.meta["timebase_hz"]
    vars_ = _Variables(rec, lo, hi, gap_s=gap_s)
    series = vars_.series(name)
    holes = _holes(vars_, [name])
    label = {v: k for k, v in (series.enum or {}).items()}
    spans: dict[int, list[float]] = {}
    held: dict[int, int] = {}
    excluded = 0
    ends = series.times[1:] + [hi]
    for i, (t, v, end) in enumerate(zip(series.times, series.values, ends)):
        hidden = _overlaps(holes, t - 1, end)                # 欠けにかかる・欠けの直後に見えた: いつ変わったか分からない
        held[v] = held.get(v, 0) + (0 if hidden else end - t)
        if 0 < i < len(series.times) - 1:                    # 両端の見えている区間
            if hidden:
                excluded += 1
            else:
                spans.setdefault(v, []).append((end - t) / hz)
    observed = sum(held.values())                            # 欠けにかからない時間（割合の分母）
    for v in (series.enum or {}).values():                   # 定義にあるが出なかった列挙の値も 0 で（出なかったと分かる）
        held.setdefault(v, 0)
    ranked = sorted(held, key=lambda v: -held[v])[:limit]
    return {"variable": name, "range": [lo, hi], "changes": max(0, len(series.times) - 1), "excluded": excluded,
            "gaps": _gaps(rec, vars_, {name}, lo, hi),
            "values": {label.get(v, str(v)): {"count": len(spans.get(v, [])), "share": held[v] / observed if observed else None,
                                              "duration_s": _quantiles(spans.get(v, []))} for v in ranked}}


def transactions(rec: Any, start: str, by: str | None = None, counts: list[str] | None = None,
                 effect: str | None = None, t0: int | None = None, t1: int | None = None, rows: int = 20,
                 by_at: str = "start", effect_lag_s: float = 0.0, gap_s: float | None = None) -> dict:
    """記録を区間（リクエスト等の 1 回の取引）に切って数える: 区間 = start（瞬間の項のある条件）が成り立ってから次に
    成り立つまで（最後は範囲の終わりまで）。区間ごとに by（変数）の開始時の値・counts の各条件が区間の中で成り立った
    回数・effect が区間の中で起きたか（by_at = "end" なら by は区間の終わり = 次の始めの直前の値。ファームウェアが要求の
    最後に書く値で分けるとき）。by の値ごとに区間の数・effect の割合・counts の平均（groups。by の値の順）、
    最初の rows 区間の中身（rows）。例: start "state changed and state=BUSY"、by "left_hi"、counts ["restart changed
    and state=BUSY"]、effect "errors changed" → B が大きい要求だけ再試行され、再試行された要求だけ誤る、が 1 回で分かる。"""
    if by_at not in ("start", "end"):
        raise ValueError("by_at は start（区間の始めの値）か end（区間の終わり = 次の始めの直前の値）")
    lo, hi = _range(rec, t0, t1)
    counts = list(counts or [])
    start_cond = parse_condition(start)
    conds = [parse_condition(c) for c in counts]
    effect_cond = parse_condition(effect) if effect else None
    vars_ = _Variables(rec, lo, hi, gap_s=gap_s)
    starts = [t for t in _instants(start_cond, vars_) if lo <= t < hi]
    if not starts:
        raise ConditionError(f"区間の始め {start!r} が範囲の中で一度も成り立たない")
    ends = starts[1:] + [hi]
    hits = [_instants(c, vars_) for c in conds]
    lag = round(effect_lag_s * rec.meta["timebase_hz"])      # 結果を lag だけ前の時刻で区間に割り当てる
    effects = [t - lag for t in _instants(effect_cond, vars_)] if effect_cond else []
    series = vars_.series(by) if by else None
    label = {v: k for k, v in ((series.enum or {}) if series else {}).items()}

    def inside(times: list[int], a: int, b: int) -> int:
        return bisect_left(times, b) - bisect_left(times, a)

    names = {t.name for c in [start_cond, *conds, *([effect_cond] if effect_cond else [])] for t in c.terms} | ({by} if by else set())
    holes = _holes(vars_, sorted(names))
    by_var = {n: _holes(vars_, [n]) for n in sorted(names)}
    excluded_by: dict[str, int] = {}
    out_rows, groups, excluded = [], {}, 0
    offsets, lengths = [], []                                # 結果の始めからの時間・区間の長さ（秒）
    split = {True: [0, [0] * len(conds)], False: [0, [0] * len(conds)]}   # 結果が起きた区間・起きなかった区間
    for a, b in zip(starts, ends):
        if _overlaps(holes, a - 1, b):                       # 欠けにかかる・欠けの直後に見えた始まり: 回数・結果が不確か
            excluded += 1
            for n, h in by_var.items():
                if _overlaps(h, a - 1, b):
                    excluded_by[n] = excluded_by.get(n, 0) + 1
            continue
        value = (series.at(a) if by_at == "start" else series.at(b - 1)) if series else None
        key = None if value is None else label.get(value, str(value))
        n = [inside(h, a, b) for h in hits]
        hit = inside(effects, a, b) > 0 if effect_cond else None
        lengths.append((b - a) / rec.meta["timebase_hz"])
        if hit:
            offsets.append((effects[bisect_left(effects, a)] - a) / rec.meta["timebase_hz"])
        if len(out_rows) < rows:
            out_rows.append({"t": a, "end": b, "by": key, "counts": n, "effect": hit})
        g = groups.setdefault(value, {"by": key, "count": 0, "effects": 0, "sums": [0] * len(n)})
        g["count"] += 1
        g["effects"] += bool(hit)
        g["sums"] = [x + y for x, y in zip(g["sums"], n)]
        if effect_cond:
            split[hit][0] += 1
            split[hit][1] = [x + y for x, y in zip(split[hit][1], n)]
    out_groups = []
    for value in sorted(groups, key=lambda v: (v is None, v if v is not None else 0)):
        g = groups[value]
        out_groups.append({"by": g["by"], "count": g["count"],
                           "effect_rate": g["effects"] / g["count"] if effect_cond else None,
                           "counts": {c: x / g["count"] for c, x in zip(counts, g["sums"])}})
    return {"start": start_cond.text, "by": by, "effect": effect_cond.text if effect_cond else None,
            "range": [lo, hi], "timebase_hz": rec.meta["timebase_hz"], "transactions": len(starts), "excluded": excluded,
            "excluded_by": excluded_by, "length_s": _quantiles(lengths),
            "effect_offset_s": _quantiles(offsets) if effect_cond else None, "effect_lag_s": effect_lag_s,
            "warnings": [f"データの欠けにかかる {excluded} 区間を除いた（"
                         + ", ".join(f"{n} {k}" for n, k in excluded_by.items()) + "。excluded_by・gaps）"] if excluded else [],
            "gaps": _gaps(rec, vars_, names, lo, hi),
            "groups": out_groups, "rows": out_rows,
            "by_effect": {k: {"count": c, "counts": {name: x / c if c else None for name, x in zip(counts, sums)}}
                          for k, (c, sums) in (("with", split[True]), ("without", split[False]))} if effect_cond else None}


def _quantiles(xs: list[float]) -> dict | None:
    if not xs:
        return None
    s = sorted(xs)
    return {"min": s[0], "median": statistics.median(s), "p90": s[min(len(s) - 1, int(0.9 * len(s)))], "max": s[-1]}
