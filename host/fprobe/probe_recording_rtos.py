"""Probe の道具: 記録の stream "rtos" から、タスクの表・CPU 負荷の推移・割り込みの統計（記録だけがあるとき）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。記録（_reading）だけを読み、機器には触らない。

取っている最中・止めた後の時間軸（rtos_timeline_get / rtos_timeline_load）と同じ定義・同じ形で返す（画面・AI が
記録から自分で数えないように。行の上限で黙って欠けない: 記録の行を全部読む）。

- 記録の時間軸: run（タスクの区間。body の idle = アイドルの印）・gap（抜け）・isr（割り込みの区間）。情報のある時間 =
  run のある時間 − 抜け（止まっていた間・記録していない間は run が無いので、情報の無い時間 not_recorded として数える）。
- 割り込み: 入れ子の区間から回ごとの自分の時間（中に入れ子になった回の時間を引く）。負荷の分母は stream rtos の
  最初から最後まで。統計は fprobe.profile.exceptions と同じ形（profile_exceptions と同じ表で描ける）。
"""

from .profile.exceptions import Activation, ExceptionProfile
from .profile.stats import Distribution
from .rtos.analysis import load_series
from .rtos.timeline import Gap, Run, Timeline

__all__ = ["RecordingRtosTools", "isr_activations_of", "timeline_of"]

STREAM = "rtos"
WINDOWS_LIMIT = 1000                     # recording_cpu_load が返す窓の数の既定の上限


def timeline_of(events) -> tuple[Timeline, set[int], list[tuple[int, int]]] | None:
    """記録の stream rtos の出来事 → (時間軸, アイドルのタスク, 割り込みの区間)。run が無ければ None。"""
    runs: list[Run] = []
    recorded_gaps: list[tuple[int, int, str]] = []
    isr: list[tuple[int, int]] = []
    idle: set[int] = set()
    method, resolution = None, 0.0
    for e in events:
        if e.t_end is None:
            continue
        body = e.body or {}
        if e.kind == "run":
            runs.append(Run(e.a, str(body.get("task") or f"0x{e.a:08X}"), e.t, e.t_end, e.uncertainty or 0,
                            body.get("samples")))
            if body.get("idle"):
                idle.add(e.a)
            method = method or body.get("method")
            resolution = resolution or float(body.get("resolution") or 0)
        elif e.kind == "gap":
            recorded_gaps.append((e.t, e.t_end, str(body.get("reason") or "gap")))
        elif e.kind == "isr":
            isr.append((e.t, e.t_end))
    if not runs:
        return None
    runs.sort(key=lambda r: r.t0)
    t0, t1 = runs[0].t0, max(r.t1 for r in runs)
    tl = Timeline(method or "sampled", t0, t1, resolution, (method or "sampled") == "sampled", runs)
    gaps = _merged(recorded_gaps)                 # 重なった抜けを二重に数えない（理由は最初のもの）
    # run の無い時間（止まっていた・記録していない）も情報が無い。記録の抜けと重ねて数えない
    cursor = t0
    for r in runs:
        if r.t0 > cursor:
            gaps += [(a, b, "not_recorded") for a, b in _outside(cursor, r.t0, gaps)]
        cursor = max(cursor, r.t1)
    tl.gaps = [Gap(a, b, reason) for a, b, reason in sorted(gaps)]
    return tl, idle, isr


def _merged(gaps: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for a, b, reason in sorted(gaps):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b), out[-1][2])
        else:
            out.append((a, b, reason))
    return out


def _outside(a: int, b: int, gaps: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """[a, b) のうち gaps に入っていない所。"""
    out, start = [], a
    for g0, g1, _ in sorted(gaps):
        if g1 <= start or g0 >= b:
            continue
        if g0 > start:
            out.append((start, g0))
        start = max(start, g1)
    if start < b:
        out.append((start, b))
    return out


def isr_activations_of(events) -> tuple[list[Activation], dict[int, str], tuple[int, int] | None]:
    """記録の stream rtos の出来事 → (割り込みの回, 番号 → 名前, stream rtos の最初〜最後)。回の自分の時間は、中に
    入れ子になった回の時間を引いたもの。"""
    rows: list[tuple[int, int, int]] = []
    names: dict[int, str] = {}
    lo = hi = None
    for e in events:
        end = e.t if e.t_end is None else e.t_end
        lo = e.t if lo is None else min(lo, e.t)
        hi = end if hi is None else max(hi, end)
        if e.kind == "isr" and e.t_end is not None:
            rows.append((e.t, e.t_end, e.a))
            name = (e.body or {}).get("name")
            if name:
                names.setdefault(e.a, str(name))
    ordered = sorted(range(len(rows)), key=lambda i: (rows[i][0], -rows[i][1]))
    nested = [0] * len(rows)
    stack: list[int] = []
    for i in ordered:
        t, t_end, _ = rows[i]
        while stack and rows[stack[-1]][1] <= t:
            stack.pop()
        if stack and t_end <= rows[stack[-1]][1]:
            nested[stack[-1]] += t_end - t
        stack.append(i)
    acts = [Activation(n, t, t_end, t_end - t, t_end - t - nested[i]) for i, (t, t_end, n) in enumerate(rows)]
    span = (lo, hi) if lo is not None and hi > lo else None
    return acts, names, span


class RecordingRtosTools:
    """記録からのタスクの表・CPU 負荷・割り込み（Probe の mixin）。"""

    def recording_cpu_load(self, window_us: float | None = None, limit: int = WINDOWS_LIMIT) -> dict:
        """記録の stream rtos の run・gap・isr から、タスクの表（stats: rtos_timeline_get の stats と同じ形）と、
        window_us を渡せば窓ごとの CPU 負荷の推移（rtos_timeline_load と同じ形。窓が limit を超えたら先頭の limit 個、
        windows_total に全体の数）。情報のある時間 = run のある時間 − 抜け（run の無い時間は抜け not_recorded）。"""
        view = self._reading()
        hz = view.meta["timebase_hz"]
        built = timeline_of(view.iter_events(streams=[STREAM], kinds=["run", "gap", "isr"]))
        if built is None:
            return {"recording": view.directory.name, "timebase_hz": hz, "t0": None, "t1": None, "stats": None}
        tl, idle, isr = built
        stats = tl.stats()
        for row in stats["tasks"].values():
            row.update({f"{k}_us": row[k] * 1e6 / hz for k in ("total", "max_run", "min_run", "mean_run")})
        out = {"recording": view.directory.name, "method": tl.method, "timebase_hz": hz, "t0": tl.t0, "t1": tl.t1,
               "lower_bound": tl.lower_bound, "stats": stats,
               "gaps_not_recorded": sum(1 for g in tl.gaps if g.reason == "not_recorded")}
        if window_us is not None:
            if window_us <= 0:
                raise ValueError("window_us は正")
            load = load_series(tl, max(1, round(window_us * hz / 1e6)), idle, isr, hz)
            load["windows_total"] = len(load["windows"])
            load["windows"] = load["windows"][:limit]
            load["limit"] = limit
            out["load"] = load
        return out

    def recording_interrupts(self, number: int | None = None, bins: int | None = None, longest: int | None = None,
                             at: int | None = None) -> dict:
        """記録の stream rtos の割り込みの区間（isr）から、割り込みごとの回数・時間（含む／自分）・負荷・最長の回の時刻
        （profile_exceptions と同じ形。負荷の分母は stream rtos の最初から最後まで。clock_hz = タイムベース）。
        number・bins・longest を渡すと、その割り込みの含む時間の分布（bins 個の等しい幅の箱）と長い順の上位 longest 回
        （t・t_end・inclusive・bin）。at（タイムベース）を渡すと、その時刻に処理していた割り込みの番号（at_numbers）。"""
        view = self._reading()
        hz = view.meta["timebase_hz"]
        acts, names, span = isr_activations_of(view.iter_events(streams=[STREAM]))
        profile = ExceptionProfile(acts, None if span is None else span[1] - span[0], hz, names=names)
        out = {"recording": view.directory.name, "timebase_hz": hz, "span": None if span is None else list(span),
               "exceptions": profile.exceptions(), "activations": len(acts)}
        if number is not None and bins is not None and longest is not None:
            mine = [a for a in acts if a.number == number]
            dist = Distribution([a.inclusive for a in mine])
            top = sorted(mine, key=lambda a: (-a.inclusive, a.start))[:longest]
            hist = dist.histogram(bins) if mine else []
            per_bin: dict[int, Activation] = {}
            for a in mine:                                       # 箱ごとのいちばん長い回（同じ長さなら早い方）
                k = dist.bin_of(a.inclusive, bins)
                if k not in per_bin or (a.inclusive, -a.start) > (per_bin[k].inclusive, -per_bin[k].start):
                    per_bin[k] = a
            for k, box in enumerate(hist):
                a = per_bin.get(k)
                box["longest"] = None if a is None else {"t": a.start, "t_end": a.end, "inclusive": a.inclusive}
            out["distribution"] = {
                "number": number, "bins": bins, "longest_limit": longest, "histogram": hist,
                "longest": [{"t": a.start, "t_end": a.end, "inclusive": a.inclusive, "bin": dist.bin_of(a.inclusive, bins)}
                            for a in top]}
        if at is not None:                                       # 外 → 内（始めの早い順。最後が実行中の回）
            inside = sorted((a for a in acts if a.start <= at < a.end), key=lambda a: (a.start, -a.end))
            out["at_numbers"] = list(dict.fromkeys(a.number for a in inside))
        return out
