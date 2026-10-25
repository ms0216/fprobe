"""API の仕様（host/api/fprobe_api.json）の読み込みと引数の検証。"""

import json
from dataclasses import dataclass
from typing import Any
from pathlib import Path

from jsonschema import Draft202012Validator

__all__ = ["SPEC_PATH", "InvalidParams", "Method", "Spec", "load_spec"]

SPEC_PATH = Path(__file__).resolve().parents[2] / "api" / "fprobe_api.json"


class InvalidParams(Exception):
    """引数が仕様に合わない。"""


@dataclass(frozen=True)
class Method:
    name: str
    kind: str            # "read" / "control"
    summary: str
    impl: str            # Probe のメソッド名（session.* は ""）
    params: dict
    result: dict
    record: bool = True  # 操作の記録に残すか（画面の表示用の頻繁な読みは残さない）
    observes: bool = False  # control だが状態を変えない読み（要求の quiet で記録から外せる）
    binary: bool = False    # 応答をバイナリのフレームで返す（fprobe.api.binary。WebSocket の利用者だけ、MCP には出さない）
    caller: bool = False    # デーモンが呼び出し元 caller = {id, name, role} を実装へ渡す（引数の仕様には出さない）
    offline: bool = False   # 機器に触らない（記録・画面の状態を読むだけ）: 機器の作業スレッドでなく読みのスレッドで実行
    irreversible: bool = False  # 取り返しのつかない操作（Flash・オプションバイトの書き込み・消去等。画面は赤く確かめる）


@dataclass(frozen=True)
class Spec:
    api_version: int
    methods: dict[str, Method]
    notifications: dict[str, dict]

    def validate_params(self, name: str, params: dict) -> None:
        method = self.methods[name]                     # 未知のメソッドは KeyError
        errors = sorted(Draft202012Validator(method.params).iter_errors(params), key=lambda e: e.path)
        if errors:
            e = errors[0]
            where = ".".join(str(p) for p in e.path) or "(引数)"
            raise InvalidParams(f"{name}: {where}: {e.message}")
        _integral(method.params, params)


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


def load_spec(path: Path | None = None) -> Spec:
    data = json.loads((path or SPEC_PATH).read_text(encoding="utf-8"))
    methods = {name: Method(name, m["kind"], m.get("summary", ""), m.get("impl", ""), m["params"], m.get("result", {}),
                            m.get("record", True), m.get("observes", False), m.get("binary", False),
                            m.get("caller", False), m.get("offline", False), m.get("irreversible", False))
               for name, m in data["methods"].items()}
    return Spec(data["api_version"], methods, data.get("notifications", {}))
