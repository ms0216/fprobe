"""fprobe.disasm: Thumb / Thumb-2 の逆アセンブル（試験用ファームウェアの ELF で）。

sram_demo.elf（Cortex-M4、movw/movt で定数を作る）と disasm_demo.elf（Cortex-M0+、リテラルプールあり）。
期待値の番地は `arm-none-eabi-objdump` 相当の capstone の出力を手で確かめたもの（ELF を作り直したら見直す）。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from fprobe.disasm import Disassembler
from fprobe.memview import BytesMemory, CodeMap, ElfImage
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
SRAM_DEMO = FIRMWARE / "sram_demo" / "sram_demo.elf"
DISASM_DEMO = FIRMWARE / "disasm_demo" / "disasm_demo.elf"
SOURCE = (SRAM_DEMO.parent / "sram_demo.c").read_text(encoding="utf-8").splitlines()


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


@pytest.fixture(scope="module")
def sram():
    return Disassembler.from_elf(SRAM_DEMO)


@pytest.fixture(scope="module")
def demo():
    return Disassembler.from_elf(DISASM_DEMO)


# --- sram_demo（Thumb-2） ---------------------------------------------------------------

def test_function_listing_of_tick(sram):
    listing = sram.function("tick")
    first, *_, last = listing.instructions
    assert listing.sync == "function"
    assert (first.address, first.data, first.mnemonic, first.operands) == (0x20000050, bytes.fromhex("80b5"),
                                                                           "push", "{r7, lr}")
    assert [i.address for i in listing.instructions][:4] == [0x20000050, 0x20000052, 0x20000054, 0x20000058]
    assert len(listing.instructions) == 14 and all(i.kind == "code" for i in listing.instructions)
    # 末尾呼び出し: b.w reached_milestone
    assert (last.address, last.mnemonic, last.target, last.target_name) == (0x20000070, "b.w", 0x20000040,
                                                                            "reached_milestone")
    assert last.flow == "jump" and not last.conditional
    popne = next(i for i in listing.instructions if i.mnemonic == "popne")
    assert popne.flow == "return" and popne.conditional


def test_calls_and_conditional_branches_are_named(sram):
    listing = sram.function("reset_handler")
    call = next(i for i in listing.instructions if i.mnemonic == "bl")
    assert (call.target, call.target_name, call.flow) == (0x20000050, "tick", "call")
    bhs = next(i for i in listing.instructions if i.mnemonic == "bhs")
    assert bhs.flow == "jump" and bhs.conditional and bhs.target_name == "reset_handler+60"


def test_instructions_carry_source_lines(sram):
    listing = sram.function("tick")
    first = listing.instructions[0]
    assert (first.function, first.offset, first.file) == ("tick", 0, "sram_demo.c")
    assert first.new_line
    lines = {i.line for i in listing.instructions}
    assert line_of("counter++;") in lines and line_of("reached_milestone(counter);") in lines
    # 行が変わる所だけ new_line
    for previous, current in zip(listing.instructions, listing.instructions[1:]):
        assert current.new_line == ((current.file, current.line) != (previous.file, previous.line))


def test_mixed_text_inserts_source_lines(sram):
    text = sram.function("tick").text(source=lambda file, line: SOURCE[line - 1])
    lines = text.splitlines()
    assert lines[0] == "tick:"
    assert any("counter++;" in line and "sram_demo.c:" in line for line in lines)
    assert any(line.startswith("20000070: fff7e6bf") and "b.w" in line and "<reached_milestone>" in line
               for line in lines)


def test_as_dict_is_plain_data(sram):
    d = sram.function("tick").as_dict()
    assert d["sync"] == "function"
    first = d["instructions"][0]
    assert first["address"] == 0x20000050 and first["bytes"] == "80b5" and first["mnemonic"] == "push"


def test_around_pc_aligns_from_the_function_start(sram):
    pc = 0x20000068                                                        # tick の popne（途中の命令）
    listing = sram.around(pc, before=3, after=2)
    assert listing.sync == "function"
    assert [i.address for i in listing.instructions] == [0x20000062, 0x20000064, 0x20000066,
                                                         0x20000068, 0x2000006A, 0x2000006C]
    assert listing.pc_index == 3


def test_around_pc_without_symbols_starts_at_pc():
    image = ElfImage(SRAM_DEMO)
    bare = Disassembler(image)                                             # シンボル無し（生のメモリ相当）
    listing = bare.around(0x20000098, before=4, after=1)
    assert listing.sync == "start" and [i.address for i in listing.instructions] == [0x20000098, 0x2000009C]


def test_range_stops_at_the_end_and_needs_readable_memory(sram):
    listing = sram.range(0x20000040, 0x20000050)                           # reached_milestone の全体
    assert [i.mnemonic for i in listing.instructions] == ["push", "mov", "movw", "movt", "str", "pop"]
    assert listing.instructions[-1].flow == "return"
    near_end = sram.around(0x200000BC, before=1, after=5)                  # .text の最後の命令の後ろは読めない
    assert near_end.instructions[-1].address == 0x200000BC


# --- disasm_demo（Thumb-1、リテラルプール） --------------------------------------------------

POOL = [(0x2000002C, 0xDEADBEEF), (0x20000030, 0x20000064), (0x20000034, 0x12345679), (0x20000038, 0x0BADF00D)]


def test_literal_pool_is_data_by_mapping_symbols(demo):
    listing = demo.function("mix")
    code = [i for i in listing.instructions if i.kind == "code"]
    data = [i for i in listing.instructions if i.kind == "data"]
    assert code[-1].address == 0x2000002A                                  # mov r8, r8（揃えの詰め物）
    assert [(d.address, d.mnemonic, d.literal_value) for d in data] == [(a, ".word", v) for a, v in POOL]
    assert data[0].operands == "0xdeadbeef"
    assert data[1].literal_name == "sink"


def test_pc_relative_loads_show_value_and_name(demo):
    listing = demo.function("mix")
    first = listing.instructions[2]
    assert (first.address, first.mnemonic, first.operands) == (0x20000018, "ldr", "r1, [pc, #0x10]")
    assert (first.literal, first.literal_value) == (0x2000002C, 0xDEADBEEF)
    second = listing.instructions[4]
    assert (second.literal, second.literal_value, second.literal_name) == (0x20000030, 0x20000064, "sink")
    assert "=0x20000064 <sink>" in str(second)
    peripheral = demo.function("store_peripheral").instructions
    load = next(i for i in peripheral if i.literal is not None)
    assert load.literal_value == 0x40020000


def test_without_mapping_symbols_the_body_does_not_guess_literal_pools():
    # 「ldr [pc] が読む先はデータ」は推定なので本体ではしない（3 回目の点検 ?14）。ldr の読む先と値（事実）は返す
    image = ElfImage(DISASM_DEMO)
    raw = BytesMemory(0x20000014, image.read_memory(0x20000014, 0x50))      # 実機から読んだメモリ相当
    listing = Disassembler(raw).range(0x20000014, 0x20000064)
    assert not any(i.data_basis == "pc_load" for i in listing.instructions)
    load = next(i for i in listing.instructions if i.mnemonic == "ldr" and i.literal is not None)
    assert (load.literal, load.literal_value) == (0x2000002C, 0xDEADBEEF)


def test_literal_pools_given_by_the_caller_are_data():
    # 使う側が渡した data_ranges（プラグイン heuristics.literal_pool_ranges が ldr [pc] の読む先から作れる）をデータに
    from fprobe.plugins import heuristics as h
    image = ElfImage(DISASM_DEMO)
    raw = BytesMemory(0x20000014, image.read_memory(0x20000014, 0x50))
    first = Disassembler(raw).range(0x20000014, 0x20000064)
    guess = h.literal_pool_ranges(first.instructions)
    assert guess["heuristic"] and guess["assumption"]
    ranges = [(r["address"], r["size"]) for r in guess["data_ranges"]]
    listing = Disassembler(raw, data_ranges=ranges).range(0x20000014, 0x20000064)
    data = [(i.address, i.literal_value) for i in listing.instructions if i.kind == "data"]
    assert data == POOL + [(0x2000004C, 0x40020000)]
    assert listing.instructions[-1].mnemonic == "b"                         # 最後まで命令の境界がずれない
    pool = [i for i in listing.instructions if i.kind == "data"]
    assert {i.data_basis for i in pool} == {"given"} and pool[0].as_dict()["data_basis"] == "given"


def test_data_basis_is_the_mapping_symbol_when_the_elf_has_one(demo):
    data = [i for i in demo.function("mix").instructions if i.kind == "data"]
    assert {i.data_basis for i in data} == {"mapping"}                      # ARM ELF の $d（規格の情報）
    assert all("推定" not in i.comment() for i in data)
    assert all(i.data_basis is None for i in demo.function("mix").instructions if i.kind == "code")


def test_given_data_ranges_do_not_override_the_mapping_symbols():
    # 渡された区間（0x1004）も、$t がコードと書いていればコードとして読む（ELF の事実が先）
    code = bytes.fromhex("004800bf00bf00bf")
    raw = BytesMemory(0x1000, code)
    given = Disassembler(raw, data_ranges=[(0x1004, 4)]).range(0x1000, 0x1008)
    assert [(i.address, i.kind, i.data_basis) for i in given.instructions][2] == (0x1004, "data", "given")
    marked = SimpleNamespace(code_map=CodeMap([(0x1000, "t")], [(0x1000, 0x1008)]), name_of=lambda a: None,
                             symbol_at=lambda a, kind: None)
    listing = Disassembler(raw, image=marked, data_ranges=[(0x1004, 4)]).range(0x1000, 0x1008)
    assert [(i.address, i.mnemonic) for i in listing.instructions] == [(0x1000, "ldr"), (0x1002, "nop"),
                                                                       (0x1004, "nop"), (0x1006, "nop")]


def test_undecodable_bytes_become_data():
    listing = Disassembler(BytesMemory(0x1000, bytes.fromhex("ffff00bf"))).range(0x1000, 0x1004)
    # 0xffff だけでは 32 ビット命令の前半で、続く 00bf と組んでも解読できない → 2 バイトのデータ
    assert [(i.kind, i.mnemonic) for i in listing.instructions][-1] == ("code", "nop")
    assert listing.instructions[0].kind == "data" and listing.instructions[0].data_basis == "undecodable"


def test_symbols_alone_are_enough_to_name(sram):
    syms = Symbols(SRAM_DEMO)
    dis = Disassembler(ElfImage(SRAM_DEMO), symbols=syms)
    assert dis.function("tick").instructions[-1].target_name == "reached_milestone"
    with pytest.raises(KeyError):
        dis.function("no_such_function")


def test_it_block_conditions_survive_around(sram):
    # popne は IT ブロックの中: 確かな点から続けて読まないと条件（ne）が分からない
    listing = sram.around(0x20000068, before=1, after=0)
    assert [i.mnemonic for i in listing.instructions] == ["it", "popne"]
    assert str(listing.instructions[listing.pc_index]).startswith("20000068: 80bd")


TBB_CODE = bytes.fromhex(
    "0228"        # 0x00 cmp r0, #2
    "04d8"        # 0x02 bhi #0x0e
    "dfe800f0"    # 0x04 tbb [pc, r0]
    "02030400"    # 0x08 表（3 項目 + 揃えの 1 バイト）
    "7047"        # 0x0c bx lr
    "7047")       # 0x0e bx lr


def test_tbb_table_size_is_not_guessed_from_the_compare():
    # 表の大きさを直前の cmp（コンパイラの慣用句）から推さない。大きさは使う側が branch_tables で明示する
    # （推定はプラグイン fprobe.plugins.heuristics.tbb_table_sizes）
    listing = Disassembler(BytesMemory(0x100, TBB_CODE)).range(0x100, 0x110)
    assert not any(i.mnemonic == ".byte" for i in listing.instructions)
    assert not hasattr(Disassembler, "_table_entries")


def test_tbb_table_is_data_when_the_caller_gives_its_size():
    listing = Disassembler(BytesMemory(0x100, TBB_CODE), branch_tables={0x104: 3}).range(0x100, 0x110)
    kinds = [(i.address - 0x100, i.kind, i.mnemonic) for i in listing.instructions]
    assert kinds == [(0, "code", "cmp"), (2, "code", "bhi"), (4, "code", "tbb"),
                     (8, "data", ".byte"), (9, "data", ".byte"), (10, "data", ".byte"), (11, "data", ".byte"),
                     (12, "code", "bx"), (14, "code", "bx")]
    assert listing.instructions[-1].flow == "return"
    assert {i.data_basis for i in listing.instructions if i.kind == "data"} == {"branch_table"}


def test_without_a_known_boundary_the_listing_starts_at_pc():
    # 関数の先頭・$t の無いメモリでは、PC より前の命令の境界は分からない。「Thumb は数命令で揃う」と見込んで前を
    # 解読しない（違う境界で読むと、もっともらしいが誤った命令を見せる）。PC から読み、そう書く
    code = bytes.fromhex("00bf" * 16)                                       # nop × 16
    listing = Disassembler(BytesMemory(0x1000, code)).around(0x1010, before=4, after=2)
    assert listing.sync == "start" and listing.pc_index == 0
    assert listing.instructions[0].address == 0x1010


def test_around_says_why_it_did_not_align_from_the_function_start(sram):
    """確かな境界から PC まで読む量の上限（sync_limit）で前を読まなかったら、そう言う（点検 2 回目 ?: disasm.py:27）。"""
    pc = sram.function("tick").instructions[3].address
    listing = sram.around(pc, before=2, after=1, sync_limit=2)
    assert listing.sync == "start" and listing.pc_index == 0
    start = sram.function("tick").instructions[0].address
    assert listing.sync_skipped == {"from": start, "kind": "function", "reason": "too_far", "distance": pc - start,
                                    "limit": 2}
    assert listing.as_dict()["sync_skipped"]["reason"] == "too_far"
    aligned = sram.around(pc, before=2, after=1)
    assert aligned.sync == "function" and aligned.sync_skipped is None
