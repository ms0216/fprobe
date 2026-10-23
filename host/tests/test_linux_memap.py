import json
from pathlib import Path

import pytest

from fprobe.linux.memap import XsdbMemAp

FAKE = str(Path(__file__).with_name("linux_fake_xsdb.py"))


def test_reads_words_without_halting(monkeypatch, tmp_path):
    log = tmp_path / "xsdb.log"
    monkeypatch.setenv("FPROBE_FAKE_XSDB_LOG", str(log))
    monkeypatch.setenv("FPROBE_FAKE_XSDB_MEM", json.dumps({hex(0x1DF00000 + 4 * i): hex(i * 3) for i in range(8)}))
    m = XsdbMemAp(FAKE, cable_serial="1234-tulA", chain_idcode=0x23727093, timeout_s=10)
    try:
        assert m.read_words(0x1DF00000, 8) == [i * 3 for i in range(8)]
        assert m.info["cable_serial"] == "1234-tulA"
    finally:
        m.close()
    sent = log.read_text(encoding="utf-8")
    assert 'jtag_cable_serial == "1234-tulA"' in sent and 'name =~ "APU"' in sent
    assert "stop" not in sent and "-force" in sent                        # 止めない（APU = DAP の AHB-AP で読む）


def test_wrong_board_is_refused(monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_XSDB_JTAG", "  1  Xilinx TUL 1234-tulA\\n     2  arm_dap (idcode 4ba00477 irlen 4)")
    with pytest.raises(RuntimeError, match="0x23727093"):
        XsdbMemAp(FAKE, cable_serial="1234-tulA", chain_idcode=0x23727093, timeout_s=10)


def test_read_error_is_a_fact(monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_XSDB_MEM", "{}")
    m = XsdbMemAp(FAKE, cable_serial="1234-tulA", chain_idcode=0x23727093, timeout_s=10)
    try:
        with pytest.raises(RuntimeError, match="AHB AP transaction error"):
            m.read_words(0x40000000, 1)
    finally:
        m.close()


def test_hung_xsdb_times_out(monkeypatch):
    """xsdb が返事をしないとき、決めた時間で誤りにする（fprobe 全体を止めない）"""
    import time
    monkeypatch.setenv("FPROBE_FAKE_XSDB_HANG", "1")
    m = XsdbMemAp(FAKE, cable_serial="1234-tulA", chain_idcode=0x23727093, timeout_s=0.5)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="0.5"):
        m.read_words(0x1DF00000, 1)
    assert time.monotonic() - t0 < 5
    with pytest.raises(RuntimeError):
        m.read_words(0x1DF00000, 1)                     # 止めた後は使えない（事実で断る）
    m.close()


def test_idcode_must_be_on_the_chosen_cable(monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_XSDB_JTAG", "  1  Xilinx TUL 1234-tulA\\n     2  arm_dap (idcode 4ba00477 irlen 4)"
                       "\\n  3  Digilent 210251A08870\\n     4  xc7z020 (idcode 23727093 irlen 6 fpga)")
    with pytest.raises(RuntimeError, match="0x23727093"):
        XsdbMemAp(FAKE, cable_serial="1234-tulA", chain_idcode=0x23727093, timeout_s=10)
