"""C の式の二項演算子（条件付きブレークポイントの条件に使う）: 優先順位・C の割り算・短絡評価。"""

from pathlib import Path

import pytest

from fprobe.dwarf import DebugInfo, ExpressionError
from fprobe.dwarf.cexpr import evaluate
from fprobe.dwarf.values import decode

ELF = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"


@pytest.fixture(scope="module")
def info():
    return DebugInfo(ELF)


def value(info, text):
    v = evaluate(text, info)
    return decode(v.type, v.place.data)


@pytest.mark.parametrize("text, expected", [
    ("1 + 2 * 3", 7),
    ("(1 + 2) * 3", 9),
    ("1 + 2 * 3 == 7", 1),
    ("10 - 4 - 3", 3),                                  # 左結合
    ("7 / -2", -3), ("-7 % 3", -1), ("7 % -3", 1),      # C の割り算は 0 の方へ切り捨て
    ("1 << 4 | 1", 17), ("0xF0 & 0x3C ^ 0x01", 0x31),   # & は ^ より強い
    ("3 > 2 && 2 >= 2", 1), ("3 < 2 || 2 != 2", 0), ("5 <= 4", 0),
    ("!0 + 1", 2), ("-3 < 0", 1),
    ("1.5 * 2 > 2.9", 1),
])
def test_binary_operators_follow_c(info, text, expected):
    assert value(info, text) == expected


def test_short_circuit_does_not_read_the_right_side(info):
    """メモリの無い評価（大域だけ）で、読む式が右にあっても、左で決まれば読まない（p && p->x の守り）。"""
    assert value(info, "0 && *(int *)0x20000000") == 0
    assert value(info, "1 || *(int *)0x20000000") == 1
    with pytest.raises(ExpressionError):
        value(info, "1 && *(int *)0x20000000")         # 右を読む必要がある: メモリが無いので読めない
    with pytest.raises(ExpressionError):
        value(info, "1 / 0")


def test_enumerator_names_are_constants(info):
    assert value(info, "MODE_FAULT")["value"] == -2 and value(info, "MODE_RUN == 1") == 1
