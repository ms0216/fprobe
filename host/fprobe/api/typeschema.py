"""実装の型（dataclass・列挙）から、応答の JSON Schema（daemon.jsonable を通した後の形）を作る。

仕様（host/api/fprobe_api.json）の $defs のうち "x-from": "モジュール:クラス" を持つものは、その dataclass から
python -m fprobe.api gen が作り直す（手で書かない: 実装の型の欄を足す・変えると、仕様の形も作り直しで追う）。
入れ子の dataclass は同じ名前の $defs への参照になり、無ければ作る。

型の写し方は jsonable と同じ: dataclass → 全部の欄を持つ object（asdict）、tuple・list・集合 → 配列（集合は並べたもの）、
固定の長さの tuple → 組（prefixItems）、dict → 名前 → 値の表（鍵は文字列にする）、bytes → 16 進の文字列、列挙 → 名前、
Path → 文字列。型の分からない注記（Any 等）は誤り（形を書かない応答を作らない）。
"""

import dataclasses
import enum
import importlib
import types
import typing
from pathlib import Path

__all__ = ["dataclass_schema", "refresh_defs", "type_schema"]

_LEAF = {int: "integer", float: "number", str: "string", bool: "boolean", type(None): "null", bytes: "string",
         bytearray: "string", Path: "string"}


def _origin(tp):
    return typing.get_origin(tp)


def type_schema(tp, defs: dict) -> dict:
    """Python の型注記 tp → JSON Schema。入れ子の dataclass は defs に足す（名前 = クラス名）。"""
    if tp in _LEAF:
        return {"type": _LEAF[tp]}
    origin = _origin(tp)
    if origin in (typing.Union, types.UnionType):
        alts = [type_schema(a, defs) for a in typing.get_args(tp)]
        leaves = [a["type"] for a in alts if set(a) == {"type"} and isinstance(a["type"], str)]
        if len(leaves) == len(alts):
            return {"type": leaves}
        return {"anyOf": alts}
    if origin is typing.Literal:
        return {"enum": list(typing.get_args(tp))}
    if origin in (tuple,):
        args = typing.get_args(tp)
        if len(args) == 2 and args[1] is Ellipsis:
            return {"type": "array", "items": type_schema(args[0], defs)}
        return {"type": "array", "prefixItems": [type_schema(a, defs) for a in args], "items": False}
    if origin in (list, set, frozenset):
        (item,) = typing.get_args(tp)
        return {"type": "array", "items": type_schema(item, defs)}
    if origin is dict:
        _, value = typing.get_args(tp)
        return {"type": "object", "additionalProperties": type_schema(value, defs)}
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return {"enum": [m.name for m in tp]}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        name = tp.__name__
        known = defs.get(name)
        # 名前が同じ別の型（別のモジュールの同名の dataclass・手書きの $defs）を黙って同じ定義にしない。{} は自分を参照する
        # 型の仮置き（作っている途中の同じ型）
        if known and known.get("x-from") != f"{tp.__module__}:{tp.__qualname__}":
            raise TypeError(f"応答の型の名前 {name} が別の定義（{known.get('x-from', '手書きの $defs')}）と重なる: "
                            f"{tp.__module__}:{tp.__qualname__}")
        if name not in defs:
            defs[name] = {}                                 # 自分を参照する型のための仮置き
            defs[name] = dataclass_schema(tp, defs)
        return {"$ref": f"#/$defs/{name}"}
    raise TypeError(f"応答の型にできない注記: {tp!r}")


def dataclass_schema(cls, defs: dict) -> dict:
    """dataclass cls → 全部の欄を持つ object のスキーマ（asdict の形）。"""
    hints = typing.get_type_hints(cls)
    # metadata の api_type: __post_init__ が決めた後の型（作るときだけ None を受けて、ほかの欄から決める欄）
    props = {f.name: type_schema(f.metadata.get("api_type", hints[f.name]), defs) for f in dataclasses.fields(cls)}
    doc = (cls.__doc__ or "").strip().split("\n")[0]
    schema = {"x-from": f"{cls.__module__}:{cls.__qualname__}"}
    if doc and not doc.startswith(cls.__name__ + "("):          # dataclass が自動で付ける署名の説明は使わない
        schema["description"] = doc
    return {**schema, "type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _load(ref: str):
    module, _, name = ref.partition(":")
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def refresh_defs(defs: dict) -> dict:
    """defs の "x-from" を持つ定義を、その dataclass から作り直した新しい defs（名前の順）。"""
    out = {name: schema for name, schema in defs.items() if "x-from" not in schema}
    for name, schema in defs.items():
        if "x-from" in schema:
            cls = _load(schema["x-from"])
            if cls.__name__ != name:
                raise ValueError(f"$defs.{name} の x-from {schema['x-from']} はクラス名が違う")
            type_schema(cls, out)
    return dict(sorted(out.items()))
