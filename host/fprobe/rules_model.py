"""規則の定義（U25）: 規則・規則集の型、YAML / JSON からの読み込みと書き方の検査、判定の悪さの並び。

記録を読む評価は fprobe.rules_eval、検査の実行は fprobe.rules（check）。fprobe.rules が再公開する。
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
