"""API の仕様（host/api/fprobe_api.json）の読み込みと引数の検証。"""

import json
from dataclasses import dataclass
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


def load_spec(path: Path | None = None) -> Spec:
    data = json.loads((path or SPEC_PATH).read_text(encoding="utf-8"))
    methods = {name: Method(name, m["kind"], m.get("summary", ""), m.get("impl", ""), m["params"], m.get("result", {}),
                            m.get("record", True), m.get("observes", False), m.get("binary", False),
                            m.get("caller", False))
               for name, m in data["methods"].items()}
    return Spec(data["api_version"], methods, data.get("notifications", {}))
