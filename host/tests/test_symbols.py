"""fprobe.symbols: 試験用ファームウェア（host/firmware/sram_demo、zig cc で Cortex-M4 向け）の ELF を読む。"""

from pathlib import Path

import pytest

from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
SOURCE = (ELF.parent / "sram_demo.c").read_text(encoding="utf-8").splitlines()


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


@pytest.fixture(scope="module")
def syms():
    return Symbols(ELF)


def test_functions_and_variables_resolve_to_addresses(syms):
    tick = syms.symbol("tick")
    assert tick.kind == "function" and tick.address % 2 == 0 and tick.size > 0      # Thumb のビットは落ちている
    assert syms.resolve("tick") == tick.address
    table = syms.variable("table")
    assert table.size == 16 and 0x20000000 <= table.address < 0x20008000
    assert syms.resolve("0x20000100") == 0x20000100 and syms.resolve(0x1234) == 0x1234
    with pytest.raises(KeyError):
        syms.variable("tick")                                                         # 関数は変数ではない
    with pytest.raises(KeyError):
        syms.resolve("no_such_symbol")


def test_addresses_describe_function_and_line(syms):
    target = syms.symbol("reached_milestone")
    where = syms.describe(target.address + 2)
    assert (where.function, where.offset, where.file) == ("reached_milestone", 2, "sram_demo.c")
    assert line_of("void reached_milestone") <= where.line <= line_of("milestone = value;")
    assert syms.describe(0x08000000).function is None                                 # ELF の外


def test_file_and_line_resolve_to_the_first_instruction(syms):
    line = line_of("milestone = value;")
    address = syms.resolve(f"sram_demo.c:{line}")
    assert syms.describe(address).function == "reached_milestone"
    assert syms.describe(address).line == line
    with pytest.raises(KeyError):
        syms.resolve("sram_demo.c:1")                                                 # コメント行に命令は無い


def test_loadable_segments_cover_the_vector_table_and_data(syms):
    starts = {s.address for s in syms.segments}
    assert 0x20000000 in starts
    vectors = next(s for s in syms.segments if s.address == 0x20000000).data
    assert int.from_bytes(vectors[4:8], "little") & ~1 == syms.symbol("reset_handler").address
    data = b"".join(s.data for s in syms.segments)
    assert (0x22222222).to_bytes(4, "little") in data                                # table の初期値


def test_sources_are_found_with_their_directories(syms):
    sources = syms.sources()
    # 場所はビルドした場所か ELF の隣（リポジトリを置いた場所によらない）。中身が同じであること
    assert sources["sram_demo.c"].parts[-4:] == ("host", "firmware", "sram_demo", "sram_demo.c")
    assert sources["sram_demo.c"].read_bytes() == (ELF.parent / "sram_demo.c").read_bytes()
    assert sources["stdint.h"].name == "stdint.h" and sources["stdint.h"] != ELF.parent / "stdint.h"


def test_lines_with_code_are_known_per_file(syms):
    lines = syms.lines_of("sram_demo.c")
    assert line_of("milestone = value;") in lines
    assert 1 not in lines and lines == sorted(lines)
    assert syms.lines_of("no_such.c") == []


def test_a_second_file_with_the_same_name_does_not_mix_its_lines(syms, monkeypatch):
    # 同じ名前（basename）のファイルが 2 つあると、場所は先のものを使う。行も先のファイルのものだけにする
    first, other = syms.sources()["sram_demo.c"], Path("C:/elsewhere/sram_demo.c")
    rows = [(0x100, "sram_demo.c", 10, first), (0x200, "sram_demo.c", 99, other)]
    monkeypatch.setattr(syms, "_lines", rows)
    assert syms.lines_of("sram_demo.c") == [10]
    with pytest.raises(KeyError):
        syms.resolve("sram_demo.c:99")


def test_local_long_paths_are_not_network_paths():
    from fprobe.symbols import is_network_path
    assert not is_network_path(r"\\?\C:\very\long\a.c")
    assert is_network_path(r"\\?\UNC\host\share\a.c") and is_network_path(r"\\.\pipe\x")


def test_breakpoints_by_function_name_go_after_the_prologue():
    """関数名のブレークポイントは前置き（スタックの用意）の後へ。引数・ローカル変数が読める場所（GDB と同じ）。"""
    elf = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"
    syms = Symbols(elf)
    start = syms.resolve("leaf_sum")
    body = syms.after_prologue(start)
    assert start < body < start + 0x20
    assert syms.describe(body).line == syms.describe(start).line + 1 or syms.describe(body).line > syms.describe(start).line
    assert syms.after_prologue(0x12345678) == 0x12345678                     # 関数の外はそのまま


def test_without_prologue_end_the_function_entry_is_kept():
    # 行表に prologue_end が無い関数で「次の行の始まり」を前置きの終わりと推定しない（点検 2 回目 H10。GDB 流の経験則は
    # プラグインの after_first_line）: 関数の入口のまま
    elf = Path(__file__).resolve().parents[1] / "firmware" / "etm_demo" / "etm_demo.elf"
    syms = Symbols(elf)
    leaf = syms.resolve("leaf")
    assert syms.after_prologue(leaf) == leaf
    assert syms.has_prologue_end(leaf) is False
    o0 = Symbols(Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf")
    assert o0.has_prologue_end(o0.resolve("leaf_sum")) is True


def test_line_entries_give_the_statement_that_contains_an_address():
    """行単位のステップの土台: 番地を含む文（行表の is_stmt の行）の始まり・ファイル・行。関数の外の行は使わない。"""
    elf = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"
    syms = Symbols(elf)
    start = syms.resolve("leaf_sum")
    body = syms.after_prologue(start)
    entry = syms.line_entry(body)
    assert (entry.address, entry.file) == (body, "dwarf_demo.c") and entry.line == syms.describe(body).line
    inside = syms.line_entry(body + 2)
    assert inside.address <= body + 2 and inside.line == syms.describe(body + 2).line
    assert syms.line_entry(0x12345678) is None                                 # 関数の外・行表の外
    assert syms.function_at(body).name == "leaf_sum" and syms.function_at(0x12345678) is None
    assert syms.function_at(syms.resolve("sensors")) is None                   # 変数は関数ではない


def test_cxx_functions_have_their_source_names():
    """C++: ELF のシンボルはマングルされた名前。DWARF の名前空間・クラスと linkage_name からソースの名前を付ける。
    多重定義は引数の型を添えて区別する。マングルされた名前でも引ける。"""
    elf = Path(__file__).resolve().parents[1] / "firmware" / "cxx_demo" / "cxx_demo.elf"
    syms = Symbols(elf)
    step = syms.resolve("motor::Pid::step")
    assert step == syms.resolve("_ZN5motor3Pid4stepEi")
    assert syms.describe(step + 2).function == "motor::Pid::step"
    one, two = syms.resolve("motor::scale(int)"), syms.resolve("motor::scale(int, int)")
    assert one != two and syms.describe(two).function == "motor::scale(int, int)"
    assert syms.describe(syms.resolve("motor::twice<int>")).function == "motor::twice<int>"
    with pytest.raises(KeyError, match=r"motor::scale\(int\)"):
        syms.resolve("motor::scale")                                           # 多重定義: どれかを選ぶよう候補を示す
    assert syms.after_prologue(step) > step                                    # 関数名のブレークポイントは前置きの後
    assert syms.function_at(step).name == "motor::Pid::step"
    assert syms.resolve("cxx_demo.cpp:" + str(syms.describe(step).line)) <= step + 0x10   # ファイル:行 はそのまま

    from fprobe.calling import signature_of
    from fprobe.debug import Debugger
    from fprobe.dwarf.info import DebugInfo
    assert Debugger(None, syms).breakpoint_address("motor::Pid::step") == syms.after_prologue(step)
    info = DebugInfo(elf)
    assert [n for n, _ in signature_of(info, syms, "motor::scale(int, int)").parameters] == ["v", "k"]
    assert [n for n, _ in signature_of(info, syms, "motor::Pid::step").parameters] == ["this", "error"]   # this は r0


def test_sources_moved_with_the_elf_are_found_next_to_it(tmp_path, monkeypatch):
    """ビルドした場所に無いソースは、ELF の隣にあればそれを使う（リポジトリを別の場所に置いたとき、同梱のデモの
    ソースが見つからず画面に出なかった。2026-10-19、公開の準備で発見）。ビルドした場所にあればそちらが先。"""
    import shutil
    shutil.copy(ELF, tmp_path / ELF.name)
    shutil.copy(ELF.parent / "sram_demo.c", tmp_path / "sram_demo.c")
    built = Symbols(ELF).sources()["sram_demo.c"]
    assert Symbols(tmp_path / ELF.name).sources()["sram_demo.c"] == built      # ビルドした場所にあればそのまま
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if self == built else real_exists(self))
    moved = Symbols(tmp_path / ELF.name)
    assert moved.sources()["sram_demo.c"] == tmp_path / "sram_demo.c"
    assert moved.relocated() == {"sram_demo.c": {"built": built, "used": tmp_path / "sram_demo.c"}}   # 置き換えた印（?68）
    assert moved.lines_of("sram_demo.c")                                 # 行も新しい場所で引ける
    assert moved.sources()["stdint.h"].name == "stdint.h"                # 隣に無いものは元の場所のまま
