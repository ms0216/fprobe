"""C の式の二項演算子（条件付きブレークポイントの条件に使う）: 優先順位・C の割り算・短絡評価。"""

from pathlib import Path

import pytest
from value_kinds import venum, vint

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
    assert value(info, text) == vint(expected)                      # 比較・論理の結果は C の int


def test_base_types_missing_from_dwarf_take_their_size_from_the_elf_not_a_32_bit_guess(info, monkeypatch):
    """DWARF に無い基本型の大きさを 32 ビットの ABI と決め打ちしない。long はアドレスの大きさ（ELF の ILP32 / LP64 の
    ABI はどちらも long = ポインタ）、long double は ABI で違うので DWARF に無ければ誤り。"""
    assert value(info, "(unsigned long)-1") == vint(0xFFFF_FFFF)                    # 32 ビットの ELF
    monkeypatch.setattr(info, "address_size", 8)                                    # 64 ビットの ELF のふり
    assert value(info, "(unsigned long)-1") == vint(0xFFFF_FFFF_FFFF_FFFF)
    assert value(info, "(unsigned long long)-1") == vint(0xFFFF_FFFF_FFFF_FFFF)
    with pytest.raises(ExpressionError, match="long double"):
        evaluate("(long double)1", info)


def test_a_quoted_file_picks_one_of_statics_with_the_same_name():
    # 同じ名前の static を選ばずに誤りにし（総点検 ?9）、式では "ファイル"::名前 で選ぶ
    from dataclasses import replace

    from fprobe.dwarf.info import AmbiguousNameError
    info = DebugInfo(ELF)
    info._index()
    [mine] = info._globals["s_ticks"]
    info._globals["s_ticks"].append(replace(mine, decl_file="lib/other.c"))
    with pytest.raises(AmbiguousNameError):
        evaluate("s_ticks + 1", info)
    assert evaluate('"dwarf_demo.c"::s_ticks', info).type is not None
    assert evaluate('"dwarf_demo.c"::walk::visits', info).type is not None


def test_short_circuit_does_not_read_the_right_side(info):
    """メモリの無い評価（大域だけ）で、読む式が右にあっても、左で決まれば読まない（p && p->x の守り）。"""
    assert value(info, "0 && *(int *)0x20000000") == vint(0)
    assert value(info, "1 || *(int *)0x20000000") == vint(1)
    with pytest.raises(ExpressionError):
        value(info, "1 && *(int *)0x20000000")         # 右を読む必要がある: メモリが無いので読めない
    with pytest.raises(ExpressionError):
        value(info, "1 / 0")


def test_enumerator_names_are_constants(info):
    assert value(info, "MODE_FAULT") == venum("MODE_FAULT", -2) and value(info, "MODE_RUN == 1") == vint(1)
