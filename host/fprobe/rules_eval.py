"""規則の評価（U25）: 記録（fprobe.recording）から規則の種類ごとに実測と違反を求める。

種類は区間・周期・順序・件数・never・値・状態。機器には触れない。fprobe.rules の check が使う。
"""

import bisect
import statistics
from typing import Any

from .recording import Event, Recording
from .rules_model import Rule, RuleError


# --- 記録を読む -----------------------------------------------------------------------------

def field_value(event: Event, name: str) -> Any:
    """field の値: a / b / kind / stream / body.<名前>（入れ子は . でつなぐ）。無ければ None。"""
    if name in ("a", "b", "kind", "stream"):
        return getattr(event, name)
    if name.startswith("body."):
        value: Any = event.body
        for part in name[5:].split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value
    raise RuleError(f"field {name!r} は a / b / kind / stream / body.<名前>")


class _Context:
    """1 回の検査の共通: 記録・範囲・換算。"""

    def __init__(self, recording: Recording, t0: int | None, t1: int | None):
        self.r, self.t0, self.t1 = recording, t0, t1
        self.hz = recording.meta["timebase_hz"]
        span = recording.span()
        self.origin = t0 if t0 is not None else (span[0] if span else 0)
        self.end = t1 if t1 is not None else (span[1] if span else self.origin)

    def find(self, cond: dict) -> list[Event]:
        q = dict(cond)
        if self.t0 is not None:
            q["t0"] = max(q.get("t0", self.t0), self.t0)
        if self.t1 is not None:
            q["t1"] = min(q.get("t1", self.t1), self.t1)
        return self.r.find(q)

    def us(self, ticks: float) -> float:
        return round(ticks * 1e6 / self.hz, 6)

    def ticks(self, us: float) -> int:
        return round(us * self.hz / 1e6)

    def violation(self, t: int, kind: str, message: str, value: Any = None, t_end: int | None = None) -> dict:
        return {"t": t, "t_end": t_end, "at_us": self.us(t - self.origin), "kind": kind, "message": message,
                "value": value}


def _stats(values_us: list[float]) -> dict:
    if not values_us:
        return {"count": 0}
    return {"count": len(values_us), "min_us": min(values_us), "max_us": max(values_us),
            "mean_us": round(statistics.fmean(values_us), 6), "stdev_us": round(statistics.pstdev(values_us), 6)}


def _bounds(lo: float | None, hi: float | None, value: float) -> str | None:
    if lo is not None and value < lo:
        return "too_short"
    if hi is not None and value > hi:
        return "too_long"
    return None


def _interval(ctx: _Context, p: dict) -> tuple[dict, list[dict], bool]:
    starts, ends = ctx.find(p["start"]), ctx.find(p["end"])
    lo, hi = p.get("min_us"), p.get("max_us")
    durations, violations, unmatched, open_ = [], [], 0, 0
    j = 0
    for i, s in enumerate(starts):
        limit = starts[i + 1].t if i + 1 < len(starts) else None
        while j < len(ends) and ends[j].t < s.t:
            j += 1
        if j < len(ends) and (limit is None or ends[j].t <= limit):
            d = ctx.us(ends[j].t - s.t)
            durations.append(d)
            kind = _bounds(lo, hi, d)
            if kind:
                violations.append(ctx.violation(s.t, kind, f"区間 {d} µs（期待 {lo} 〜 {hi} µs）", d, ends[j].t))
            j += 1
        elif limit is not None:
            unmatched += 1
            violations.append(ctx.violation(s.t, "no_end", "終わりが来ないまま次の開始", None, limit))
        else:
            open_ += 1                                 # 記録の終わりまで終わりが無い（切れただけかもしれない）
    return {**_stats(durations), "unmatched": unmatched, "open": open_}, violations, bool(durations or unmatched)


def _period(ctx: _Context, p: dict) -> tuple[dict, list[dict], bool]:
    events = ctx.find(p["event"])
    if "period_us" in p:
        tol = p.get("tolerance_us", 0)
        lo, hi = p["period_us"] - tol, p["period_us"] + tol
    else:
        lo, hi = p.get("min_us"), p.get("max_us")
    gaps, violations = [], []
    for a, b in zip(events, events[1:]):
        d = ctx.us(b.t - a.t)
        gaps.append(d)
        kind = _bounds(lo, hi, d)
        if kind:
            violations.append(ctx.violation(a.t, kind, f"間隔 {d} µs（期待 {lo} 〜 {hi} µs）", d, b.t))
    measured = _stats(gaps)
    if gaps:
        measured["jitter_us"] = round(max(gaps) - min(gaps), 6)
    return measured, violations, bool(gaps)


def _first_at(events: list[Event], times: list[int], t: int, end: int | None, exclude: Event) -> Event | None:
    """t 以後・end より前の最初の出来事（exclude と同じものは除く）。"""
    k = bisect.bisect_left(times, t)
    while k < len(events) and (end is None or events[k].t < end):
        if events[k] != exclude:
            return events[k]
        k += 1
    return None


def _sequence(ctx: _Context, p: dict) -> tuple[dict, list[dict], bool]:
    triggers = ctx.find(p["trigger"])
    steps = [ctx.find(c) for c in p["steps"]]
    step_times = [[e.t for e in s] for s in steps]
    forbids = [ctx.find(c) for c in p.get("forbid", [])]
    forbid_times = [[e.t for e in f] for f in forbids]
    within = ctx.ticks(p["within_us"]) if "within_us" in p else None
    completed, violations = 0, []
    for i, trig in enumerate(triggers):
        end = triggers[i + 1].t if i + 1 < len(triggers) else None
        if within is not None:
            end = trig.t + within if end is None else min(end, trig.t + within)
        cur, last, missing = trig.t, trig, None
        for k, (events, times) in enumerate(zip(steps, step_times)):
            hit = _first_at(events, times, cur, end, last if k else trig)
            if hit is None:
                missing = k
                break
            cur, last = hit.t, hit
        if missing is not None:
            violations.append(ctx.violation(trig.t, "missing_step", f"手順 {missing} が来ない", missing, end))
            continue
        bad = None
        for events, times in zip(forbids, forbid_times):
            hit = _first_at(events, times, trig.t, cur + 1, trig)
            if hit is not None and (bad is None or hit.t < bad.t):
                bad = hit
        if bad is not None:
            violations.append(ctx.violation(trig.t, "forbidden", f"禁止の出来事（{bad.stream} {bad.kind}）が挟まった",
                                            {"stream": bad.stream, "kind": bad.kind, "t": bad.t}, bad.t))
            continue
        completed += 1
    return {"triggers": len(triggers), "completed": completed, "failed": len(triggers) - completed}, violations, \
        bool(triggers)


def _count(ctx: _Context, p: dict) -> tuple[dict, list[dict], bool]:
    events = ctx.find(p["event"])
    lo, hi = p.get("min"), p.get("max")
    violations = []
    if "window_us" not in p:
        n = len(events)
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            violations.append(ctx.violation(events[0].t if events else ctx.origin, "count",
                                            f"件数 {n}（期待 {lo} 〜 {hi}）", n))
        return {"count": n}, violations, True
    width = ctx.ticks(p["window_us"])
    counts = []
    times = [e.t for e in events]
    w0 = ctx.origin
    while w0 < ctx.end or not counts:
        w1 = w0 + width
        n = bisect.bisect_left(times, w1) - bisect.bisect_left(times, w0)
        counts.append(n)
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            violations.append(ctx.violation(w0, "count", f"窓の件数 {n}（期待 {lo} 〜 {hi}）", n, w1))
        w0 = w1
    # 窓は検査の範囲の始め（window_t0）から window_us ごとに区切る（窓の数え方は起点で変わるので、使った起点を返す）
    return {"count": len(events), "windows": len(counts), "min_per_window": min(counts),
            "max_per_window": max(counts), "window_t0": ctx.origin, "window_us": p["window_us"]}, violations, True


def _never(ctx: _Context, p: dict) -> tuple[dict, list[dict], bool]:
    events = ctx.find(p["event"])
    return {"count": len(events)}, [ctx.violation(e.t, "occurred", f"起きてはいけない出来事（{e.stream} {e.kind}）",
                                                  e.body if e.body is not None else e.a, e.t_end)
                                    for e in events], True


def _value(ctx: _Context, p: dict) -> tuple[dict, list[dict], bool]:
    events = ctx.find(p["event"])
    lo, hi = p.get("min"), p.get("max")
    numbers, violations = [], []
    for e in events:
        v = field_value(e, p["field"])
        number = isinstance(v, (int, float)) and not isinstance(v, bool)
        if number:
            numbers.append(v)
        if "equals" in p and v != p["equals"]:
            violations.append(ctx.violation(e.t, "not_equal", f"{p['field']} = {v!r}（期待 {p['equals']!r}）", v))
        elif (lo is not None or hi is not None) and not number:
            violations.append(ctx.violation(e.t, "not_number", f"{p['field']} = {v!r} は数でない", v))
        elif number and ((lo is not None and v < lo) or (hi is not None and v > hi)):
            violations.append(ctx.violation(e.t, "out_of_range", f"{p['field']} = {v}（期待 {lo} 〜 {hi}）", v))
    measured: dict[str, Any] = {"count": len(events)}
    if numbers:
        measured.update(min=min(numbers), max=max(numbers))
    return measured, violations, bool(events)


def _label(labels: dict, value: Any) -> str:
    return str(labels.get(str(value), value))


def _state_series(ctx: _Context, cond: dict, field_name: str, labels: dict) -> list[tuple[int, str]]:
    """状態の変化点 (時刻, 状態の名前)。同じ値の続きは 1 つにまとめる。"""
    out: list[tuple[int, str]] = []
    for e in ctx.find(cond):
        v = field_value(e, field_name)
        if v is None:
            continue
        name = _label(labels, v)
        if not out or out[-1][1] != name:
            out.append((e.t, name))
    return out


def _states(ctx: _Context, p: dict) -> tuple[dict, list[dict], bool]:
    labels = p.get("labels") or {}
    series = _state_series(ctx, p["event"], p["field"], labels)
    allowed = {(_label(labels, a), _label(labels, b)) for a, b in p["allowed"]}
    violations = []
    if "initial" in p and series and series[0][1] != _label(labels, p["initial"]):
        violations.append(ctx.violation(series[0][0], "initial", f"最初の状態 {series[0][1]}（期待 "
                                        f"{_label(labels, p['initial'])}）", series[0][1]))
    for (_, a), (t, b) in zip(series, series[1:]):
        if (a, b) not in allowed:
            violations.append(ctx.violation(t, "illegal_transition", f"許されない遷移 {a} -> {b}", f"{a} -> {b}"))
    return {"transitions": max(0, len(series) - 1), "states": len({s for _, s in series}),
            "illegal": sum(v["kind"] == "illegal_transition" for v in violations)}, violations, bool(series)


EVALUATORS = {"interval": _interval, "period": _period, "sequence": _sequence, "count": _count, "never": _never,
              "value": _value, "states": _states}


def _evaluate(ctx: _Context, rule: Rule) -> tuple[dict, list[dict], bool]:
    return EVALUATORS[rule.kind](ctx, rule.params)


def recording_info(recording: Recording) -> dict:
    meta = recording.meta
    return {"name": recording.directory.name, "title": meta.get("name"), "board": meta.get("board"),
            "design": meta.get("design"), "elf": meta.get("elf"), "timebase_hz": meta["timebase_hz"],
            "span": recording.span()}
