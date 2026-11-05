"""道具の所在解決の共通部品（toolchain/locate.py）。"""

from pathlib import Path

import pytest

from fprobe_gw.toolchain import gowin
from fprobe_gw.toolchain.locate import ToolchainNotFound, first_usable


def _missing(home: Path) -> list[Path]:
    return [p for p in (home / "a.exe", home / "b.exe") if not p.is_file()]


def test_first_usable_returns_the_first_home_with_all_files(tmp_path):
    bad, good, later = tmp_path / "bad", tmp_path / "good", tmp_path / "later"
    for home in (bad, good, later):
        home.mkdir()
    (bad / "a.exe").write_text("")
    for home in (good, later):
        (home / "a.exe").write_text("")
        (home / "b.exe").write_text("")
    assert first_usable([bad, good, later], lambda h: h, _missing, what="X", env_var="E") == good


def test_first_usable_lists_what_each_candidate_lacks(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (two / "a.exe").write_text("")
    with pytest.raises(ToolchainNotFound) as e:
        first_usable([one, two], lambda h: h, _missing, what="X", env_var="E_VAR")
    text = str(e.value)
    assert "X が見つからない（E_VAR で指定可能）" in text
    assert f"{one}: a.exe, b.exe が無い" in text and f"{two}: b.exe が無い" in text


def test_first_usable_without_candidates_says_so():
    with pytest.raises(ToolchainNotFound, match="候補なし"):
        first_usable([], lambda h: h, _missing, what="X", env_var="E")


def test_gowin_reexports_the_error_class():
    assert gowin.ToolchainNotFound is ToolchainNotFound
