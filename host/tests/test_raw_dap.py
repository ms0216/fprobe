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


def test_jtag_faults_are_found_through_ctrl_stat():
    """JTAG-DP の AP の誤りは応答に出ず STICKYERR だけ: 読んだ値と同じ往復で CTRL/STAT を見て、誤りなら値を返さない"""
    ap = FakeMemAp(words={0x20000000: 0x11111111, 0x20000004: 0x22222222})
    link = FakeDapLink(FakeTarget(swd=False, jtag_dp_index=0, aps={0: ap}))
    dap = RawDap(link)
    dap.start("jtag", 1_000_000, jtag_chain=[4], dap_index=0)
    dap.read_dpidr()
    dap.power_up()
    assert dap.mem_read32(0, 0x20000000) == 0x11111111
    with pytest.raises(DapError) as e:
        dap.mem_read32(0, 0x60000000)
    assert e.value.ack == Ack.FAULT and "CTRL/STAT" in str(e.value) and not link.sticky   # 印は消してある
    assert dap.mem_read32(0, 0x20000004) == 0x22222222
    with pytest.raises(DapError):
        dap.mem_read32_repeat(0, 0x60000000, 3)
    assert dap.mem_read32_repeat(0, 0x20000000, 2) == [0x11111111, 0x11111111]


def test_switch_to_jtag_leaves_the_tap_in_run_test_idle():
    """fprobe の jtag_phy（と CMSIS-DAP の DAP_Transfer）は TAP が Run-Test/Idle にいる前提。TMS = 1 の列で終えると Test-Logic-Reset に
    残り、最初の転送が崩れた（実機の Pi 2B で DPIDR が 0xFFFFFFFF、2026-10-29）。TMS = 1 の列の後に TMS = 0 を 1 回。"""
    link = FakeDapLink(FakeTarget(swd=False, jtag_dp_index=0))
    dap = RawDap(link)
    dap.start("jtag", 1_000_000, jtag_chain=[4], dap_index=0)
    dap.switch_to_jtag()
    tms = "".join(format(bits, "0%db" % n)[::-1] for n, bits in link.swj_log)     # 時間順の TMS
    assert tms.endswith("1" * 5 + "0")


def test_many_memory_words_go_in_one_round_trip_and_jtag_faults_are_still_found():
    """複数の番地の読み書きを 1 回の往復（flush）にまとめる（複数のコアへの停止要求をほぼ同時に出すため）。JTAG の誤りは CTRL/STAT で見る"""
    ap = FakeMemAp(words={0x20000000: 0x11111111, 0x20000004: 0x22222222})
    link = FakeDapLink(FakeTarget(swd=False, jtag_dp_index=0, aps={0: ap}))
    dap = RawDap(link)
    dap.start("jtag", 1_000_000, jtag_chain=[4], dap_index=0)
    dap.read_dpidr()
    dap.power_up()
    dap.mem_read32(0, 0x20000000)                                    # CSW・SELECT は先に済ませておく
    before = link.flushes
    dap.mem_write32_many(0, [(0x20000000, 0xA), (0x20000004, 0xB)])
    assert link.flushes - before == 1 and (ap.words[0x20000000], ap.words[0x20000004]) == (0xA, 0xB)
    before = link.flushes
    assert dap.mem_read32_many(0, [0x20000004, 0x20000000]) == [0xB, 0xA]
    assert link.flushes - before == 1
    with pytest.raises(DapError):
        dap.mem_read32_many(0, [0x20000000, 0x60000000])
    assert dap.mem_write32_many(0, []) is None and dap.mem_read32_many(0, []) == []


@pytest.mark.parametrize("protocol", ["jtag", "swd"])
def test_writes_then_a_repeated_read_are_one_round_trip_over_jtag_and_counted(protocol):
    """書きの後に同じ番地を繰り返し読む（コアの DCC の速いモード）: JTAG は 1 回の往復（誤りの確かめも同じ往復）。SWD は書きと
    読みを分ける（値は同じ）。round_trips は RawDap が送った転送の往復を数える（計測用）。"""
    ap = FakeMemAp(words={0x20000000: 0x11111111})
    link = FakeDapLink(FakeTarget(swd=protocol == "swd", jtag_dp_index=0, aps={0: ap}))
    dap = RawDap(link)
    dap.start(protocol, 1_000_000, jtag_chain=[4], dap_index=0) if protocol == "jtag" else dap.start("swd", 1_000_000)
    if protocol == "swd":
        dap.switch_to_swd()
    dap.read_dpidr()
    dap.power_up()
    dap.mem_read32(0, 0x20000000)
    before, counted = link.flushes, dap.round_trips
    assert dap.mem_write32_then_read_repeat(0, [(0x20000004, 0x5)], 0x20000000, 3) == [0x11111111] * 3
    assert ap.words[0x20000004] == 0x5
    if protocol == "jtag":
        assert link.flushes - before == dap.round_trips - counted == 1
        with pytest.raises(DapError):
            dap.mem_write32_then_read_repeat(0, [(0x20000004, 0x6)], 0x60000000, 2)


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


def test_jtag_idle_cycles_are_set_only_when_given_and_after_the_connect():
    link = FakeDapLink()
    dap = RawDap(link)
    dap.start("jtag", 4_000_000, [4], jtag_idle_cycles=8)
    assert link.transfer_idle_cycles == [8]
    dap.start("jtag", 4_000_000, [4])
    assert link.transfer_idle_cycles == [8]                      # 与えなければ触らない（pyOCD の既定のまま）


def test_idle_cycles_are_for_jtag_only():
    with pytest.raises(ValueError):
        RawDap(FakeDapLink()).start("swd", 1_000_000, jtag_idle_cycles=8)


def test_starting_jtag_puts_the_tap_in_run_test_idle_so_the_first_transfer_does_not_assume_a_state_the_pins_changed():
    # DAP_Connect で線を解放して取り直すと、TMS のプルアップ等で TAP の状態が変わりうる（PYNQ の PMOD で Pi 2B を読むと、IR スキャンが
    # Shift-IR に入らず DPIDR が 0xFFFFFFFF になった）。start の最後に TMS = 1 を 6 回 → TMS = 0 で Run-Test/Idle に置く
    link = FakeDapLink()
    calls = []
    real = link.jtag_sequence
    link.jtag_sequence = lambda cycles, tms, read_tdo, tdi: (calls.append((cycles, tms)), real(cycles, tms, read_tdo, tdi))[1]
    RawDap(link).start("jtag", 1_000_000, [4])
    assert calls[-2:] == [(6, 1), (1, 0)]


def test_starting_swd_sends_no_jtag_sequence():
    link = FakeDapLink()
    calls = []
    link.jtag_sequence = lambda *a: calls.append(a)
    RawDap(link).start("swd", 1_000_000)
    assert calls == []
