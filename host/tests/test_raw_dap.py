"""fprobe.raw_dap: 線の選択・DP / AP の直接の読み書き・MEM-AP・誤りの種類（偽の CMSIS-DAP の層で）。"""

import pytest
from coresight_fakes import stm32f401_words
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget

from fprobe.raw_dap import Ack, DapError, RawDap, dp_address


def started(target=None, dormant=False):
    link = FakeDapLink(target)
    dap = RawDap(link)
    dap.start("swd", 1_000_000)
    dap.switch_to_swd(dormant=dormant)
    return dap, link


def test_dpidr_is_read_after_switching_to_swd():
    dap, _ = started()
    assert dap.read_dp("DPIDR") == 0x2BA01477 and not dap.adi_v6


def test_without_the_switch_there_is_no_ack():
    link = FakeDapLink()
    dap = RawDap(link)
    dap.start("swd", 1_000_000)
    with pytest.raises(DapError) as e:
        dap.read_dpidr()
    assert e.value.ack == Ack.NO_ACK and "応答が無い" in str(e.value)


def test_dormant_only_targets_need_the_dormant_wakeup():
    dap, _ = started(FakeTarget(dormant_only=True))
    with pytest.raises(DapError):
        dap.read_dpidr()
    dap.switch_to_swd(dormant=True)
    assert dap.read_dpidr() == 0x2BA01477


def test_multidrop_needs_targetsel_and_banked_registers_are_selected():
    target = FakeTarget(dpidr=0x0BC12477, targetid=0x01002927, multidrop=True)
    dap, link = started(target, dormant=True)
    with pytest.raises(DapError):
        dap.read_dpidr()
    dap.targetsel(0x01002927)
    assert dap.read_dpidr() == 0x0BC12477
    assert dap.read_dp("TARGETID") == 0x01002927 and link.select & 0xF == 2
    assert dap.read_dp(0x4) == 0 and link.select & 0xF == 0               # バンク 0 に戻る
    assert "targetsel 0x01002927" in link.log


def test_power_up_and_ap_and_memory_access():
    ap = FakeMemAp(words=stm32f401_words())
    dap, link = started(FakeTarget(aps={0: ap}))
    dap.read_dpidr()
    with pytest.raises(DapError) as e:                                       # 電源を上げる前の AP は FAULT
        dap.read_ap(0, 0xFC)
    assert e.value.ack == Ack.FAULT
    dap.clear_errors()
    power = dap.power_up()
    assert power["debug_power"] and power["system_power"]
    assert dap.read_ap(0, 0xFC) == 0x24770011 and link.select >> 24 == 0 and link.select & 0xF0 == 0xF0
    assert dap.read_ap(1, 0xFC) == 0                                         # 無い AP
    assert dap.mem_read32(0, 0xE000ED00) == 0x410FC241
    assert ap.csw & 0x7 == 2 and ap.csw & 0x40                               # 32 ビット・DeviceEn は保つ
    dap.mem_write32(0, 0x20000000, 0x12345678)
    assert ap.words[0x20000000] == 0x12345678


def test_faults_are_reported_and_cleared():
    dap, link = started()
    dap.read_dpidr()
    dap.power_up()
    with pytest.raises(DapError) as e:
        dap.mem_read32(0, 0x60000000)
    assert e.value.ack == Ack.FAULT and link.sticky
    dap.clear_errors()
    assert not link.sticky


def test_wait_and_parity_errors_have_their_own_kinds():
    dap, _ = started(FakeTarget(stuck_wait=True))
    dap.read_dpidr()
    dap.power_up()
    with pytest.raises(DapError) as e:
        dap.read_ap(0, 0xFC)
    assert e.value.ack == Ack.WAIT
    dap, _ = started(FakeTarget(parity_errors=True))
    with pytest.raises(DapError) as e:
        dap.read_dpidr()
    assert e.value.ack == Ack.PROTOCOL


def test_adiv6_ap_addresses_go_into_select():
    ap = FakeMemAp(words={0xE000ED00: 0x410FD210}, base=0)
    target = FakeTarget(dpidr=0x4C013477, adi_v6=True, baseptr=0x00000003, aps={0x2000: ap})
    dap, link = started(target)
    assert dap.read_dpidr() and dap.adi_v6
    dap.power_up()
    assert dap.read_dp(0x20) == 3                                            # BASEPTR0（バンク 2 の 0x0）
    assert dap.read_ap(0x2000, 0xDFC) == ap.idr and link.select & 0xFFFFFFF0 == 0x2DF0
    assert dap.mem_read32(0x2000, 0xE000ED00) == 0x410FD210


def test_jtag_dp_transfers_use_the_chain_position():
    target = FakeTarget(swd=False, jtag_dp_index=1)
    link = FakeDapLink(target)
    dap = RawDap(link)
    dap.start("jtag", 1_000_000, jtag_chain=[5, 4], dap_index=1)
    assert link.jtag_irlens == [5, 4]
    assert dap.read_dpidr() == 0x2BA01477
    dap.power_up()
    dap.clear_errors()


def test_dp_register_names_and_addresses():
    assert dp_address("ctrl/stat") == 4 and dp_address("TARGETID") == 0x24 and dp_address("0x34") == 0x34
    for bad in ("FOO", 0x3, 0x100):
        with pytest.raises(ValueError):
            dp_address(bad)
    dap, _ = started()
    with pytest.raises(ValueError):
        dap.read_dp(0x28)                                                    # 0x8 にはバンクが無い
    with pytest.raises(ValueError):
        dap.read_ap(256, 0)
