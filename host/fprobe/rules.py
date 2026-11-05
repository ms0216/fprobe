"""規則の検査（U25）: 区間の上限・周期・順序・件数・値・状態遷移の規則を宣言的に書き、共通の時間軸の記録
（fprobe.recording）から違反を探す。期待値と実測の比較、違反の派生イベント（stream "rule"）、状態遷移図（dot）、
要件と対応づけた証跡（Markdown / JSON）。

仕様: docs/superpowers/specs/2026-10-18-u25-u44-rules-hooks-design.md の 1 節。
機器には触らない（記録を読むだけ。印は記録の SQLite に書く）。条件は Recording.find と同じ形。

分け方: 規則の定義と読み込みは rules_model.py、記録からの評価は rules_eval.py、状態遷移図と証跡は rules_report.py。ここには検査の実行（check）と再公開を置く。
"""

from datetime import datetime

from .recording import Event, Recording
from .rules_eval import EVALUATORS, _Context, _evaluate, field_value, recording_info  # noqa: F401  (再公開)
from .rules_model import (  # noqa: F401  (再公開)
    COMMON, CONDITION_KEYS, KINDS, RANK, RULE_STREAM, Rule, RuleError, RuleSet, _condition, _number, load_yaml, parse_rule, worst,
)
from .rules_report import evidence, render_report, state_machine  # noqa: F401  (再公開)

__all__ = ["RULE_STREAM", "Rule", "RuleError", "RuleSet", "check", "field_value", "render_report", "state_machine",
           "worst"]


def _verdict(rule: Rule, violations: list, has_data: bool) -> str:
    if violations:
        return "fail" if rule.severity == "error" else "warn"
    if not has_data and not rule.allow_empty:
        return "no_data"
    return "pass"


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
                "violations": violations[:limit], "violations_total": len(violations), "violations_limit": limit}
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
