"""fprobe.dwarf.values: どの値も kind（DWARF の型で決まる値の種類）を持つ。使う側がキーの有無・型の名前で推さずに済む形。"""

import math
import struct

from value_kinds import ints, varray, vbool, vbytes, venum, vfloat, vint, vna, vptr, vstruct

from fprobe.dwarf.typesys import (ArrayType, BaseType, EnumType, Member, PointerType, QualifiedType, StructType,
                                  TypedefType, VoidType)
from fprobe.dwarf.values import SCALAR_KINDS, DecodeContext, decode, decode_bits, scalar, unavailable

INT = BaseType("int", 4, "signed")
U8 = BaseType("unsigned char", 1, "unsigned_char")
FLOAT = BaseType("float", 4, "float")
BOOL = BaseType("_Bool", 1, "bool")
MODE = EnumType("Mode", 4, True, (("MODE_IDLE", 0), ("MODE_RUN", 1)))


def test_a_struct_whose_members_are_named_like_other_kinds_is_still_a_struct():
    # 前の形（{メンバー: 値}）では、メンバー名 enum・value は列挙に、pointer・to はポインタに、length・head は切った
    # 配列に、string・values は文字の配列に、unavailable は読めない値に見えた
    names = ["enum", "value", "pointer", "to", "length", "head", "string", "values", "unavailable", "kind"]
    s = StructType("struct", "Tricky", 4 * len(names), [Member(n, INT, 4 * i) for i, n in enumerate(names)])
    data = b"".join(struct.pack("<i", i) for i in range(len(names)))
    assert decode(s, data) == vstruct(**{n: vint(i) for i, n in enumerate(names)})


def test_a_typedef_of_a_float_is_a_float_even_when_the_number_is_whole():
    # float32_t 等の名前で浮動小数を見分けない: 種類は typedef の先の基本型（DWARF の encoding）で決まる
    float32_t = TypedefType("float32_t", QualifiedType("volatile", TypedefType("my_float", FLOAT)))
    assert decode(float32_t, struct.pack("<f", 3.0)) == vfloat(3.0)
    assert decode(float32_t, struct.pack("<f", math.inf)) == vfloat("inf")
    assert decode(float32_t, struct.pack("<f", math.nan)) == vfloat("nan")
    assert decode(TypedefType("int32_t", INT), struct.pack("<i", 3)) == vint(3)


def test_an_array_says_its_length_and_how_many_items_it_carries():
    a = ArrayType(INT, 5)
    data = b"".join(struct.pack("<i", i * 10) for i in range(5))
    assert decode(a, data) == varray(ints(0, 10, 20, 30, 40))
    cut = decode(a, data, DecodeContext(max_elements=2))
    assert cut == varray(ints(0, 10), length=5) and len(cut["items"]) < cut["length"]       # 切った
    text = decode(ArrayType(U8, 4), b"ab\0z", DecodeContext(max_elements=1))
    assert text == varray(ints(97), length=4, string="ab")                                  # 文字列は全体から


def test_bitfields_are_int_enum_or_bool_like_whole_values():
    data = (0b1_01_110).to_bytes(1, "little")                        # bool 1 ビット・enum 2 ビット・int 3 ビット
    assert decode_bits(INT, data, 0, 3) == vint(-2)                  # 符号付きは符号拡張（0b110）
    assert decode_bits(MODE, data, 3, 2) == venum("MODE_RUN", 1)
    assert decode_bits(BOOL, data, 5, 1) == vbool(True)
    assert decode_bits(MODE, (3 << 3).to_bytes(1, "little"), 3, 2) == venum(None, -1)       # 列挙子に無い値（符号付き）
    s = StructType("struct", "Bits", 1, [Member("flag", BOOL, 0, 1, 5), Member("mode", MODE, 0, 2, 3)])
    assert decode(s, data) == vstruct(flag=vbool(True), mode=venum("MODE_RUN", 1))


def test_every_kind_of_value_and_the_number_of_the_scalar_kinds():
    assert decode(BOOL, b"\x01") == vbool(True)
    assert decode(BaseType("odd", 2, "unknown"), b"\xff\x7f") == vbytes("ff7f")
    assert decode(BaseType("half", 2, "float"), b"\x00\x3c") == vbytes("003c")             # 4 / 8 以外の浮動小数
    assert decode(VoidType(), b"\x01\x02") == vbytes("0102")
    assert decode(PointerType(INT), (0x2000_0000).to_bytes(4, "little")) == vptr(0x2000_0000, "int")
    assert decode(StructType("struct", "Opaque", None), b"") == vna("不完全型 struct Opaque")
    assert unavailable("optimized out") == vna("optimized out")
    assert SCALAR_KINDS == ("int", "float", "bool", "enum")
    assert [scalar(v) for v in (vint(3), vfloat(2.5), vbool(False), venum("A", 1))] == [3, 2.5, False, 1]
    assert scalar(vstruct(enum=venum("A", 1), value=vint(1))) is None and scalar(varray(ints(1))) is None
