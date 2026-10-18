"""fprobe.profile.coverage: PC サンプルによるカバレッジ（近似）。sram_demo.elf と dwarf_demo の ELF。"""

import json
from pathlib import Path

import pytest

from fprobe.disasm import Disassembler
from fprobe.profile import PcSamples, Profiler
from fprobe.profile.coverage import coverage, elf_functions

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
SRAM_DEMO = FIRMWARE / "sram_demo" / "sram_demo.elf"
DWARF_O1 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O1.elf"
SOURCE = (SRAM_DEMO.parent / "sram_demo.c").read_text(encoding="utf-8").splitlines()


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


@pytest.fixture(scope="module")
def sram():
    return Disassembler.from_elf(SRAM_DEMO)


def code(dis, name):
    return [i for i in dis.function(name).instructions if i.kind == "code"]


def test_elf_functions():
    assert elf_functions(SRAM_DEMO) == ["reached_milestone", "tick", "reset_handler", "default_handler"]


def test_full_and_partial_functions(sram):
    tick = [i.address for i in code(sram, "tick")]
    milestone = code(sram, "reached_milestone")[0].address
    prof = Profiler(sram.symbols, sram).profile(PcSamples.from_pcs(tick * 3 + [milestone]))
    cov = coverage(prof, sram, all_functions=True)
    assert [f.function for f in cov.functions] == ["reached_milestone", "tick", "reset_handler", "default_handler"]
    t = cov.function("tick")
    assert t.instructions == t.instructions_hit == len(tick)
    assert t.line_fraction == 1.0 and t.instruction_fraction == 1.0
    first = line_of("void tick(void)")                     # 関数の行・counter++・if・呼び出し・閉じ括弧
    assert {line for _, line in t.lines} == {first, first + 1, first + 2, first + 3, first + 5}
    m = cov.function("reached_milestone")
    assert m.instructions_hit == 1 and 0 < m.line_fraction < 1
    assert m.hit_lines == ((("sram_demo.c", line_of("void reached_milestone"))),)
    d = cov.function("default_handler")
    assert d.instructions_hit == 0 and d.line_fraction == 0.0
    out = cov.as_dict()
    json.dumps(out)
    assert out["approximate"] is True and "近似" in out["notes"][0]
    assert out["lines_hit"] == sum(len(f.hit_lines) for f in cov.functions)
    dm = next(f for f in out["functions"] if f["function"] == "default_handler")
    assert dm["missed_lines"] == [list(x) for x in cov.function("default_handler").lines]


def test_default_is_functions_with_hits(sram):
    tick = code(sram, "tick")[0].address
    cov = coverage(PcSamples.from_pcs([tick, 0x08000000]), sram)
    assert [f.function for f in cov.functions] == ["tick"]
    assert cov.samples == 2


def test_sleep_samples_count_as_executed(sram):
    # 眠りとして分けたサンプルも、その命令が実行された証拠（Profile.hit_addresses に含む）
    prof = Profiler(sram.symbols, sram).profile(PcSamples.from_pcs([code(sram, "tick")[0].address]))
    prof.sleep_pcs[code(sram, "tick")[1].address] += 1
    cov = coverage(prof, sram, functions=["tick"])
    assert cov.function("tick").instructions_hit == 2


def test_dwarf_demo_lines():
    dis = Disassembler.from_elf(DWARF_O1)
    leaf = code(dis, "leaf_sum")
    cov = coverage([leaf[0].address, leaf[-1].address], dis, functions=["leaf_sum", "walk"])
    f = cov.function("leaf_sum")
    assert f.instructions == len(leaf) and f.instructions_hit == 2
    assert set(f.hit_lines) == {(leaf[0].file, leaf[0].line), (leaf[-1].file, leaf[-1].line)}
    assert set(f.hit_lines) <= set(f.lines)
    assert cov.function("walk").instructions_hit == 0


def test_requires_symbols():
    from fprobe.memview import BytesMemory
    with pytest.raises(ValueError):
        coverage([0x1000], Disassembler(BytesMemory(0x1000, b"\x00\xbf")))
