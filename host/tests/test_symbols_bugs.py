"""Symbols（シンボル表・行表）の不具合の再現（検出専用。strict xfail。台帳: docs/reports/bugs/dwarf-symbols.md）。"""

from pathlib import Path

import pytest
from minimal_elf import make_elf

from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"


def test_line_entry_uses_the_last_row_at_an_address():
    # threadx_shape.elf の行表: 0x20000074 は 277, 278, 277 の順（最後が 277）
    syms = Symbols(FIRMWARE / "threadx_shape" / "threadx_shape.elf")
    assert syms.line_entry(0x20000074).line == 277


def test_function_at_with_a_zero_size_alias_at_the_same_start(tmp_path):
    elf = make_elf([("Real", 0x1000, 0x40, "func", "global"), ("Alias", 0x1000, 0, "func", "weak")], tmp_path / "a.elf")
    syms = Symbols(elf)
    assert syms.symbol_at(0x1010)[0].name == "Real"                  # symbol_at は見つける
    assert syms.function_at(0x1010) is not None                      # function_at は見つけない
    assert syms.describe(0x1010).function == "Real"


def test_same_named_local_symbols_are_not_silently_dropped(tmp_path):
    elf = make_elf([("init", 0x2000, 8, "func", "local"), ("init", 0x3000, 8, "func", "local")], tmp_path / "b.elf")
    syms = Symbols(elf)
    assert syms.symbol_at(0x3000) is not None


def test_a_missing_line_or_symbol_says_the_facts_not_a_guess():
    """行に命令が無い・記号が無いときの文は、推測（最適化で消えたか）でなく行表・記号表の事実を言う（アクティビティの記録 2026-11-05）。"""
    import pytest
    from tests.test_dwarf_unwind import FIRMWARE
    from fprobe.symbols import Symbols
    syms = Symbols(FIRMWARE / "dwarf_demo_O0.elf")
    with pytest.raises(KeyError) as line:
        syms.resolve("dwarf_demo.c:1")
    text = str(line.value)
    assert "行表に無い" in text and "最適化" not in text and "全" in text and "行" in text
    with pytest.raises(KeyError) as sym:
        syms.resolve("systick")
    assert "記号は全" in str(sym.value) and "SysTick_Handler" in str(sym.value)    # 名前に含むものを挙げる
    with pytest.raises(KeyError) as none:
        syms.resolve("zzz_no_such")
    assert "含むものも無い" in str(none.value)


def test_a_missing_variable_says_it_is_a_function_or_lists_names_that_contain_it_and_what_was_searched():
    """debug.evaluate が変数を引けないとき: 関数の名前なら関数と言う・名前に含む大域の変数を挙げる・探した範囲（大域・static）を言う
    （Haiku の周回 3: 関数名 sum_list を『変数が無い』とだけ言われて迷った）。"""
    import pytest
    from tests.test_dwarf_unwind import FIRMWARE
    from fprobe.dwarf import DebugInfo
    info = DebugInfo(FIRMWARE / "dwarf_demo_O0.elf")
    fn = next(f.name for f in info.functions())
    with pytest.raises(KeyError) as is_fn:
        info.global_variable(fn)
    assert f"{fn} は関数の名前" in str(is_fn.value) and "大域・static" in str(is_fn.value)
    some = next(iter(info._globals))
    part = some[1:-1] if len(some) > 3 else some
    with pytest.raises(KeyError) as like:
        info.global_variable(part)
    assert some in str(like.value) or "含む大域の変数" not in str(like.value)       # 含むものがあれば挙げる
    with pytest.raises(KeyError) as none:
        info.global_variable("zzz_no_such_variable")
    assert "含む大域の変数" not in str(none.value) and "大域・static" in str(none.value)


def test_symbol_search_finds_names_by_substring_in_name_order_and_says_when_it_cut():
    """debug.symbol.search（Symbols.search）: 名前の部分一致（大文字小文字を区別しない）・種類で絞る・名前順・limit で切ったら total と returned で分かる。"""
    import pytest
    from tests.test_dwarf_unwind import FIRMWARE
    from fprobe.symbols import Symbols
    syms = Symbols(FIRMWARE / "dwarf_demo_O0.elf")
    everything = syms.search("", None, 10_000)
    assert everything["total"] == everything["returned"] == len(everything["symbols"]) > 5
    names = [x["name"] for x in everything["symbols"]]
    assert names == sorted(names)                                          # 並べ替えの基準は名前だけ
    functions = syms.search("", "function", 10_000)["symbols"]
    assert functions and all(x["kind"] == "function" for x in functions)
    hit = syms.search("SYSTICK", "function", 10)                           # 大文字小文字を区別しない
    assert [x["name"] for x in hit["symbols"]] == ["SysTick_Handler"] and hit["total"] == 1
    cut = syms.search("", None, 3)
    assert cut["returned"] == 3 and cut["total"] == everything["total"] and len(cut["symbols"]) == 3
    assert syms.search("zzz_no_such", None, 10) == {"total": 0, "returned": 0, "symbols": []}
    with pytest.raises(ValueError):
        syms.search("", "bogus", 1)


def test_the_linux_view_returns_symbol_search_with_runtime_addresses():
    """Linux の停止デバッグの symbol_search は、番地を実行時の番地（リンク時 + load bias）で返す（symbol_info と同じ）。"""
    from types import SimpleNamespace
    from tests.test_dwarf_unwind import FIRMWARE
    from fprobe.linux_halt.view_inspect import ViewInspectMixin
    from fprobe.symbols import Symbols
    syms = Symbols(FIRMWARE / "dwarf_demo_O0.elf")
    view = SimpleNamespace(d=SimpleNamespace(symbols=syms, load_bias=0x1000))
    plain = syms.search("SysTick", "function", 5)["symbols"][0]
    shifted = ViewInspectMixin.symbol_search(view, "SysTick", "function", 5)["symbols"][0]
    assert shifted["name"] == plain["name"] and shifted["address"] == plain["address"] + 0x1000
