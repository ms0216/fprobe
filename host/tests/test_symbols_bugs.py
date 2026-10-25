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
