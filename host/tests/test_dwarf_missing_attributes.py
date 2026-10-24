"""DWARF に無い値を暗黙の値（unsigned・4 バイト）で埋めない（点検 3 回目の ?3）。偽の DIE で確かめる。"""

from itertools import count
from types import SimpleNamespace

import pytest
from value_kinds import vbytes, venum

from fprobe.dwarf.info import _const_bytes
from fprobe.dwarf.typesys import BaseType, EnumType, StructType, TypeLoader
from fprobe.dwarf.values import decode, decode_member, encode

_offsets = count(0x100)


class Die:
    def __init__(self, tag, children=(), **attrs):
        self.tag, self.offset, self._children = tag, next(_offsets), list(children)
        self.attributes = {}
        for k, v in attrs.items():
            form = None
            if isinstance(v, tuple):
                v, form = v
            self.attributes[f"DW_AT_{k}"] = SimpleNamespace(value=v, form=form)
        self.refs = {}

    def iter_children(self):
        return iter(self._children)

    def get_DIE_from_attribute(self, name):
        return self.refs[name]


def with_type(die, target):
    die.attributes["DW_AT_type"] = SimpleNamespace(value=target.offset, form="DW_FORM_ref4")
    die.refs["DW_AT_type"] = target
    return die


def test_a_base_type_without_a_known_encoding_is_not_read_as_unsigned():
    loader = TypeLoader()
    missing = loader.load(Die("DW_TAG_base_type", name=b"odd", byte_size=2))
    complex_float = loader.load(Die("DW_TAG_base_type", name=b"_Complex", byte_size=8, encoding=0x3))
    assert isinstance(missing, BaseType) and missing.encoding == "unknown" and complex_float.encoding == "unknown"
    assert decode(missing, b"\xff\x7f") == vbytes("ff7f")              # 生のバイト（符号を決めない）
    with pytest.raises(TypeError):
        encode(missing, 1)
    known = loader.load(Die("DW_TAG_base_type", name=b"int", byte_size=4, encoding=0x5))
    assert known.encoding == "signed"


def test_an_enum_without_a_size_is_not_given_four_bytes():
    loader = TypeLoader()
    enum = loader.load(Die("DW_TAG_enumeration_type", [Die("DW_TAG_enumerator", name=b"A", const_value=1)], name=b"e"))
    assert isinstance(enum, EnumType) and enum.size is None
    assert decode(enum, b"\x01\x00\x00\x00")["kind"] == "unavailable"
    with pytest.raises(TypeError):
        encode(enum, "A")
    sized = loader.load(Die("DW_TAG_enumeration_type", [Die("DW_TAG_enumerator", name=b"A", const_value=1)],
                            name=b"e2", byte_size=1))
    assert sized.size == 1 and decode(sized, b"\x01") == venum("A", 1)


def test_a_dwarf3_bitfield_without_a_storage_size_has_no_guessed_position():
    loader = TypeLoader()
    incomplete = Die("DW_TAG_structure_type", name=b"opaque", declaration=True)
    member = with_type(Die("DW_TAG_member", name=b"f", bit_size=3, bit_offset=5, data_member_location=0), incomplete)
    s = loader.load(Die("DW_TAG_structure_type", [member], name=b"s", byte_size=4))
    assert isinstance(s, StructType) and s.members[0].bit_size == 3 and s.members[0].bit_offset is None
    assert decode_member(s.members[0], b"\xff" * 4, None)["kind"] == "unavailable"


def test_a_constant_without_a_typed_size_takes_the_form_size_or_the_least_bytes():
    unsized = SimpleNamespace(size=None)
    assert _const_bytes(5, unsized, "DW_FORM_data2") == b"\x05\x00"         # 形式の大きさ（事実）
    assert _const_bytes(300, unsized, "DW_FORM_udata") == b"\x2c\x01"      # 形式に大きさが無い: 値の入る最小
    assert _const_bytes(-1, unsized, "DW_FORM_sdata") == b"\xff"
    assert _const_bytes(-1, BaseType("int", 4, "signed"), "DW_FORM_sdata") == b"\xff\xff\xff\xff"
