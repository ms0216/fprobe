"""symbol_info: 番地の場所を文字（where）だけでなく、関数・ファイル・行の欄でも返す（画面は where の文の形から
ファイルと行を拾わない）。機器は使わない（sram_demo.elf）。"""

from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from fprobe.symbols import Symbols

SRAM_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._symbols = Symbols(SRAM_DEMO)
    return p


def test_the_place_is_returned_as_fields(probe):
    tick = probe._symbols.symbol("tick").address
    info = probe.symbol_info(f"0x{tick + 4:x}")
    place = probe._symbols.describe(tick + 4)
    assert info["function"] == "tick" and info["offset"] == 4
    assert info["file"] == place.file and info["line"] == place.line and info["line"] is not None
    assert info["where"] == str(place)


def test_unknown_fields_are_null(probe):
    info = probe.symbol_info("0x0")
    place = probe._symbols.describe(0)
    assert info["function"] == place.function and info["file"] == place.file and info["line"] == place.line
