"""結果の直前に多い出来事（原因の判定はしない）: 記録の変数（MCU のメモリ・FPGA の信号・ピン）の条件で、結果の直前に候補が来る割合と偶然との比。

変数は名前で指す: MCU のメモリ = 記録の情報 sample.names の名前か番地（0x…）、FPGA の信号 = fpga.signals の名前、
ピン = 記録の情報 pins（既定の名前）・channel_names の名前か ch<N>。同じ名前が複数の種類にあれば mcu. / fpga. / pin. で区別する。
条件 = 項を and でつないだもの。項: <変数> changed / rose / fell（瞬間）・<変数>=<値>（その時の値）。
仕様: docs/superpowers/specs/2026-10-21-system-analysis-design.md §3
"""

import re

from bisect import bisect_left
from typing import Any

from .correlate_cond import EDGES, Condition, ConditionError, Term, parse_condition, term_dict  # noqa: F401（EDGES は再公開）
from .correlate_durations import _quantiles, durations, transactions  # noqa: F401（durations・transactions は再公開）
from .correlate_support import UNSURE_REASONS, _array_of, _gaps, _holding, _holes, _instants, _overlaps, _range  # noqa: F401
from .correlate_vars import _Series, _Variables, _target  # noqa: F401（試験・利用者向けの再公開）
from .la_wave import _np

__all__ = ["Condition", "ConditionError", "ConditionWatcher", "Term", "correlate", "durations", "parse_condition",
           "term_dict"]

EXAMPLES = 5
AUTO_LIMIT = 20                       # 候補を自動で作ったときに返す数


def correlate(rec: Any, effect: str, causes: list[str] | None, window_s: float | None = None, t0: int | None = None,
              t1: int | None = None, examples: int = EXAMPLES, limit: int = AUTO_LIMIT, gap_s: float | None = None,
              max_coverage: float | None = None) -> dict:
    """結果の条件 effect の各時刻について、窓 window_s の中で直前に起きた候補（causes の各条件）を探す。返すのは
    測った事実だけ（経験則・仮定による判定や順位づけをしない。CLAUDE.md）: 候補ごとに preceded（直前に候補があった
    結果の割合）・coverage（記録のうち、候補の後 window 以内に当たる時間の割合。先行率とどう比べるかは使う側が決める）・
    followed（後に結果が来た候補の割合）・遅れ（秒）・例（時刻。タイムベース）。並びは preceded の大きい順、同じなら
    条件の文字の順。causes が None なら記録の変数から候補を作る（_candidates。auto = True、一度も起きない候補は除き、
    上位 limit 個。max_coverage を明示すれば coverage がそれを超える候補を除く）。切る前の候補の数は causes_total、
    切ったかは truncated、使った limit は limit（明示の causes は切らないので None）。gap_s を明示すれば、それより長い
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
        terms = [{**term_dict(t), "kind": vars_.resolve(t.name)[0]} for t in cond.terms]
        out_causes.append({
            "condition": cond.text, "terms": terms, "fpga_only": all(t["kind"] == "fpga" for t in terms),
            "count": len(cs), "judged": len(judged), "preceded": preceded,
            "coverage": _coverage(cs, window, lo, hi, holes),
            "followed": followed / len(cs) if cs else None,
            "delay_s": _quantiles(delays), "examples": pairs, "_times": tuple(cs)})
    out_causes = _rank(out_causes)
    if auto:
        out_causes = [c for c in out_causes if c["count"]
                      and (max_coverage is None or c["coverage"] is None or c["coverage"] <= max_coverage)]
        out_causes = _merge_same(out_causes)
    causes_total = len(out_causes)                          # 切る前の数（切ったことを黙らない）
    if auto:
        out_causes = out_causes[:limit]
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
    unsure = [g for g in gaps if g["reason"] in UNSURE_REASONS]
    if unsure:
        warnings.append("時刻の一部が分からない区間が " + str(len(unsure)) + " 件（" +
                        "・".join(sorted({g["variable"] for g in unsure})) +
                        "。ピンの ch のずれが分からず、ずらさずに置いた。gaps の pin_skew_unknown。結果からは外していない）")
    missing = [g for g in gaps if g["reason"] not in UNSURE_REASONS]
    if missing:
        span, end = 0, lo                                  # 重なった欠けは 1 回だけ数える（gaps は t0 の順）
        for g in missing:
            span += max(0, g["t1"] - max(g["t0"], end))
            end = max(end, g["t1"])
        span /= hz
        per = {}                                               # 変数ごとの欠けの長さ（使った変数で合計が変わるので）
        for g in missing:
            if g.get("variable"):
                per[g["variable"]] = per.get(g["variable"], 0) + (g["t1"] - g["t0"]) / hz
        detail = "・".join(f"{n} {v * 1000:g} ms" for n, v in per.items())
        warnings.append(f"データに欠けが {len(missing)} 件（重なりを除いた長さ {span * 1000:g} ms。{detail}{'。' if detail else ''}gaps）。読みの空白・ピンの無い"
                        "区間にかかる結果は judged から外した。ほかの欠け（FPGA の欠落・取り込みの失敗等）は時刻を示すだけ"
                        + ("" if gap_s is not None else "（MCU のメモリの読みの空白は gap_s を渡したときだけ欠けにする。"
                                                       "読みの間隔は reads）"))
    # MCU のメモリの結果は範囲の最初と最後の値の差も（変化の回数 count と違えば、1 回の読みで何回分も動いている）
    delta = {t.name: s.values[-1] - s.values[0] for t in eff_cond.terms
             if vars_.resolve(t.name)[0] == "mcu" and (s := vars_.series(t.name)).values}
    return {"effect": {"condition": eff_cond.text, "count": len(eff), "delta": delta}, "window_s": window_s, "range": [lo, hi],
            "timebase_hz": hz, "auto": auto, "no_values": no_values, "warnings": warnings,
            "causes": out_causes, "causes_total": causes_total, "truncated": len(out_causes) < causes_total,
            "limit": limit if auto else None, "sampling": sampling, "reads": reads, "fpga_hidden": vars_.hidden, "gaps": gaps,
            "gap_s": gap_s}


def _reads_facts(reads: dict, hz: float) -> dict:
    """MCU のメモリの読みの事実: 回数・読めなかった回数・間隔（秒の分位）・いちばん長い空白（時刻）。変化の時刻の意味
    time_point（read_request = 変わった値を読みに行った転送の点（線上の点の回路）、value_returned = 値が返った周期（古い
    回路））と、線上の点の回路では変化があった範囲（前の読みの t_lo より後〜変わった値の語の t_hi）の幅の分位
    change_window_s と、端が分からない変化の数 change_window_unknown。相関の数え方は時刻（time_point）を使う。"""
    windows = reads.get("change_windows") or []
    known = [(hi - lo) / hz for lo, hi in windows if lo is not None and hi is not None]
    return {"count": reads["count"], "failed": reads["failed"],
            "interval_s": _quantiles([i / hz for i in reads["intervals"]]), "longest": reads["longest"],
            "time_point": reads.get("time_point", "value_returned"), "change_window_s": _quantiles(known),
            "change_window_unknown": len(windows) - len(known)}


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


def _rank(causes: list[dict]) -> list[dict]:
    """先行率の大きい順、同じなら条件の文字の順（事実だけで。経験則の順位づけをしない）。先行率が無いものは後ろ。"""
    return sorted(causes, key=lambda c: (c["preceded"] is None, -(c["preceded"] or 0), c["condition"]))


def _merge_same(causes: list[dict]) -> list[dict]:
    """成立する時刻（_times）がすべて同じ候補を 1 つに: 並びの最初のもの（_rank の事実の順）を残し、ほかを same に
    （自動の候補の重複。例 "restart changed and state=BUSY" と "restart changed and addr=CTRL"・"restart rose"
    がこのデータでは同じ出来事）。代表を「項が多い」等の決めごとで選ばない。順は保つ。"""
    groups: dict[tuple, list[dict]] = {}
    for c in causes:
        groups.setdefault(c["_times"], []).append(c)
    out = []
    for group in groups.values():
        keep = group[0]
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
            if (kind, name) in vars_.dups:                         # 名前が曖昧（記録の側の重複）: 候補にしない
                continue
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

