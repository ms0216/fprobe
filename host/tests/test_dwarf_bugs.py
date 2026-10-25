"""DWARF・C の式・値の整形の不具合の再現（検出専用。strict xfail。台帳: docs/reports/bugs/dwarf-symbols.md）。

直したら xfail が XPASS（strict なので失敗）になるので、マーカーを外して通常の試験にする。
"""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.dwarf.structs import DWARFStructs

from fprobe.dwarf import DebugInfo, ExpressionError
from fprobe.dwarf.access import ImageAccess, register_number
from fprobe.dwarf.cexpr import evaluate
from fprobe.dwarf.info import _const_bytes
from fprobe.dwarf.location import EvalContext, MemoryPlace, evaluate_location, read_place
from fprobe.dwarf.typesys import (ArrayType, BaseType, PointerType, TypedefType, TypeLoader)
from fprobe.dwarf.unwind import RegisterFile
from fprobe.dwarf.values import DecodeContext, decode, encode

ELF = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"
PARSER = DWARFExprParser(DWARFStructs(little_endian=True, dwarf_format=32, address_size=4))


@pytest.fixture(scope="module")
def info():
    return DebugInfo(ELF)


def number(info, text):
    v = evaluate(text, info)
    return decode(v.type, v.place.data)["value"]


def test_leading_zero_literal_is_octal(info):
    assert number(info, "010") == 8


@pytest.mark.parametrize("text, expected", [("0u - 1", 0xFFFFFFFF), ("-1 < 0u", 0), ("~0u", 0xFFFFFFFF),
                                            ("(unsigned)0 - 1", 0xFFFFFFFF)])
def test_unsigned_arithmetic_wraps_like_c(info, text, expected):
    assert number(info, text) == expected


def test_negative_shift_count_is_an_expression_error(info):
    with pytest.raises(ExpressionError):
        evaluate("1 << -1", info)


def test_float_cast_of_too_large_value_is_inf(info):
    assert number(info, "(float)1e40") == "inf"


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-05: 4・8 以外の大きさの浮動小数 encode が 8 バイトを返す")
@pytest.mark.parametrize("size", [2, 12, 16])
def test_encode_float_of_other_size_has_that_size_or_refuses(size):
    try:
        data = encode(BaseType("x", size, "float"), 1.5)
    except TypeError:
        return
    assert len(data) == size


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-06: float への encode が範囲外で OverflowError、decode が返す \"nan\"/\"inf\" を encode できない")
def test_encode_float_round_trips_decode_output():
    f = BaseType("float", 4, "float")
    data = encode(f, "inf")
    assert decode(f, data) == {"kind": "float", "value": "inf"}


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-06b: float の範囲外の値が OverflowError で漏れる（TypeError/ValueError でない）")
def test_encode_out_of_range_float_is_a_value_error():
    with pytest.raises((TypeError, ValueError)):
        encode(BaseType("float", 4, "float"), 1e40)


class _Attr:
    def __init__(self, value, form="DW_FORM_block1"):
        self.value, self.form = value, form


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-07: DW_AT_data_member_location が式（DWARF 2/3 の block）だと静かに 0 にする")
def test_member_location_given_as_plus_uconst_expression():
    die = NS(tag="DW_TAG_member", offset=1,
             attributes={"DW_AT_data_member_location": _Attr([0x23, 8]), "DW_AT_name": _Attr(b"m", "DW_FORM_string")})
    loader = TypeLoader()
    loader.type_of = lambda d: BaseType("int", 4, "signed")
    assert loader._member(die).offset == 8


def _ctx():
    return EvalContext(lambda a, n: bytes(n), lambda n: 0)


def test_64_bit_stack_value_is_kept():
    ops = PARSER.parse_expr(bytes([0x10, 0x89, 0xCF, 0x95, 0x9A, 0x12, 0x9F]))      # constu 0x123456789; stack_value
    place = evaluate_location(ops, _ctx())
    assert int.from_bytes(read_place(place, 8, _ctx()), "little") == 0x123456789


def test_64_bit_signed_stack_value_is_sign_extended():
    ops = PARSER.parse_expr(bytes([0x0F] + [0xFF] * 8 + [0x9F]))
    place = evaluate_location(ops, _ctx())
    assert int.from_bytes(read_place(place, 8, _ctx()), "little") == 0xFFFFFFFFFFFFFFFF


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-09: DW_AT_const_value が data1 で値が型の符号の範囲外（255 の int8）だと OverflowError")
def test_const_value_data1_for_signed_type_is_reinterpreted():
    assert _const_bytes(255, BaseType("signed char", 1, "signed_char"), "DW_FORM_data1") == b"\xff"


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-10: DWARF に無い char を常に signed で補う（ARM の char は unsigned）")
def test_synthetic_char_is_unsigned_on_arm(info):
    info._index()
    saved = info._type_dies.pop("char")
    try:
        v = evaluate("(char)200", info)
        assert decode(v.type, v.place.data)["value"] == 200
    finally:
        info._type_dies["char"] = saved


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-11: uint8_t * は文字列として読むのに uint8_t[] は文字列にしない（is_char の扱いが経路で違う）")
def test_uint8_pointer_and_array_are_treated_alike():
    u8 = TypedefType("uint8_t", BaseType("unsigned char", 1, "unsigned_char"))
    memory = b"hello\0"
    ctx = DecodeContext(read_memory=lambda a, n: memory[a:a + n])
    array = decode(ArrayType(u8, 6), memory, ctx)
    pointer = decode(PointerType(u8), (0).to_bytes(4, "little"), ctx)
    pointer = decode(PointerType(u8), (1).to_bytes(4, "little"), DecodeContext(read_memory=lambda a, n: memory[:n]))
    assert ("string" in array) == ("string" in pointer)


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-12: 場所の番地を 32 ビットに切る（64 ビット ELF で 0x1_0000_0000 が 0 番地に）")
def test_memory_place_beyond_32_bits_is_not_wrapped():
    seen = []
    ctx = EvalContext(lambda a, n: seen.append(a) or bytes(n), lambda n: 0)
    read_place(MemoryPlace(0x1_0000_0000), 4, ctx)
    assert seen == [0x1_0000_0000]


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-13: 表に無いレジスタ番号で register_name の KeyError が RegisterFile.try_read を抜ける（巻き戻しが落ちる）")
def test_unknown_register_number_is_not_available_not_a_crash():
    assert RegisterFile(access=ImageAccess()).try_read(143) is None      # 143 = Armv8.1-M PAC の RA_AUTH_CODE


@pytest.mark.xfail(strict=True, reason="BUG-DWARF-13b: register_number('') が IndexError（KeyError でない）")
def test_register_number_of_empty_name_is_key_error():
    with pytest.raises(KeyError):
        register_number("")
