"""ELF のメモリ使用量（fprobe.elfsize、U26 の Build Analyzer 相当）: 区画・領域・シンボルの大きさ、スタックの範囲。
stack_demo.elf（Flash + SRAM、.data は Flash に写し）を使う。"""

from pathlib import Path

import pytest

from fprobe.elfsize import (ElfLayout, MemoryRegion, parse_linker_regions, parse_map_regions, size_report,
                            stack_bounds)

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "stack_demo"
ELF = DEMO / "stack_demo.elf"
FLASH = MemoryRegion("FLASH", 0x0800_0000, 512 * 1024, "rx")
RAM = MemoryRegion("RAM", 0x2000_0000, 96 * 1024, "rwx")

GNU_MAP = """\
Archive member included to satisfy reference by file (symbol)

Memory Configuration

Name             Origin             Length             Attributes
FLASH            0x0000000008000000 0x0000000000080000 xr
RAM              0x0000000020000000 0x0000000000018000 xrw
*default*        0x0000000000000000 0xffffffffffffffff

Linker script and memory map
"""


@pytest.fixture(scope="module")
def layout():
    return ElfLayout.from_elf(ELF)


def test_sections_have_vma_lma_and_kind(layout):
    by = {s.name: s for s in layout.sections}
    assert set(by) >= {".isr_vector", ".text", ".rodata", ".data", ".bss", "._user_heap_stack"}
    assert ".debug_info" not in by                                  # 読み込まない区画は除く
    data = by[".data"]
    assert data.address == 0x2000_0000 and data.load_address == 0x0800_027C and data.size == 64 and data.has_content
    assert not by[".bss"].has_content and by[".bss"].load_address == by[".bss"].address
    assert by[".text"].executable and not by[".text"].writable and by[".data"].writable


def test_symbols_include_absolute_linker_symbols(layout):
    assert layout.absolute["_estack"] == 0x2001_8000 and layout.absolute["_Min_Stack_Size"] == 0x400
    big = layout.symbol("big_buffer")
    assert big.size == 1024 and big.kind == "object" and big.section == ".bss"
    assert layout.symbol("middle").kind == "function" and layout.symbol("middle").address % 2 == 0


def test_regions_from_the_linker_script_and_the_map():
    assert parse_linker_regions((DEMO / "link.ld").read_text(encoding="utf-8")) == [FLASH, RAM]
    assert parse_map_regions(GNU_MAP) == [MemoryRegion("FLASH", 0x0800_0000, 0x80000, "xr"),
                                          MemoryRegion("RAM", 0x2000_0000, 0x18000, "xrw")]
    text = "MEMORY {\n  /* c */ rom (rx) : org = 0x0, len = 0x10000 - 1K\n  ram : ORIGIN = 0x1000, LENGTH = 2M\n}"
    assert parse_linker_regions(text) == [MemoryRegion("rom", 0, 0x10000 - 1024, "rx"),
                                          MemoryRegion("ram", 0x1000, 2 * 1024 * 1024, "")]


def test_region_usage_counts_the_load_copy_of_data_in_flash(layout):
    report = size_report(layout, [FLASH, RAM], top=3)
    regions = {r["name"]: r for r in report["regions"]}
    flash, ram = regions["FLASH"], regions["RAM"]
    assert flash["used"] == 64 + 500 + 72 + 64                      # ベクタ表・コード・定数・.data の写し
    assert {(c["section"], c["as"]) for c in flash["sections"]} >= {(".data", "load"), (".text", "run")}
    assert ram["used"] == 64 + 1028 + 1540
    assert ram["free"] == 96 * 1024 - ram["used"] and 0 < ram["fraction"] < 0.03
    assert report["outside"] == []
    assert [s["name"] for s in report["symbols"]][:2] == ["big_buffer", "Reset_Handler"]    # 大きい順
    assert report["symbols"][0] == {"name": "big_buffer", "address": 0x2000_0044, "size": 1024, "kind": "object",
                                    "section": ".bss", "region": "RAM"}
    assert len(report["symbols"]) == 3 and report["symbols_total"] > 10


def test_symbols_can_be_filtered(layout):
    report = size_report(layout, [FLASH, RAM], top=100, region="FLASH", kind="function", name="*e*")
    names = {s["name"] for s in report["symbols"]}
    assert "middle" in names and "leaf" in names and "big_buffer" not in names and "tail" not in names
    by_section = {s["name"]: s for s in size_report(layout, [], top=0)["sections"]}
    assert by_section[".text"]["size"] == 500


def test_without_regions_sections_are_outside_and_noted(layout):
    report = size_report(layout, [])
    assert report["regions"] == [] and {s["section"] for s in report["outside"]} >= {".text", ".bss"}
    assert any("領域" in n for n in report["notes"])


def test_stack_bounds_from_the_linker_symbols(layout):
    b = stack_bounds(layout, [FLASH, RAM])
    assert b.top == 0x2001_8000 and b.nominal_size == 0x400
    assert b.bottom == 0x2001_8000 - 0x400 and b.top_source == "_estack"
    free = stack_bounds(layout, [FLASH, RAM], nominal=False)
    assert free.bottom == 0x2000_0A48                                # 最後の RAM の区画（._user_heap_stack）の終わり


def test_stack_top_falls_back_to_the_vector_table(tmp_path):
    sram = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
    b = stack_bounds(ElfLayout.from_elf(sram), [MemoryRegion("RAM", 0x2000_0000, 32 * 1024)])
    assert b.top == 0x2000_8000 and b.top_source in ("__stack_top", "vector_table")
    assert b.nominal_size is None and b.bottom % 8 == 0 and b.bottom < b.top
