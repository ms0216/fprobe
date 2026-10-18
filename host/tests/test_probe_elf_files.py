"""fprobe.probe_elf_files: 画面からの ELF（ブラウザで選んだファイルを送る・同梱のデモ）。機器は開かない。"""

import base64

import pytest

from fprobe.mcp_server import Probe
from fprobe.probe_elf_files import DEMO_ELF


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path / "home"))
    return Probe("unused")


def test_the_demo_elf_is_bundled_and_loads(probe):
    assert DEMO_ELF.is_file()
    out = probe.elf_load_demo()
    assert out["path"] == str(DEMO_ELF)
    assert "sram_demo.c" in {f["name"] for f in out["files"]}


def test_an_uploaded_elf_is_kept_under_the_home_and_loaded(probe, tmp_path):
    data = base64.b64encode(DEMO_ELF.read_bytes()).decode()
    out = probe.elf_upload("C:\\work\\..\\app.elf", data)              # 名前の中の場所は捨てる
    saved = tmp_path / "home" / "uploads" / "app.elf"
    assert out["path"] == str(saved) and saved.read_bytes() == DEMO_ELF.read_bytes()
    assert probe.source_list()["files"]


@pytest.mark.parametrize("name", ["", "..", "a/..", "con:"])
def test_bad_names_are_refused(probe, name):
    with pytest.raises(ValueError):
        probe.elf_upload(name, base64.b64encode(b"\x7fELF").decode())


def test_data_that_is_not_an_elf_is_refused_before_it_is_written(probe, tmp_path):
    with pytest.raises(ValueError):
        probe.elf_upload("x.elf", base64.b64encode(b"hello").decode())
    assert not (tmp_path / "home" / "uploads" / "x.elf").exists()


@pytest.mark.parametrize(("name", "data", "fmt"), [("app.elf", DEMO_ELF.read_bytes(), "elf"),
                                                   ("app.hex", b":00000001FF\n", "hex"),
                                                   ("app.bin", bytes(range(16)), "bin")], ids=["elf", "hex", "bin"])
def test_a_file_to_flash_is_kept_under_the_home_and_not_loaded(probe, tmp_path, name, data, fmt):
    """書き込むファイル（ELF / HEX / BIN）を画面から送る: 置いた場所・大きさ・形式を返し、ELF でも読まない（Flash の
    画面がそのパスを target.flash に渡す）。"""
    out = probe.flash_upload(f"C:\\work\\{name}", base64.b64encode(data).decode())
    saved = tmp_path / "home" / "uploads" / name
    assert out == {"path": str(saved), "bytes": len(data), "format": fmt}
    assert saved.read_bytes() == data
    assert probe._symbols is None


def test_a_file_to_flash_is_refused_when_empty_or_badly_named(probe):
    with pytest.raises(ValueError):
        probe.flash_upload("app.bin", "")
    with pytest.raises(ValueError):
        probe.flash_upload("..", base64.b64encode(b"x").decode())
