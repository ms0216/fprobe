"""linux.halt.profile.load: 読めないファイルは、何が悪いかを日本語の ValueError で返す（生の FileNotFoundError を出さない）。"""
import pytest

from fprobe.probe_linux_halt import LinuxHaltService


def load(path):
    return LinuxHaltService.linux_halt_profile_load(None, str(path))


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="設定のファイルが無い"):
        load(tmp_path / "none.json")


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"x"', "{}"])
def test_broken_or_foreign_file_is_a_clear_error(tmp_path, text):
    p = tmp_path / "p.json"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load(p)
