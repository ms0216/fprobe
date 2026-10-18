"""規則の検査（U25）: 区間の上限・周期・順序・件数・値・状態遷移の規則を宣言的に書き、共通の時間軸の記録
（fprobe.recording）から違反を探す。期待値と実測の比較、違反の派生イベント（stream "rule"）、状態遷移図（dot）、
要件と対応づけた証跡（Markdown / JSON）。

仕様: docs/superpowers/specs/2026-10-18-u25-u44-rules-hooks-design.md の 1 節。
機器には触らない（記録を読むだけ。印は記録の SQLite に書く）。条件は Recording.find と同じ形。
"""

import bisect
import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .recording import Event, Recording

__all__ = ["RULE_STREAM", "Rule", "RuleError", "RuleSet", "check", "field_value", "render_report", "state_machine",
           "worst"]

RULE_STREAM = "rule"                       # 違反の派生イベントの stream
COMMON = {"id", "kind", "description", "requirement", "severity", "allow_empty"}
CONDITION_KEYS = {"stream", "kind", "t0", "t1", "a", "b", "body"}
# kind → (必須の欄, 任意の欄, 期待値として結果に写す欄)
KINDS: dict[str, tuple[set[str], set[str], tuple[str, ...]]] = {
    "interval": ({"start", "end"}, {"min_us", "max_us"}, ("min_us", "max_us")),
    "period": ({"event"}, {"period_us", "tolerance_us", "min_us", "max_us"},
               ("period_us", "tolerance_us", "min_us", "max_us")),
    "sequence": ({"trigger", "steps"}, {"within_us", "forbid"}, ("within_us",)),
    "count": ({"event"}, {"min", "max", "window_us"}, ("min", "max", "window_us")),
    "never": ({"event"}, set(), ()),
    "value": ({"event", "field"}, {"min", "max", "equals"}, ("min", "max", "equals")),
    "states": ({"event", "field", "allowed"}, {"labels", "initial"}, ("allowed", "initial")),
}
# 判定の悪さ（大きいほど悪い）。故障注入の試行の判定も同じ並びに入れる
RANK = {"pass": 0, "warn": 1, "no_data": 2, "not_triggered": 3, "error": 3, "inject_error": 3, "fail": 4}


class RuleError(ValueError):
    """規則の書き方の誤り。"""


def worst(verdicts) -> str:
    return max(verdicts, key=lambda v: RANK.get(v, 3), default="pass")


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    params: dict
    requirement: tuple[str, ...] = ()
    severity: str = "error"
    allow_empty: bool = False
    description: str = ""


@dataclass(frozen=True)
class RuleSet:
    name: str
    rules: tuple[Rule, ...]
    requirements: dict = field(default_factory=dict)
    sha256: str = ""
    source: str | None = None                  # 読んだファイル

    @classmethod
    def parse(cls, data: dict, source: str | None = None) -> "RuleSet":
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            raise RuleError("規則集は {name, requirements, rules: [...]} の形")
        unknown = set(data) - {"name", "requirements", "rules", "description"}
        if unknown:
            raise RuleError(f"規則集の未知の欄: {sorted(unknown)}")
        requirements = data.get("requirements") or {}
        if not isinstance(requirements, dict):
            raise RuleError("requirements は {要件 ID: 文}")
        rules, seen = [], set()
        for n, raw in enumerate(data["rules"]):
            rule = parse_rule(raw, where=f"rules[{n}]")
            if rule.id in seen:
                raise RuleError(f"規則の id {rule.id!r} が重複")
            seen.add(rule.id)
            rules.append(rule)
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return cls(str(data.get("name") or "rules"), tuple(rules), {str(k): str(v) for k, v in requirements.items()},
                   hashlib.sha256(canonical.encode()).hexdigest(), source)

    @classmethod
    def parse_text(cls, text: str, source: str | None = None) -> "RuleSet":
        """JSON か YAML の文字列。"""
        try:
            data = json.loads(text)
        except ValueError:
            data = load_yaml(text)
        return cls.parse(data, source)

    @classmethod
    def load(cls, path: str | Path) -> "RuleSet":
        return cls.parse_text(Path(path).read_text(encoding="utf-8"), str(path))


def load_yaml(text: str) -> Any:
    try:
        import yaml
    except ImportError:
        raise RuleError("YAML を読むには PyYAML が要る（pip install pyyaml）。JSON なら要らない") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RuleError(f"YAML として読めない: {e}") from None


def _condition(value: Any, where: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise RuleError(f"{where} は条件（recording_find と同じ {{stream, kind, a, b, body}}）")
    unknown = set(value) - CONDITION_KEYS
    if unknown:
        raise RuleError(f"{where} の未知の欄: {sorted(unknown)}")
    return value


def _number(params: dict, key: str, where: str, positive: bool = False) -> None:
    if key in params:
        v = params[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (positive and v <= 0) or v < 0:
            raise RuleError(f"{where}.{key} は{'正' if positive else '0 以上'}の数")


def parse_rule(raw: Any, where: str = "rule") -> Rule:
    if not isinstance(raw, dict):
        raise RuleError(f"{where} は辞書")
    rid = raw.get("id")
    if not isinstance(rid, str) or not rid:
        raise RuleError(f"{where}: id（文字列）が要る")
    where = f"規則 {rid!r}"
    kind = raw.get("kind")
    if kind not in KINDS:
        raise RuleError(f"{where}: kind は {sorted(KINDS)} のどれか（{kind!r}）")
    needed, optional, _ = KINDS[kind]
    unknown = set(raw) - COMMON - needed - optional
    if unknown:
        raise RuleError(f"{where}: 未知の欄 {sorted(unknown)}（kind {kind} の欄は {sorted(needed | optional)}）")
    missing = needed - set(raw)
    if missing:
        raise RuleError(f"{where}: {sorted(missing)} が要る")
    params = {k: v for k, v in raw.items() if k not in COMMON}
    for key in ("start", "end", "event", "trigger"):
        if key in params:
            _condition(params[key], f"{where}.{key}")
    for key in ("steps", "forbid"):
        if key in params:
            if not isinstance(params[key], list) or (key == "steps" and not params[key]):
                raise RuleError(f"{where}.{key} は条件の列")
            for n, c in enumerate(params[key]):
                _condition(c, f"{where}.{key}[{n}]")
    for key in ("min_us", "max_us", "tolerance_us", "min", "max"):
        _number(params, key, where)
    for key in ("period_us", "within_us", "window_us"):
        _number(params, key, where, positive=True)
    if kind == "interval" and not {"min_us", "max_us"} & set(params):
        raise RuleError(f"{where}: min_us か max_us が要る")
    if kind == "period" and not ({"period_us"} & set(params) or {"min_us", "max_us"} & set(params)):
        raise RuleError(f"{where}: period_us（と tolerance_us）か min_us・max_us が要る")
    if kind == "period" and "period_us" in params and {"min_us", "max_us"} & set(params):
        raise RuleError(f"{where}: period_us と min_us・max_us は同時に書かない")
    if kind == "count" and not {"min", "max"} & set(params):
        raise RuleError(f"{where}: min か max が要る")
    if kind == "value" and not {"min", "max", "equals"} & set(params):
        raise RuleError(f"{where}: min・max・equals のどれかが要る")
    if kind in ("value", "states") and not isinstance(params["field"], str):
        raise RuleError(f"{where}.field は a / b / kind / body.<名前>")
    if kind == "states":
        allowed = params["allowed"]
        if not isinstance(allowed, list) or not all(isinstance(p, list) and len(p) == 2 for p in allowed):
            raise RuleError(f"{where}.allowed は [from, to] の列")
        if "labels" in params and not isinstance(params["labels"], dict):
            raise RuleError(f"{where}.labels は {{値: 名前}}")
    severity = raw.get("severity", "error")
    if severity not in ("error", "warning"):
        raise RuleError(f"{where}: severity は error か warning")
    requirement = raw.get("requirement") or ()
    requirement = (requirement,) if isinstance(requirement, str) else tuple(str(r) for r in requirement)
    return Rule(rid, kind, params, requirement, severity, bool(raw.get("allow_empty", False)),
                str(raw.get("description", "")))


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
    return {"count": len(events), "windows": len(counts), "min_per_window": min(counts),
            "max_per_window": max(counts)}, violations, True


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


def _verdict(rule: Rule, violations: list, has_data: bool) -> str:
    if violations:
        return "fail" if rule.severity == "error" else "warn"
    if not has_data and not rule.allow_empty:
        return "no_data"
    return "pass"


def recording_info(recording: Recording) -> dict:
    meta = recording.meta
    return {"name": recording.directory.name, "title": meta.get("name"), "board": meta.get("board"),
            "design": meta.get("design"), "elf": meta.get("elf"), "timebase_hz": meta["timebase_hz"],
            "span": recording.span()}


def check(recording: Recording, ruleset: RuleSet, *, t0: int | None = None, t1: int | None = None,
          baseline: Recording | None = None, mark: bool = False, limit: int = 100) -> dict:
    """規則集を記録の範囲 [t0, t1) に当てる。mark なら違反を派生イベント（stream "rule"）として記録に書く
    （同じ規則集の前の印は消す）。baseline（別の記録）を渡すと実測を比べる。violations は規則ごとに limit 件まで。"""
    ctx = _Context(recording, t0, t1)
    base_ctx = _Context(baseline, None, None) if baseline is not None else None
    results, marks = [], []
    for rule in ruleset.rules:
        measured, violations, has_data = _evaluate(ctx, rule)
        verdict = _verdict(rule, violations, has_data)
        _, _, expected_keys = KINDS[rule.kind]
        expected = {k: rule.params[k] for k in expected_keys if k in rule.params}
        if rule.kind == "never":
            expected = {"count": 0}
        elif rule.kind == "sequence":
            expected["steps"] = len(rule.params["steps"])
        item = {"id": rule.id, "kind": rule.kind, "description": rule.description, "requirement": list(rule.requirement),
                "severity": rule.severity, "verdict": verdict, "expected": expected, "measured": measured,
                "violations": violations[:limit], "violations_total": len(violations)}
        if base_ctx is not None:
            base_measured, _, _ = _evaluate(base_ctx, rule)
            delta = {k: round(v - base_measured[k], 6) for k, v in measured.items()
                     if isinstance(v, (int, float)) and isinstance(base_measured.get(k), (int, float))}
            item["baseline"] = {"measured": base_measured, "delta": delta}
        results.append(item)
        for v in violations:
            marks.append(Event(v["t"], RULE_STREAM, "violation", t_end=v["t_end"],
                               body={"ruleset": ruleset.name, "rule": rule.id, "requirement": list(rule.requirement),
                                     "violation": v["kind"], "message": v["message"], "value": v["value"],
                                     "severity": rule.severity}))
    counts = {k: sum(r["verdict"] == k for r in results) for k in ("pass", "fail", "warn", "no_data")}
    out = {"verdict": worst(r["verdict"] for r in results), "counts": counts,
           "ruleset": {"name": ruleset.name, "sha256": ruleset.sha256, "source": ruleset.source,
                       "requirements": dict(ruleset.requirements)},
           "recording": recording_info(recording), "window": {"t0": t0, "t1": t1}, "rules": results,
           "checked_at": datetime.now().isoformat(timespec="seconds")}
    if baseline is not None:
        out["baseline"] = recording_info(baseline)
    if mark:
        out["marked"] = recording.replace_derived(RULE_STREAM, f"rules:{ruleset.name}", marks)
    return out


# --- 状態遷移図 ---------------------------------------------------------------------------------

def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def state_machine(recording: Recording, event: dict, field_name: str, labels: dict | None = None,
                  t0: int | None = None, t1: int | None = None) -> dict:
    """出来事の field の値を状態として、状態ごとの入った回数・滞在時間（µs。最後の状態は終わりが分からないので除く）、
    遷移ごとの回数と最初の時刻、Graphviz の dot。"""
    _condition(event, "event")
    ctx = _Context(recording, t0, t1)
    series = _state_series(ctx, event, field_name, labels or {})
    states: dict[str, dict] = {}
    dwell: dict[str, list[float]] = {}
    for t, name in series:
        states.setdefault(name, {"entries": 0, "first_t": t})["entries"] += 1
        dwell.setdefault(name, [])
    for (t, name), (t_next, _) in zip(series, series[1:]):
        dwell[name].append(ctx.us(t_next - t))
    for name, values in dwell.items():
        states[name]["dwell_us"] = ({"min": min(values), "max": max(values), "mean": round(statistics.fmean(values), 6),
                                     "total": round(sum(values), 6)} if values else None)
    transitions: dict[tuple[str, str], dict] = {}
    for (_, a), (t, b) in zip(series, series[1:]):
        transitions.setdefault((a, b), {"from": a, "to": b, "count": 0, "first_t": t})["count"] += 1
    lines = ["digraph states {", "  rankdir=LR;"]
    for name, info in states.items():
        d = info["dwell_us"]
        label = f"{name}\n{info['entries']} 回" + (f"・平均 {d['mean']:g} µs" if d else "")
        lines.append(f"  {_quote(name)} [label={_quote(label)}];")
    for x in transitions.values():
        lines.append(f"  {_quote(x['from'])} -> {_quote(x['to'])} [label=\"{x['count']}\"];")
    lines.append("}")
    return {"states": states, "transitions": list(transitions.values()), "changes": len(series),
            "initial": series[0][1] if series else None, "dot": "\n".join(lines) + "\n"}


# --- 証跡 ---------------------------------------------------------------------------------------

def _sha256_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def evidence(rules: dict | None = None, inject: dict | None = None, recording: Recording | None = None) -> dict:
    """証跡の中身（render_report の JSON と同じ）。要件 → 規則・故障注入の試行 → 判定の対応表を作る。"""
    rec = recording_info(recording) if recording is not None else (rules or {}).get("recording") or \
        (inject or {}).get("recording")
    elf = (rec or {}).get("elf")
    requirements: dict[str, dict] = {}
    texts = dict((rules or {}).get("ruleset", {}).get("requirements", {}))
    texts.update((inject or {}).get("requirements", {}))
    for kind, items in (("rule", (rules or {}).get("rules", [])), ("inject", (inject or {}).get("trials", []))):
        for item in items:
            for req in item.get("requirement", []):
                entry = requirements.setdefault(req, {"text": texts.get(req), "items": []})
                entry["items"].append({"type": kind, "id": item["id"], "verdict": item["verdict"]})
    for req, text in texts.items():
        requirements.setdefault(req, {"text": text, "items": []})
    for entry in requirements.values():
        entry["verdict"] = worst(i["verdict"] for i in entry["items"]) if entry["items"] else "not_covered"
    verdicts = [x["verdict"] for x in (rules, inject) if x]
    return {"generated": datetime.now().isoformat(timespec="seconds"), "verdict": worst(verdicts),
            "recording": rec, "elf": {"path": elf, "sha256": _sha256_file(elf)} if elf else None,
            "requirements": requirements, "rules": rules, "inject": inject}


def _md_value(v: Any) -> str:
    if v is None:
        return ""
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", " ")


def render_report(format: str, rules: dict | None = None, inject: dict | None = None,
                  recording: Recording | None = None) -> str:
    """証跡の文書: format markdown / json。rules は check の結果、inject は故障注入（fprobe.inject）の結果。"""
    if rules is None and inject is None:
        raise ValueError("規則の検査（rules_check）も故障注入（inject_run）もまだ無い")
    data = evidence(rules, inject, recording)
    if format == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    if format != "markdown":
        raise ValueError("format は markdown か json")
    out = ["# 試験の証跡", "", f"- 作成: {data['generated']}", f"- 総合判定: **{data['verdict']}**"]
    rec = data["recording"]
    if rec:
        out.append(f"- 記録: `{rec['name']}`（{_md_value(rec.get('title'))}。基板 {rec.get('board')}・設計 "
                   f"{rec.get('design')}・タイムベース {rec['timebase_hz']} Hz・範囲 {rec.get('span')}）")
    if data["elf"]:
        out.append(f"- ELF: `{data['elf']['path']}`（SHA-256 {data['elf']['sha256']}）")
    if rules:
        rs = rules["ruleset"]
        out.append(f"- 規則集: {rs['name']}（{rs.get('source') or '直接'}、SHA-256 {rs['sha256']}）")
    if inject:
        out.append(f"- 故障注入の台本: {inject['name']}（SHA-256 {inject.get('sha256')}）")
    out += ["", "## 要件ごとの判定", "", "| 要件 | 内容 | 判定 | 規則・試行 |", "|---|---|---|---|"]
    for req, entry in data["requirements"].items():
        items = "、".join(f"{i['type']}:{i['id']}={i['verdict']}" for i in entry["items"])
        out.append(f"| {req} | {_md_value(entry['text'])} | {entry['verdict']} | {items} |")
    if rules:
        out += ["", "## 規則ごとの期待値と実測", "", "| 規則 | 種類 | 判定 | 期待 | 実測 | 違反 |", "|---|---|---|---|---|---|"]
        for r in rules["rules"]:
            out.append(f"| {r['id']} | {r['kind']} | {r['verdict']} | {_md_value(r['expected'])} | "
                       f"{_md_value(r['measured'])} | {r['violations_total']} |")
        for r in rules["rules"]:
            if r["violations"]:
                out += ["", f"### 違反: {r['id']}", "", "| 時刻（記録の最初から µs） | t | 種類 | 内容 |", "|---|---|---|---|"]
                out += [f"| {v['at_us']} | {v['t']} | {v['kind']} | {_md_value(v['message'])} |" for v in r["violations"]]
                if r["violations_total"] > len(r["violations"]):
                    out.append(f"\nほか {r['violations_total'] - len(r['violations'])} 件")
            if "baseline" in r:
                out += ["", f"基準との差（{r['id']}）: {_md_value(r['baseline']['delta'])}"]
    if inject:
        out += ["", "## 故障注入", "", "| 試行 | 判定 | 注入の時刻 t | 番地 | 値 | 判定の内訳 |", "|---|---|---|---|---|---|"]
        for tr in inject["trials"]:
            checks = "、".join(f"{c['check']}={'ok' if c['ok'] else 'NG'}" for c in tr.get("checks", []))
            addr = tr.get("address")
            out.append(f"| {tr['id']} | {tr['verdict']} | {_md_value(tr.get('t'))} | "
                       f"{'' if addr is None else f'0x{addr:08X}'} | {_md_value(tr.get('value'))} | {checks} |")
    return "\n".join(out) + "\n"
