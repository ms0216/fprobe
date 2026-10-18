"""定義の DIE が型を持たず、宣言の DIE（DW_AT_specification）が型を持つ変数。

C でヘッダに `extern struct z_kernel _kernel;` と宣言し、同じ翻訳単位で定義すると、GCC は定義の DIE に
DW_AT_specification（宣言への参照）と DW_AT_location だけを付け、DW_AT_type は宣言の側に置く。Zephyr の
_kernel・z_main_thread 等がこの形で、型を宣言から引かないと void になり、ウォッチ・データ サンプリング・式の評価で
メンバーを読めなかった（実機の XIAO nRF52840 + ZMK で見つかった）。"""

from types import SimpleNamespace

from fprobe.dwarf.typesys import BaseType, TypeLoader, VoidType


class Die:
    def __init__(self, offset, tag, attributes=None, refs=None):
        self.offset = offset
        self.tag = tag
        self.attributes = {k: SimpleNamespace(value=v, form="DW_FORM_data4") for k, v in (attributes or {}).items()}
        self._refs = refs or {}
        for k in self._refs:
            self.attributes[k] = SimpleNamespace(value=0, form="DW_FORM_ref4")

    def get_DIE_from_attribute(self, name):
        return self._refs[name]

    def iter_children(self):
        return iter(())


def test_type_comes_from_the_declaration_when_the_definition_has_none():
    uint = Die(10, "DW_TAG_base_type", {"DW_AT_name": b"unsigned int", "DW_AT_byte_size": 4, "DW_AT_encoding": 7})
    declaration = Die(20, "DW_TAG_variable", {"DW_AT_name": b"counter", "DW_AT_declaration": True},
                      {"DW_AT_type": uint})
    definition = Die(30, "DW_TAG_variable", {"DW_AT_location": [3, 0, 0, 0, 32]},
                     {"DW_AT_specification": declaration})
    t = TypeLoader().type_of(definition)
    assert isinstance(t, BaseType) and t.name == "unsigned int" and t.size == 4


def test_abstract_origin_also_gives_the_type():
    uint = Die(10, "DW_TAG_base_type", {"DW_AT_name": b"int", "DW_AT_byte_size": 4, "DW_AT_encoding": 5})
    origin = Die(20, "DW_TAG_formal_parameter", {"DW_AT_name": b"x"}, {"DW_AT_type": uint})
    concrete = Die(30, "DW_TAG_formal_parameter", {}, {"DW_AT_abstract_origin": origin})
    assert TypeLoader().type_of(concrete).name == "int"


def test_no_type_anywhere_is_still_void():
    assert isinstance(TypeLoader().type_of(Die(30, "DW_TAG_variable")), VoidType)
