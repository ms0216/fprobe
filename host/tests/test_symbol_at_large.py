"""番地を含むシンボルを引くとき、決めた距離（64 KiB・1 MiB）より後ろを黙って探さないことをしない（点検 2 回目 ?:
symbols.py:307・memview.py:197）。大きいバッファ（128 KiB 等）の奥の番地も、そのシンボルとして引ける。"""

from fprobe.memview import ElfImage, ElfSymbol
from fprobe.symbols import Symbol, Symbols


def test_symbols_symbol_at_finds_a_large_object_far_from_its_start():
    syms = Symbols.__new__(Symbols)
    syms._index_all([Symbol("big", 0x2000_0000, 0x40000, "object"), Symbol("small", 0x2000_0100, 4, "object")])
    hit = syms.symbol_at(0x2003_0000)                           # big の先頭から 192 KiB
    assert hit is not None and hit[0].name == "big" and hit[1] == 0x3_0000
    assert syms.symbol_at(0x2000_0102)[0].name == "small"       # 入れ子は先頭の近いほう
    assert syms.symbol_at(0x2004_0000) is None                  # big の外


def test_elf_image_symbol_at_finds_a_large_object_far_from_its_start():
    image = ElfImage.__new__(ElfImage)
    image._index_names({"big": ElfSymbol("big", 0x2000_0000, 0x20_0000, "object"),
                        "small": ElfSymbol("small", 0x2000_0100, 4, "object")})
    assert image.symbol_at(0x2018_0000).name == "big"           # 先頭から 1.5 MiB
    assert image.symbol_at(0x2000_0101).name == "small"
    assert image.symbol_at(0x2020_0000) is None
