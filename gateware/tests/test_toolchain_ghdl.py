import os
import stat
from pathlib import Path

import pytest

from fprobe_gw.toolchain import ghdl as ghdl_mod
from fprobe_gw.toolchain.ghdl import GhdlToolchain
from fprobe_gw.toolchain.gowin import ToolchainNotFound

EXE, VPI = ghdl_mod.ghdl_name(), ghdl_mod.vpi_name()        # この OS の名前（Windows: ghdl.exe・libghdlvpi.dll）


def _make_home(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir(parents=True)
    (root / "bin" / EXE).write_bytes(b"")
    (root / "bin" / EXE).chmod((root / "bin" / EXE).stat().st_mode | stat.S_IXUSR)   # PATH で探せる（Linux は実行の印が要る）
    (root / "lib" / VPI).write_bytes(b"")
    return root


def test_env_override_wins(tmp_path):
    home = _make_home(tmp_path / "ghdl")
    tc = GhdlToolchain.locate(env={"FPROBE_GHDL_HOME": str(home), "PATH": ""})
    assert tc.ghdl == home / "bin" / EXE
    assert tc.vpi_lib_dir == home / "lib"


def test_found_via_path(tmp_path, monkeypatch):
    home = _make_home(tmp_path / "onpath")
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", ())
    tc = GhdlToolchain.locate(env={"PATH": str(home / "bin")})
    assert tc.home == home


def test_missing_vpi_library_is_rejected(tmp_path, monkeypatch):
    home = _make_home(tmp_path / "broken")
    (home / "lib" / VPI).unlink()
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", ())
    with pytest.raises(ToolchainNotFound, match=VPI):
        GhdlToolchain.locate(env={"FPROBE_GHDL_HOME": str(home), "PATH": ""})


def test_not_found_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", (tmp_path / "none",))
    with pytest.raises(ToolchainNotFound, match="GHDL"):
        GhdlToolchain.locate(env={"PATH": ""})


def test_environment_prepends_required_directories(tmp_path):
    home = _make_home(tmp_path / "ghdl")
    keep = str(tmp_path / "keep")
    env = GhdlToolchain(home).environment({"PATH": keep})
    entries = env["PATH"].split(os.pathsep)
    assert entries[0] == str(home / "bin")
    assert str(home / "lib") in entries
    assert entries[-1] == keep


def test_the_names_follow_the_os(monkeypatch):
    monkeypatch.setattr(ghdl_mod, "WINDOWS", True)
    assert (ghdl_mod.ghdl_name(), ghdl_mod.vpi_name()) == ("ghdl.exe", "libghdlvpi.dll")
    monkeypatch.setattr(ghdl_mod, "WINDOWS", False)
    assert (ghdl_mod.ghdl_name(), ghdl_mod.vpi_name()) == ("ghdl", "libghdlvpi.so")


@pytest.mark.skipif(ghdl_mod.WINDOWS, reason="Linux の配置（ghdl はシェルの起動役、VPI は GHDL が示す場所）")
def test_a_linux_ghdl_on_path_gives_its_vpi_library_directory(tmp_path, monkeypatch):
    # Debian / Ubuntu のパッケージ: /usr/bin/ghdl（起動役）・/usr/lib/ghdl/mcode/libghdlvpi.so。lib の場所は
    # `ghdl --vpi-library-dir` が答える（2026-11-03 のレビューで、Linux の配置を探せなかったのを直した）
    vpi = tmp_path / "usr" / "lib" / "ghdl" / "mcode"
    vpi.mkdir(parents=True)
    (vpi / "libghdlvpi.so").write_bytes(b"")
    bin_dir = tmp_path / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "ghdl"
    tool.write_text(f'#!/bin/sh\n[ "$1" = --vpi-library-dir ] && echo {vpi}\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", ())
    tc = GhdlToolchain.locate(env={"PATH": str(bin_dir)})
    assert tc.ghdl == tool and tc.vpi_lib_dir == vpi
    assert not tc.missing_files()
    assert str(vpi) in tc.environment({"PATH": ""})["PATH"].split(os.pathsep)


@pytest.mark.skipif(ghdl_mod.WINDOWS, reason="Linux の配置")
def test_a_ghdl_that_names_no_vpi_library_is_rejected_with_the_reason(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "ghdl"
    tool.write_text("#!/bin/sh\nexit 1\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", ())
    with pytest.raises(ToolchainNotFound, match="libghdlvpi.so"):
        GhdlToolchain.locate(env={"PATH": str(bin_dir)})
