"""fprobe.halt_risk: 止める前の手がかり（ELF のシンボルだけ）。本体は、使う側が明示して渡した名前・正規表現に合う
シンボルが ELF にあるか、の事実だけを返す。名前の慣習（接頭辞 ll_・名前に wdt を含む 等）で種類を決める表を本体に
持たない（CLAUDE.md。慣習の表はプラグイン fprobe.plugins.heuristics）。"""

from pathlib import Path

import pytest

from fprobe import halt_risk
from fprobe.halt_risk import scan
from fprobe.symbols import Symbol, Symbols

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


def f(name, address=0x1000):
    return Symbol(name, address, 4, "function")


def v(name, address=0x20000000):
    return Symbol(name, address, 4, "object")


def test_without_names_nothing_is_matched_even_for_conventional_names():
    # 既定の名前の表を持たない: 渡されなければ、無線・USB・ウォッチドッグ・RTOS らしい名前があっても何も言わない
    r = scan([f("RADIO_IRQHandler"), f("ll_adv_enable"), f("tud_task"), f("wdt_feed"), v("pxCurrentTCB")])
    assert r == {"names": [], "patterns": [], "matched": False, "matches": [], "monitor_in_elf": False}


def test_no_kind_table_in_the_core():
    assert not hasattr(halt_risk, "KINDS") and not hasattr(halt_risk, "RTOS_GLOBALS")


def test_given_names_are_matched_exactly_with_the_symbols_as_facts():
    r = scan([f("main"), f("RADIO_IRQHandler", 0x1234), v("_kernel", 0x20000010), f("radio_init")],
             names=["RADIO_IRQHandler", "_kernel", "absent"])
    assert r["matched"] is True and r["names"] == ["RADIO_IRQHandler", "_kernel", "absent"]
    assert r["matches"] == [
        {"query": "RADIO_IRQHandler", "by": "name", "count": 1,
         "symbols": [{"name": "RADIO_IRQHandler", "kind": "function", "address": 0x1234}]},
        {"query": "_kernel", "by": "name", "count": 1,
         "symbols": [{"name": "_kernel", "kind": "object", "address": 0x20000010}]},
    ]


def test_given_patterns_return_every_matching_symbol_without_cutting():
    syms = [f(f"ll_func_{i:02}", 0x1000 + i) for i in range(40)] + [f("all_done")]
    r = scan(syms, patterns=[r"^ll_"])
    (m,) = r["matches"]
    assert m["query"] == "^ll_" and m["by"] == "pattern" and m["count"] == 40 and len(m["symbols"]) == 40
    assert [s["name"] for s in m["symbols"]] == sorted(s.name for s in syms[:40])


def test_a_bad_pattern_is_an_error():
    with pytest.raises(ValueError):
        scan([f("main")], patterns=["("])


def test_monitor_in_elf_is_reported():
    assert scan([f("fprobe_monitor_init")])["monitor_in_elf"] is True


def test_demo_elf_with_no_names_matches_nothing():
    assert scan(Symbols(DEMO).names())["matched"] is False
