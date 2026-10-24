"""Probe のメモリ・スタックの道具（U26）: debug.elf.size・debug.stack.static・target.stack.paint / usage。
stack_demo.elf と偽のターゲット（バイトの辞書のメモリ）で。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_fakes import DictMemory, FakeTarget
from pyocd.core.target import Target

from fprobe.mcp_server import Probe
from fprobe.symbols import Symbols

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "stack_demo"
ELF = DEMO / "stack_demo.elf"
TOP = 0x2001_8000
BOTTOM = TOP - 0x400                     # _estack − _Min_Stack_Size


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=None)
    p._symbols = Symbols(ELF)
    yield p
    p._stop_observing()


def connect(p, msp=TOP - 0x80, halted=True):
    target = FakeTarget(DictMemory(), {"msp": msp, "sp": msp}, halted=halted)
    target.memory_map = SimpleNamespace(regions=[
        SimpleNamespace(name="Flash", start=0x0800_0000, length=512 * 1024, is_flash=True),
        SimpleNamespace(name="IRAM", start=0x2000_0000, length=96 * 1024, is_flash=False)])
    p._debug = SimpleNamespace(target=target, probe=None, close=lambda: None)
    return target


def test_elf_size_regions_from_the_linker_script_or_the_target(probe):
    out = probe.elf_size(linker_script=str(DEMO / "link.ld"), top=2)
    assert out["regions_source"] == "linker_script"
    regions = {r["name"]: r for r in out["regions"]}
    assert regions["FLASH"]["used"] == 700 and regions["RAM"]["length"] == 96 * 1024
    assert [s["name"] for s in out["symbols"]] == ["big_buffer", "Reset_Handler"]
    assert probe.elf_size()["regions"] == [] and probe.elf_size()["regions_source"] is None
    connect(probe)
    target = probe.elf_size()
    assert target["regions_source"] == "target" and {r["name"] for r in target["regions"]} == {"Flash", "IRAM"}
    explicit = probe.elf_size(regions=[{"name": "R", "origin": 0x2000_0000, "length": 4096}], kind="object", top=1)
    assert explicit["regions_source"] == "regions" and explicit["symbols"][0]["region"] == "R"


def test_elf_size_of_another_elf_and_errors(probe, tmp_path):
    sram = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
    assert probe.elf_size(elf=str(sram))["elf"] == str(sram)
    probe._symbols = None
    with pytest.raises(RuntimeError, match="debug_elf_load"):
        probe.elf_size()
    with pytest.raises(ValueError, match="共有"):
        probe.elf_size(elf="//server/share/app.elf")


def test_stack_static_finds_the_su_next_to_the_elf(probe):
    out = probe.stack_static(top=3, vector_table=0x0800_0000)
    assert out["su_files"] == [str(DEMO / "stack_demo.su")] and len(out["functions"]) == 3
    assert out["su_paths"] == [str(DEMO)]                                   # 探した所を返す（?48）
    assert out["msp"]["exception_frame_source"] in ("fpu_instructions", "no_fpu_instructions")   # 枠の出どころ（?63）
    assert out["functions_total"] > 10 and out["msp"]["thread"] == "Reset_Handler"
    assert out["vector_table"] == {"address": 0x0800_0000, "size": 64}
    bare = probe.stack_static(top=3)                                          # ベクタ表を名前で探さない
    assert bare["msp"] is None and bare["vector_table"] is None
    one = probe.stack_static(function="tail")
    assert one["functions"][0]["path"] == ["tail", "middle", "leaf"] and one["functions"][0]["max"] == 112
    # 画面の呼び出しの木（Call Graph）: 直接の呼び先と関数の番地
    assert one["functions"][0]["calls"] == ["middle"]
    assert one["functions"][0]["address"] == probe._symbols.symbol("tail").address
    hinted = probe.stack_static(function="apply", indirect={"apply": ["op_double", "op_inc"]})
    assert hinted["functions"][0]["bounded"]
    nosu = probe.stack_static(su_paths=[], function="middle")
    assert nosu["su_files"] == [] and nosu["functions"][0]["frame_source"] == "disasm"
    with pytest.raises(KeyError, match="nothing"):
        probe.stack_static(function="nothing")


def test_paint_then_measure_the_msp(probe):
    target = connect(probe, msp=TOP - 0x80)
    painted = probe.stack_paint(margin=64, top_symbol="_estack", size_symbol="_Min_Stack_Size")
    assert painted["margin"] == 64
    assert painted["bottom"] == BOTTOM and painted["end"] == TOP - 0x80 - 64 and painted["top"] == TOP
    assert painted["fill"] == 0xCD and painted["bytes"] == 0x400 - 0x80 - 64
    assert target.memory.read_memory(BOTTOM, 4) == b"\xCD" * 4
    assert target.memory.read_memory(TOP - 0x80 - 64, 4) == b"\x00" * 4           # SP の近くは塗らない
    target.memory.write_memory(TOP - 0x200, b"\x11" * 8)                            # 走って深く使った
    target.state = Target.State.RUNNING
    m = probe.stack_usage()
    assert m["used"] == 0x200 and m["sp"] is None and not m["overflow"] and m["painted"]
    target.state = Target.State.HALTED
    target.registers["msp"] = TOP - 0x40
    m = probe.stack_usage(threshold=0.5)
    assert m["current"] == 0x40 and m["warning"] and m["fraction"] == 0.5
    target.memory.write_memory(BOTTOM, b"\x00" * 16)
    assert probe.stack_usage()["reached_bottom"]


def test_paint_needs_a_halted_core_and_room(probe):
    target = connect(probe, halted=False)
    with pytest.raises(RuntimeError, match="止め"):
        probe.stack_paint()
    target.state = Target.State.HALTED
    with pytest.raises(ValueError, match="top_symbol"):
        probe.stack_paint()                                 # 範囲の名前を推さない
    target.registers["msp"] = BOTTOM + 8
    with pytest.raises(ValueError, match="SP"):
        probe.stack_paint(top_symbol="_estack", size_symbol="_Min_Stack_Size")
    target.registers["msp"] = TOP
    out = probe.stack_paint(bottom=TOP - 0x100, top=TOP, fill=0xAA, margin=0)
    assert out["bytes"] == 0x100 and out["bounds"]["bottom_source"] == "argument"


def test_usage_without_paint_is_marked(probe):
    # 塗りの記録が無ければ塗りの値（IAR の慣習の 0xCD 等）を仮定しない（点検 2 回目 H12）: 使う側が fill で渡す
    connect(probe)
    with pytest.raises(ValueError, match="fill"):
        probe.stack_usage(top_symbol="_estack", size_symbol="_Min_Stack_Size")
    m = probe.stack_usage(top_symbol="_estack", size_symbol="_Min_Stack_Size", fill=0xCD)
    assert not m["painted"] and m["reached_bottom"] and any("塗" in n for n in m["notes"]) and m["fill"] == 0xCD


def test_usage_after_paint_uses_the_painted_value_even_with_bounds(probe):
    # 範囲を渡しても、塗りの記録の値（事実）で測る（暗黙の既定の値に戻らない）
    target = connect(probe, msp=TOP)
    probe.stack_paint(bottom=TOP - 0x100, top=TOP, fill=0xAA, margin=0)
    target.memory.write_memory(TOP - 0x40, b"\x11" * 4)
    m = probe.stack_usage(bottom=TOP - 0x100, top=TOP)
    assert m["fill"] == 0xAA and m["used"] == 0x40 and m["painted"]


def test_stack_static_reuses_the_loaded_symbols(probe, monkeypatch):
    """読み込み済みの ELF の名前・行の表を使い回す（逆アセンブラのために DWARF を読み直さない。2026-10-18 に XIAO の
    ZMK（Zephyr）で、読み直しに 11 秒かかり stack_static が 16 秒だった）。"""
    import fprobe.disasm
    monkeypatch.setattr(fprobe.disasm, "Symbols", lambda *a, **k: pytest.fail("DWARF を読み直した"))
    assert probe.stack_static(top=1)["functions"]
