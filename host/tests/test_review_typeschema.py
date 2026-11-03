"""2026-11-03 のレビュー（API のスキーマ）: typeschema が同じ名前の別の dataclass を黙って同じ $defs にしない
（今の仕様では起きていない潜在の誤り。直す前は B が A の定義を参照した）。"""
import dataclasses

import pytest

from fprobe.api.typeschema import type_schema


def _make(field_type, qualname):
    @dataclasses.dataclass
    class Point:                       # 別のモジュールの同じ名前の型を模す
        x: field_type
    Point.__qualname__ = qualname
    return Point


def test_two_dataclasses_with_the_same_name_are_refused():
    A, B = _make(int, "a.Point"), _make(str, "b.Point")
    defs: dict = {}
    type_schema(A, defs)
    with pytest.raises(TypeError, match="Point"):
        type_schema(B, defs)


def test_the_same_dataclass_twice_is_one_definition():
    A = _make(int, "a.Point")
    defs: dict = {}
    assert type_schema(A, defs) == type_schema(A, defs) == {"$ref": "#/$defs/Point"}
