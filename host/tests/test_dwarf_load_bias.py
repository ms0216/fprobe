"""DWARF の部品の読み込み位置のずれ（load bias）: PIE を 0 以外の番地に読み込んだプロセスを、リンク時の番地の ELF で読む。

題材は tests/data/linux_halt/race_arm（ARM の PIE、-O0 -g。大域変数は DW_OP_addrx、局所変数は sp 相対）。メモリとレジスタは
ImageAccess（ELF の区画を load bias だけずらして置く）。load bias 0 は今までと同じ振る舞い（既定）。"""
import struct
from pathlib import Path

import pytest

from fprobe.dwarf import DebugInfo, ImageAccess, Inspector
from fprobe.symbols import Symbols

ELF = Path(__file__).parent / "data" / "linux_halt" / "race_arm"
BIAS = 0x00400000
SP = 0xBEFFF000


def v(entry):
    """読んだ値（{"kind": "int", "value": n} の n）。"""
    return entry["value"]["value"]


@pytest.fixture(scope="module")
def elf():
    return DebugInfo(ELF), Symbols(ELF)


def stopped_in_add_one(symbols, bias, counter=41, before=41, after=42, who=2):
    """add_one の `counter = after;`（race.c:11）で止まったスレッド。フレーム 0 は add_one、呼び出し元は worker。"""
    access = ImageAccess(((s.address + bias, s.data) for s in symbols.segments))
    access.write_u32(symbols.variable("counter").address + bias, counter)
    pc = symbols.resolve("race.c:11") + bias
    access.write_memory(SP, struct.pack("<III", after, before, who))            # sp+0 after, sp+4 before, sp+8 who
    worker_sp = SP + 12
    access.write_memory(worker_sp, struct.pack("<IIII", 0, 7, who, 0))           # worker: sp+4 i, sp+8 who
    regs = {f"r{i}": 0 for i in range(13)}
    regs.update(sp=SP, lr=0x10224 + bias, pc=pc, r11=worker_sp + 16)
    access.registers = regs
    return access


def test_without_a_bias_a_pie_loaded_elsewhere_reads_the_wrong_global(elf):
    info, symbols = elf
    access = stopped_in_add_one(symbols, BIAS)
    value = Inspector(info, access, symbols=symbols).read_global("counter")["value"]
    assert value["kind"] == "unavailable"                                       # リンク時の番地は読めない（ずらしていない）


def test_globals_and_statics_are_read_at_link_address_plus_bias(elf):
    info, symbols = elf
    insp = Inspector(info, stopped_in_add_one(symbols, BIAS, counter=41), symbols=symbols, load_bias=BIAS)
    r = insp.read_global("counter")
    assert v(r) == 41 and r["address"] == symbols.variable("counter").address + BIAS
    assert v(insp.read_global("rounds")) == 1000                          # 静的変数（ELF の初期値）
    pointer = insp.read("&counter")["value"]
    assert pointer["address"] == symbols.variable("counter").address + BIAS and pointer["symbol"] == "counter"


def test_frame_0_function_line_arguments_and_locals_are_found_from_pc_minus_bias(elf):
    info, symbols = elf
    insp = Inspector(info, stopped_in_add_one(symbols, BIAS), symbols=symbols, load_bias=BIAS)
    bt = insp.backtrace(variables=True)
    top = bt[0]
    assert (top["function"], top["file"], top["line"]) == ("add_one", "race.c", 11)
    assert top["pc"] == symbols.resolve("race.c:11") + BIAS                     # 番地は実行時のまま
    values = {x["name"]: v(x) for x in top["arguments"] + top["locals"]}
    assert values == {"who": 2, "before": 41, "after": 42}
    assert v(insp.read("before + after")) == 83


def test_the_cfi_is_looked_up_at_pc_minus_bias_and_the_caller_is_found(elf):
    info, symbols = elf
    insp = Inspector(info, stopped_in_add_one(symbols, BIAS), symbols=symbols, load_bias=BIAS)
    bt = insp.backtrace(variables=True)
    assert [f["function"] for f in bt[:2]] == ["add_one", "worker"]
    assert bt[1]["pc"] == 0x10224 + BIAS and bt[1]["line"] == 19             # race.c:19 add_one(who)
    assert {x["name"]: v(x) for x in bt[1]["locals"]}["i"] == 7


def test_a_zero_bias_is_the_behaviour_of_before(elf):
    info, symbols = elf
    insp = Inspector(info, stopped_in_add_one(symbols, 0, counter=5), symbols=symbols)
    assert v(insp.read_global("counter")) == 5
    assert insp.backtrace()[0]["line"] == 11
