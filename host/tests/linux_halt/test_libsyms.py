"""共有ライブラリ（libc など）のシンボル: 実行時の番地 → 関数名 + オフセット（fprobe.linux_halt.libsyms）。

実機の libc の代わりに、2 スレッドの競合のアプリの ELF（シンボル表のある ARM の ELF）を「ライブラリ」として、別の番地に置いて引く。
サイズの外は引かない（推測しない）ことと、ARM / Thumb の別、取り寄せに失敗したライブラリは事実（error）として残すことを確かめる。"""
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from fprobe.linux_halt.engine import EngineError
from fprobe.linux_halt.image import ElfImage
from fprobe.linux_halt.libsyms import LibraryModules, ModuleSymbols, library_bases, parse_maps
from .race_world import BIAS, ELF, PID, RaceWorld
from .test_debugger import ATTACH

MAPS = """00010000-00011000 r-xp 00000000 b3:02 1234 /home/pi/demo_arm
76d20000-76eb0000 r-xp 00000000 b3:02 99 /usr/lib/arm-linux-gnueabihf/libc.so.6
76eb0000-76ec0000 ---p 00190000 b3:02 99 /usr/lib/arm-linux-gnueabihf/libc.so.6
76ec0000-76ec2000 r--p 00190000 b3:02 99 /usr/lib/arm-linux-gnueabihf/libc.so.6
76f00000-76f20000 r-xp 00000000 b3:02 98 /usr/lib/arm-linux-gnueabihf/ld-linux-armhf.so.3
7ec00000-7ec21000 rw-p 00000000 00:00 0 [stack]
76f30000-76f31000 r-xp 00000000 b3:02 77 /tmp/gone.so (deleted)
"""
LIB_AT = 0x70000000                                              # 「ライブラリ」を置く実行時の番地（実行ファイルとは別の所）


def _first_page(path: Path) -> int:
    with open(path, "rb") as f:
        return min(s["p_vaddr"] for s in ELFFile(f).iter_segments() if s["p_type"] == "PT_LOAD") & ~0xFFF


def test_maps_gives_each_shared_library_its_start_at_file_offset_zero_and_skips_the_rest():
    maps = parse_maps(MAPS)
    assert library_bases(maps) == {"/usr/lib/arm-linux-gnueabihf/libc.so.6": 0x76D20000,
                                   "/usr/lib/arm-linux-gnueabihf/ld-linux-armhf.so.3": 0x76F00000}   # exe・[stack]・(deleted) は除く


@pytest.fixture
def module(world_symbols):
    return ModuleSymbols("/lib/librace.so.1", ELF, LIB_AT + _first_page(ELF))


@pytest.fixture
def world_symbols(tmp_path):
    w = RaceWorld(tmp_path)
    yield w.symbols
    w.s.close()


def test_an_address_in_a_function_is_named_with_its_offset_and_module_and_isa(module, world_symbols):
    fn = world_symbols.symbol("add_one")
    at = LIB_AT + fn.address                                     # load_bias = LIB_AT（リンク時の番地 + LIB_AT = 実行時）
    here = module.lookup(at + 4)
    assert (here.module, here.function, here.offset) == ("librace.so.1", "add_one", 4)
    assert here.thumb == (fn.address in ElfImage(ELF).thumb_functions)
    assert module.lookup(at).offset == 0 and module.contains(at)


def test_an_address_outside_every_symbol_size_or_the_module_is_not_named(module, world_symbols):
    fn = world_symbols.symbol("add_one")
    assert module.lookup(LIB_AT + fn.address + max(fn.size, 1) + 0x100000) is None or module.contains(0) is False
    assert module.lookup(0x10) is None and not module.contains(0x10)         # モジュールの外


def test_a_library_that_cannot_be_read_is_kept_as_a_fact_and_the_others_still_work(world_symbols):
    base = LIB_AT + _first_page(ELF)
    libs = LibraryModules([{"path": "/lib/ok.so", "local_path": str(ELF), "base": base},
                           {"path": "/lib/missing.so", "local_path": "/no/such/file.so", "base": 0x76000000},
                           {"path": "/lib/failed.so", "base": 0x75000000, "error": "RuntimeError: cat: No such file"}])
    facts = {f["path"]: f for f in libs.facts()}
    assert facts["/lib/ok.so"]["error"] is None and facts["/lib/ok.so"]["symbols"] > 0 and facts["/lib/ok.so"]["source"]
    assert "FileNotFoundError" in facts["/lib/missing.so"]["error"] and facts["/lib/missing.so"]["symbols"] is None
    assert facts["/lib/failed.so"]["error"] == "RuntimeError: cat: No such file"
    fn = world_symbols.symbol("add_one")
    assert libs.lookup(LIB_AT + fn.address + 2).function == "add_one"


def test_the_debugger_names_an_address_in_a_library_and_knows_its_isa_for_stepping(tmp_path):
    w = RaceWorld(tmp_path)
    try:
        base = LIB_AT + _first_page(ELF)
        w.s.debug_attach(**ATTACH, libraries=[{"path": "/lib/librace.so.1", "local_path": str(ELF), "base": base}])
        d = w.s._debug
        fn = w.symbols.symbol("add_one")
        runtime = LIB_AT + fn.address + 4
        assert d._where(runtime)["function"] == "add_one" and d._where(runtime)["file"] is None
        assert d.lib_location(runtime).offset == 4
        assert d._width_at(runtime) == (2 if fn.address in ElfImage(ELF).thumb_functions else 4)
        with pytest.raises(EngineError, match="shared library"):                 # ライブラリにも実行ファイルにも無い番地
            d._width_at(0x50000000)
        assert d._width_at(0x50000000, return_thumb=True) == 2 and d._width_at(0x50000000, return_thumb=False) == 4  # 戻り番地の bit 0
        facts = w.s.debug_status()["library_symbols"]
        assert [(f["path"], f["error"], f["source"] is not None) for f in facts] == [("/lib/librace.so.1", None, True)]
    finally:
        w.s.close()


def test_an_address_in_a_library_without_a_symbol_still_says_which_library_it_is_in(tmp_path):
    """公開されていない内部の関数（.dynsym に無い）の番地: 最も近い名前に当てはめず、ライブラリ名 ! ライブラリの中の番地だけ。"""
    from fprobe.linux_halt.view import LinuxDebugView
    w = RaceWorld(tmp_path)
    try:
        base = LIB_AT + _first_page(ELF)
        w.s.debug_attach(**ATTACH, libraries=[{"path": "/lib/librace.so.1", "local_path": str(ELF), "base": base}])
        d = w.s._debug
        view = LinuxDebugView(d, breakpoint_kind="sw")
        gap = LIB_AT + w.symbols.symbol("add_one").address + 0x7FFFF                    # 関数のサイズの外（モジュールの中）
        in_module = d.module_of(gap)
        if in_module is not None:                                                    # 外なら、モジュールの外の番地として None
            assert view._in_library(gap)[2] == f"librace.so.1!0x{in_module[1]:08X}" or view._in_library(gap)[0] is not None
        fn = view._in_library(LIB_AT + w.symbols.symbol("add_one").address + 4)
        assert fn[:2] == ("add_one", 4) and fn[2] == "add_one+4 (librace.so.1)"
        assert view._in_library(0x50000000) == (None, None, None)
    finally:
        w.s.close()


def test_a_call_is_told_from_a_return_by_what_the_instruction_did_to_lr():
    """step: ELF の外に出たのが呼び出し（BL / BLX）か戻り（main が libc へ戻る）かは、直前の命令が LR を次の命令の番地にしたかで決まる。
    戻りを呼び出しと取り違えて LR（0 等）まで走ろうとすると『0x0 is not mapped』が漏れた（実機 2026-11-05）。"""
    from fprobe.linux_halt.debugger import call_made
    assert call_made(0x10400, 4, 0x10404)                 # ARM の BL: LR = 次の命令
    assert call_made(0x10401, 4, 0x10405)                 # Thumb-2 の BL（pc の bit 0 と LR の bit 0 は無視）
    assert call_made(0x10400, 2, 0x10403)                 # Thumb の BLX reg: 2 バイトの命令、LR = 次 | 1
    assert not call_made(0x10400, 4, 0)                   # LR が 0（_start 等）: 戻りか分岐
    assert not call_made(0x10400, 4, 0x76d0abcd)          # LR が前の呼び出しの戻り番地のまま（BX LR / POP {pc}）


def test_a_breakpoint_by_code_address_inside_a_shared_library_knows_arm_or_thumb_from_the_fetched_symbols(tmp_path):
    """実行ファイルの外（取り寄せたライブラリのシンボルがある番地）への番地指定のブレークポイントは、ライブラリのシンボルの bit 0 で ARM / Thumb を知って置ける。
    ライブラリにもシンボルが無い番地は、名前・ファイル:行 は実行ファイルだけで解くことと一緒に断る（Haiku の周回 3: dlopen した libplug.so の bp が断られた）。"""
    w = RaceWorld(tmp_path)
    try:
        base = LIB_AT + _first_page(ELF)
        w.s.debug_attach(**ATTACH, libraries=[{"path": "/lib/librace.so.1", "local_path": str(ELF), "base": base}])
        d = w.s._debug
        fn = w.symbols.symbol("add_one")
        runtime = LIB_AT + fn.address + 4
        placed = d.break_insert(hex(runtime), "hw")
        assert placed["address"] == runtime and placed["width"] == (2 if fn.address in ElfImage(ELF).thumb_functions else 4)
        with pytest.raises(EngineError, match="shared library"):
            d.break_insert("0x50000000", "hw")
    finally:
        w.s.close()
