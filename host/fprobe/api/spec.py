"""API の仕様（host/api/fprobe_api.json）の読み込みと引数の検証。"""

import json
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from jsonschema import Draft202012Validator

__all__ = ["HINTS_PATH", "SPEC_PATH", "InvalidParams", "InvalidResult", "Method", "Spec", "load_hints", "load_spec", "result_schema_problems"]

SPEC_PATH = Path(__file__).resolve().parents[2] / "api" / "fprobe_api.json"
HINTS_PATH = SPEC_PATH.parent / "hints.json"             # 使い方の落とし穴の記録（session.hints）


class InvalidParams(Exception):
    """引数が仕様に合わない。validator = jsonschema の検証の種類（required・type・additionalProperties 等。ヒントを絞る事実）。"""

    def __init__(self, message: str, validator: str | None = None):
        super().__init__(message)
        self.validator = validator


class InvalidResult(Exception):
    """実装の応答が仕様の result に合わない（実装と仕様のずれ。試験が見つける）。"""


@dataclass(frozen=True)
class Method:
    name: str
    kind: str            # "read" / "control"
    summary: str
    impl: str            # Probe のメソッド名（session.* は ""）
    params: dict
    result: dict         # 応答の JSON Schema（daemon.jsonable を通した後の形。共通の形は Spec.defs を "#/$defs/名前" で引く）
    record: bool = True  # 操作の記録に残すか（画面の表示用の頻繁な読みは残さない）
    observes: bool = False  # control だが状態を変えない読み（要求の quiet で記録から外せる）
    binary: bool = False    # 応答をバイナリのフレームで返す（fprobe.api.binary。WebSocket の利用者だけ、MCP には出さない）
    caller: bool = False    # デーモンが呼び出し元 caller = {id, name, role} を実装へ渡す（引数の仕様には出さない）
    offline: bool = False   # 機器に触らない（記録・画面の状態を読むだけ）: 機器の作業スレッドでなく読みのスレッドで実行
    irreversible: bool = False  # 取り返しのつかない操作（Flash・オプションバイトの書き込み・消去等。画面は赤く確かめる）
    # PC のパスの引数: 名前 → (access = "exec" / "write", when = 真のときだけ確かめる真偽の引数の名前か None)。
    # デーモンが許可の表（fprobe.api.permissions）で確かめる（仕様の "x-path"・"x-path-when"）
    paths: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Spec:
    api_version: int
    methods: dict[str, Method]
    notifications: dict[str, dict]
    defs: dict[str, dict] = field(default_factory=dict)     # 応答の共通の形（仕様の "$defs"）
    _result_validators: dict = field(default_factory=dict, repr=False, compare=False)

    def result_schema(self, name: str) -> dict:
        """メソッド name の応答のスキーマ（$defs を含めた、それだけで検証できる形）。"""
        return {**self.methods[name].result, "$defs": self.defs}

    def validate_result(self, name: str, value: Any) -> None:
        """応答 value（daemon.jsonable を通した後の形）が仕様に合うか。合わなければ InvalidResult（最初の誤りの場所と理由）。"""
        validator = self._result_validators.get(name)
        if validator is None:
            validator = self._result_validators[name] = Draft202012Validator(self.result_schema(name))
        error = min(validator.iter_errors(value), key=lambda e: len(e.absolute_path), default=None)
        if error is not None:
            where = "".join(f"[{p!r}]" for p in error.absolute_path) or "(応答)"
            raise InvalidResult(f"{name}: {where}: {error.message}")

    def validate_params(self, name: str, params: dict) -> None:
        method = self.methods[name]                     # 未知のメソッドは KeyError
        errors = sorted(Draft202012Validator(method.params).iter_errors(params), key=lambda e: e.path)
        if errors:
            e = errors[0]
            where = ".".join(str(p) for p in e.path) or "(引数)"
            raise InvalidParams(f"{name}: {where}: {_param_message(e)}", e.validator)
        _integral(method.params, params)


_JSON_TYPES = {"string": "文字列", "integer": "整数", "number": "数", "boolean": "真偽値", "array": "配列", "object": "オブジェクト",
               "null": "null"}


def cut(text: str, limit: int) -> str:
    """誤りの文に載せる文字列。長いときは先頭だけにして、全体の長さを言う（欠けを明示する）。"""
    return text if len(text) <= limit else f"{text[:limit]}…（全体 {len(text)} 文字）"


def shown(value, limit: int = 120) -> str:
    """誤りの文に載せる値（repr。長いときは cut）。"""
    return cut(repr(value), limit)


def _param_message(e) -> str:
    """検証の誤り e を、何が足りない・何が違うかの日本語の文にする（jsonschema の英文をそのまま返さない）。
    種類を知らない検証は元の文を添える（事実を落とさない）。"""
    kind, rule = e.validator, e.validator_value
    if kind == "required":
        missing = [k for k in rule if k not in (e.instance if isinstance(e.instance, dict) else {})]
        return "必須の引数が無い: " + ", ".join(missing)
    if kind == "additionalProperties" and isinstance(e.instance, dict):
        props = e.schema.get("properties", {}) if isinstance(e.schema, dict) else {}
        extra = [k for k in e.instance if k not in props]
        return "未知の引数: " + ", ".join(extra) + "（この API の引数: " + (", ".join(props) or "なし") + "）"
    if kind == "type":
        want = rule if isinstance(rule, list) else [rule]
        note = ""
        if isinstance(e.instance, str) and any(t in ("integer", "number") for t in want):
            try:                                      # '0x20000000' のような文字列: JSON の数（10 進）で渡す
                note = f"。文字列でなく JSON の数で渡す（{e.instance} は {int(e.instance, 0)}）"
            except ValueError:
                note = "。文字列でなく JSON の数で渡す"
        return f"型が違う（{'または'.join(_JSON_TYPES.get(t, t) for t in want)}が必要。渡された値: {shown(e.instance)}{note}）"
    if kind == "enum":
        return f"受け付ける値は {', '.join(repr(v) for v in rule)} のどれか（渡された値: {shown(e.instance)}）"
    if kind in ("minimum", "exclusiveMinimum"):
        return f"{rule} {'より大きい' if kind == 'exclusiveMinimum' else '以上'}が必要（渡された値: {shown(e.instance)}）"
    if kind in ("maximum", "exclusiveMaximum"):
        return f"{rule} {'より小さい' if kind == 'exclusiveMaximum' else '以下'}が必要（渡された値: {shown(e.instance)}）"
    if kind == "minLength":
        return f"{rule} 文字以上が必要" + ("（空の文字列は受け付けない）" if rule == 1 else "")
    if kind == "maxLength":
        return f"{rule} 文字以下が必要"
    if kind == "minItems":
        return f"要素が {rule} 個以上必要"
    if kind == "maxItems":
        return f"要素は {rule} 個以下"
    return cut(e.message, 300)


def _integral(schema: dict, value: Any):
    """検証を通った値の中で、整数だけを受ける所（"integer"）の 3.0 のような整数値の小数を int にする（JSON Schema は 3.0 を整数と
    みなすが、実装は range(count) 等で TypeError になる）。dict・list は in place、戻り値は直した値。number も受ける所は触らない。"""
    kinds = [schema.get("type")] if "type" in schema else []
    for alt in schema.get("anyOf", []):
        kinds.append(alt.get("type"))
    if isinstance(value, float) and "integer" in kinds and "number" not in kinds and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in value:
            if key in props:
                value[key] = _integral(props[key], value[key])
    elif isinstance(value, list):
        subs = [schema] + schema.get("anyOf", [])
        items = next((s["items"] for s in subs if isinstance(s.get("items"), dict)), None)
        if items is not None:
            for i, v in enumerate(value):
                value[i] = _integral(items, v)
    return value


_PATH_ACCESS = ("exec", "write")


def _paths(method: str, params: dict) -> dict:
    """引数の "x-path"（exec / write）と "x-path-when"（真偽の引数の名前）。形が違えば仕様の誤り（ValueError）。"""
    props = params.get("properties", {})
    out = {}
    for name, schema in props.items():
        access = schema.get("x-path")
        if access is None:
            continue
        when = schema.get("x-path-when")
        if access not in _PATH_ACCESS or (when is not None and props.get(when, {}).get("type") != "boolean"):
            raise ValueError(f"{method}.{name}: x-path は {_PATH_ACCESS} のどれか、x-path-when は真偽の引数の名前")
        out[name] = (access, when)
    return out


def load_spec(path: Path | None = None) -> Spec:
    data = json.loads((path or SPEC_PATH).read_text(encoding="utf-8"))
    methods = {name: Method(name, m["kind"], m.get("summary", ""), m.get("impl", ""), m["params"], m.get("result", {}),
                            m.get("record", True), m.get("observes", False), m.get("binary", False),
                            m.get("caller", False), m.get("offline", False), m.get("irreversible", False),
                            _paths(name, m["params"]))
               for name, m in data["methods"].items()}
    return Spec(data["api_version"], methods, data.get("notifications", {}), data.get("$defs", {}))


_LEAF_TYPES = {"integer", "number", "string", "boolean", "null"}
_ANNOTATIONS = {"description", "title", "default", "examples", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "minLength", "maxLength", "pattern", "format", "minItems", "maxItems", "uniqueItems"}


def result_schema_problems(spec: Spec) -> list[str]:
    """応答のスキーマが「形をすべて書いた」約束になっているかの点検（違反の一覧。空なら良い）。
    約束: どの節も型を持つ（type・enum・const・anyOf・$ref のどれか。何でもよい値は $defs の Json を明示して引く）。
    object は 2 つの形のどちらか: 欄の決まった記録（properties と additionalProperties: false）か、名前 → 値の表
    （properties なしの additionalProperties: スキーマ）。array は items（固定の組は prefixItems と items: false）。
    required は properties にある名前だけ。$ref は $defs にある名前だけ。使われない $defs は残さない。"""
    problems: list[str] = []
    used: set[str] = set()

    def walk(schema, where: str) -> None:
        if not isinstance(schema, dict):
            problems.append(f"{where}: スキーマが辞書でない")
            return
        keys = set(schema)
        if "$ref" in schema:
            ref = schema["$ref"]
            name = ref.removeprefix("#/$defs/")
            if not ref.startswith("#/$defs/") or name not in spec.defs:
                problems.append(f"{where}: 知らない $ref {ref}")
            used.add(name)
            if keys - {"$ref", "description"}:
                problems.append(f"{where}: $ref と並べた {sorted(keys - {'$ref', 'description'})}")
            return
        if "anyOf" in schema:
            for i, sub in enumerate(schema["anyOf"]):
                walk(sub, f"{where}.anyOf[{i}]")
            if keys - {"anyOf"} - _ANNOTATIONS:
                problems.append(f"{where}: anyOf と並べた {sorted(keys - {'anyOf'} - _ANNOTATIONS)}")
            return
        if "enum" in schema or "const" in schema:
            return
        kind = schema.get("type")
        kinds = kind if isinstance(kind, list) else [kind]
        if kind is None:
            problems.append(f"{where}: 型が無い（type・enum・const・anyOf・$ref のどれか）")
            return
        if isinstance(kind, list) and not set(kinds) <= _LEAF_TYPES:
            problems.append(f"{where}: 型の並び {kinds} は葉の型（{sorted(_LEAF_TYPES)}）だけ。組み合わせは anyOf で")
        if "object" in kinds:
            props, extra = schema.get("properties"), schema.get("additionalProperties")
            if props is not None:
                if extra is not False:
                    problems.append(f"{where}: 欄の決まった object は additionalProperties: false")
                for k, sub in props.items():
                    walk(sub, f"{where}.{k}")
                missing = set(schema.get("required", [])) - set(props)
                if missing:
                    problems.append(f"{where}: required に properties に無い名前 {sorted(missing)}")
            elif isinstance(extra, dict):
                walk(extra, f"{where}.*")
                if "required" in schema:
                    problems.append(f"{where}: 名前 → 値の表に required")
            else:
                problems.append(f"{where}: object は properties か additionalProperties のスキーマで中身を書く")
        elif "array" in kinds:
            if "prefixItems" in schema:
                for i, sub in enumerate(schema["prefixItems"]):
                    walk(sub, f"{where}[{i}]")
                if schema.get("items") is not False:
                    problems.append(f"{where}: 固定の組（prefixItems）は items: false")
            elif isinstance(schema.get("items"), dict):
                walk(schema["items"], f"{where}[]")
            else:
                problems.append(f"{where}: array は items（要素のスキーマ）")
        else:
            unknown = set(kinds) - _LEAF_TYPES
            if unknown:
                problems.append(f"{where}: 知らない型 {sorted(unknown)}")

    for name, schema in spec.defs.items():
        walk(schema, f"$defs.{name}")
    for m in spec.methods.values():
        walk(m.result, f"{m.name}.result")
    # 定義どうしの参照も使われたとみなす（walk が $defs も歩いて used に入れている）
    problems += [f"$defs.{name}: どこからも使われない" for name in sorted(set(spec.defs) - used)]
    return problems


_HINTS_CACHE: tuple[float, dict] | None = None


def load_hints() -> dict:
    """使い方の落とし穴の記録（host/api/hints.json）。診断ではなく、確かめる事実と手順の文書。ファイルの更新時刻でキャッシュする。"""
    global _HINTS_CACHE
    mtime = HINTS_PATH.stat().st_mtime
    if _HINTS_CACHE is None or _HINTS_CACHE[0] != mtime:
        _HINTS_CACHE = (mtime, json.loads(HINTS_PATH.read_text(encoding="utf-8")))
    return _HINTS_CACHE[1]
