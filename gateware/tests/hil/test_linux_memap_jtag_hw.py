"""実機: PYNQ-Z2 の PS の DAP（PJTAG、independent 起動）を Primer の fprobe_jtag（J12）で止めずに読む（linux.memap.connect_jtag）。

FPROBE_ZYNQ_JTAG=1 で走る。配線・起動は docs/boards/pynq-z2.md「PS の DAP の JTAG」。Primer には fprobe_jtag を SRAM に書いておく。
期待値は 2026-10-31 に PYNQ の Linux（SSH）で読んだ値: BOOT_MODE 0xD（independent）、DEVCFG_CTRL の DAP_EN[2:0] = 7。"""
import os
import types

import pytest

pytestmark = [pytest.mark.hil, pytest.mark.skipif(not os.environ.get("FPROBE_ZYNQ_JTAG"), reason="FPROBE_ZYNQ_JTAG が無い")]


@pytest.fixture(scope="module")
def memap():
    from fprobe.probe import Probe
    p = Probe()
    t = types.SimpleNamespace(memap=None)
    p._lt = lambda target: t                              # Linux の SSH 接続は要らない（口だけ試す）
    p.linux_memap_connect_jtag("z", chain_idcode=0x4BA00477, ap=0, frequency_hz=1_000_000)
    yield t.memap
    t.memap.close()


def test_boot_mode_is_independent(memap):
    assert memap.read_words(0xF800025C, 1) == [0xD]


def test_dap_is_enabled(memap):
    assert memap.read_words(0xF8007000, 1)[0] & 7 == 7


def test_many_words_are_read_in_order(memap):
    assert len(memap.read_words(0x00100000, 130)) == 130
