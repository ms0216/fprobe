"""Probe のソースの道具（画面のデバッガ用）: 読み込んだ ELF が参照するソースだけを返す。機器は開かない。"""

from pathlib import Path

import pytest

from fprobe.debug import Stop
from fprobe.mcp_server import Probe
from fprobe.symbols import Location

ELF = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


@pytest.fixture
def probe():
    return Probe("unused")                                       # 機器は使う時まで開かない


def test_without_an_elf_there_are_no_sources(probe):
    assert probe.source_list() == {"files": []}
    with pytest.raises(RuntimeError):
        probe.source_read("sram_demo.c")


def test_sources_of_the_loaded_elf_can_be_listed_and_read(probe):
    loaded = probe.elf_load(str(ELF))
    names = {f["name"]: f for f in loaded["files"]}
    assert names["sram_demo.c"]["exists"]
    assert Path(names["sram_demo.c"]["path"]).parts[-4:] == ("host", "firmware", "sram_demo", "sram_demo.c")
    assert probe.source_list()["files"] == loaded["files"]
    src = probe.source_read("sram_demo.c")
    assert src["text"] == (ELF.parent / "sram_demo.c").read_text(encoding="utf-8")
    assert src["lines"] and all(isinstance(n, int) for n in src["lines"])


def test_only_files_the_elf_refers_to_can_be_read(probe):
    probe.elf_load(str(ELF))
    for name in ["build.py", "../../../CLAUDE.md", "C:/Windows/win.ini"]:
        with pytest.raises(KeyError):
            probe.source_read(name)


def test_stops_carry_function_file_and_line():
    stop = Stop("breakpoint", 0x20000010, Location(0x20000010, "tick", 4, "sram_demo.c", 12), 0.1)
    d = Probe._stop_dict(stop)
    assert (d["function"], d["file"], d["line"]) == ("tick", "sram_demo.c", 12)
    assert d["where"].startswith("tick+4")


def test_breakpoints_are_listed_with_file_and_line(probe, monkeypatch):
    probe.elf_load(str(ELF))
    address = probe._symbols.resolve("reached_milestone")

    class FakeDebugger:                                          # 機器なしで一覧の形だけ確かめる
        breakpoints = {address: "reached_milestone"}
        watches = {}
    monkeypatch.setattr(Probe, "dbg", property(lambda self: FakeDebugger()))
    b = probe.breakpoint_list()["breakpoints"][0]
    assert b["location"] == "reached_milestone" and b["file"] == "sram_demo.c" and b["function"] == "reached_milestone"


def test_network_paths_are_refused(probe):
    # 細工した ELF や引数で、外の共有（UNC）へつながせない（NTLM の認証が外へ出る）
    with pytest.raises(ValueError):
        probe.elf_load(r"\\attacker\share\x.elf")
    from fprobe.symbols import is_network_path
    assert is_network_path(r"\\host\share\a.c") and is_network_path("//host/share/a.c")
    assert not is_network_path(r"C:\src\a.c") and not is_network_path("/home/a.c")


def test_resume_steps_over_a_breakpoint_at_the_current_pc(probe, monkeypatch):
    # target.resume（画面の「実行」・AI）がブレークポイントの上から再開しても、同じ場所で止まり直さない
    calls = []

    class FakeDebugger:
        def resume(self):
            calls.append("dbg.resume")                           # ブレークポイントを 1 命令踏み越えてから再開する

    class OnlyThisProbe(Probe):                                   # クラス全体を差し替えない（ほかの試験の背景の
        dbg = property(lambda self: FakeDebugger())               # スレッドが偽物を踏まないように）
        target = property(lambda self: object())

        def target_status(self):
            return {"state": "RUNNING"}

    monkeypatch.setattr(probe, "__class__", OnlyThisProbe)
    assert probe.target_resume() == {"state": "RUNNING"} and calls == ["dbg.resume"]
