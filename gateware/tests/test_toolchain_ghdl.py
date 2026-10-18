import os
from pathlib import Path

import pytest

from fprobe_gw.toolchain.ghdl import GhdlToolchain
from fprobe_gw.toolchain.gowin import ToolchainNotFound


def _make_home(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir(parents=True)
    (root / "bin" / "ghdl.exe").write_bytes(b"")
    (root / "lib" / "libghdlvpi.dll").write_bytes(b"")
    return root


def test_env_override_wins(tmp_path):
    home = _make_home(tmp_path / "ghdl")
    tc = GhdlToolchain.locate(env={"FPROBE_GHDL_HOME": str(home), "PATH": ""})
    assert tc.ghdl == home / "bin" / "ghdl.exe"
    assert tc.vpi_lib_dir == home / "lib"


def test_found_via_path(tmp_path, monkeypatch):
    home = _make_home(tmp_path / "onpath")
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", ())
    tc = GhdlToolchain.locate(env={"PATH": str(home / "bin")})
    assert tc.home == home


def test_missing_vpi_library_is_rejected(tmp_path, monkeypatch):
    home = _make_home(tmp_path / "broken")
    (home / "lib" / "libghdlvpi.dll").unlink()
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", ())
    with pytest.raises(ToolchainNotFound, match="libghdlvpi.dll"):
        GhdlToolchain.locate(env={"FPROBE_GHDL_HOME": str(home), "PATH": ""})


def test_not_found_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr("fprobe_gw.toolchain.ghdl.DEFAULT_GHDL_HOMES", (tmp_path / "none",))
    with pytest.raises(ToolchainNotFound, match="GHDL"):
        GhdlToolchain.locate(env={"PATH": ""})


def test_environment_prepends_required_directories(tmp_path):
    home = _make_home(tmp_path / "ghdl")
    env = GhdlToolchain(home).environment({"PATH": "C:\keep"})
    entries = env["PATH"].split(os.pathsep)
    assert entries[0] == str(home / "bin")
    assert str(home / "lib") in entries
    assert entries[-1] == "C:\keep"
