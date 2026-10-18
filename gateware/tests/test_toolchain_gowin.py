from pathlib import Path

import pytest

from fprobe_gw.toolchain.gowin import GowinToolchain, ToolchainNotFound


def _make_home(root: Path) -> Path:
    (root / "IDE" / "bin").mkdir(parents=True)
    (root / "Programmer" / "bin").mkdir(parents=True)
    (root / "IDE" / "bin" / "gw_sh.exe").write_bytes(b"")
    (root / "Programmer" / "bin" / "programmer_cli.exe").write_bytes(b"")
    return root


def test_locate_uses_env_override(tmp_path):
    home = _make_home(tmp_path / "gowin")
    tc = GowinToolchain.locate(env={"FPROBE_GOWIN_HOME": str(home)})
    assert tc.home == home
    assert tc.gw_sh == home / "IDE" / "bin" / "gw_sh.exe"
    assert tc.programmer_cli == home / "Programmer" / "bin" / "programmer_cli.exe"


def test_locate_rejects_incomplete_install(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    with pytest.raises(ToolchainNotFound, match="gw_sh.exe"):
        GowinToolchain.locate(env={"FPROBE_GOWIN_HOME": str(home)})


def test_locate_falls_back_to_defaults(tmp_path, monkeypatch):
    home = _make_home(tmp_path / "default")
    monkeypatch.setattr("fprobe_gw.toolchain.gowin.DEFAULT_GOWIN_HOMES",
                        (tmp_path / "missing", home))
    assert GowinToolchain.locate(env={}).home == home


def test_locate_reports_all_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr("fprobe_gw.toolchain.gowin.DEFAULT_GOWIN_HOMES",
                        (tmp_path / "a", tmp_path / "b"))
    with pytest.raises(ToolchainNotFound) as e:
        GowinToolchain.locate(env={})
    assert str(tmp_path / "a") in str(e.value) and str(tmp_path / "b") in str(e.value)
