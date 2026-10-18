"""fprobe.jtag_chain: TAP リセット・IR / DR のシフト・チェーンの走査（JTAG の模型で。機器は使わない）。"""

import pytest
from jtag_fakes import FakeChain, Tap

from fprobe import jtag_chain as jc

DP = 0x4BA00477
STM32_BSC = 0x06433041


def test_a_single_jtag_dp_is_found_with_its_ir_length():
    chain = FakeChain([Tap(4, DP)])
    scan = jc.scan_chain(chain)
    assert [d.idcode for d in scan.devices] == [DP]
    assert scan.chain == [4] and scan.devices[0].ir_length_source == "table"
    assert scan.devices[0].name.startswith("Arm JTAG-DP")
    assert chain.state == "RTI" and scan.tdo == "ok"


def test_stm32_chain_is_ordered_from_the_tdo_side():
    chain = FakeChain([Tap(4, DP), Tap(5, STM32_BSC)])
    scan = jc.scan_chain(chain)
    assert [d.idcode for d in scan.devices] == [DP, STM32_BSC]
    assert scan.chain == [4, 5] and scan.total_ir_length == 9 and not scan.notes
    d = scan.as_dict()
    assert d["jtag_chain"] == [4, 5] and d["devices"][1]["designer"] == "STMicroelectronics"


def test_unknown_devices_ir_lengths_are_measured_from_capture_ir():
    chain = FakeChain([Tap(6, 0x12345679, ir_capture=0b000001), Tap(3, None, ir_capture=0b001)])
    scan = jc.scan_chain(chain)
    assert [d.idcode for d in scan.devices] == [0x12345679, None]       # 2 台目は BYPASS（IDCODE なし）
    assert scan.chain == [6, 3]
    assert [d.ir_length_source for d in scan.devices] == ["measured", "measured"]


def test_ambiguous_capture_ir_is_marked_guessed():
    chain = FakeChain([Tap(4, 0x11111111, ir_capture=0b0101), Tap(3, 0x22222223, ir_capture=0b001)])
    scan = jc.scan_chain(chain)
    assert scan.devices[0].ir_length_source == "guessed"
    assert any("推測" in n for n in scan.notes)


def test_the_scan_never_loads_zero_into_an_ir():
    taps = [Tap(4, DP), Tap(5, STM32_BSC)]
    jc.scan_chain(FakeChain(taps))
    for t in taps:
        assert ("IR", 0) not in t.updates                    # 0 = EXTEST の装置がある


@pytest.mark.parametrize("stuck,expected", [(0, "stuck_low"), (1, "stuck_high")])
def test_stuck_tdo_is_reported(stuck, expected):
    scan = jc.scan_chain(FakeChain([Tap(4, DP)], tdo_stuck=stuck))
    assert scan.tdo == expected and not scan.devices and scan.notes


def test_shift_device_puts_the_others_in_bypass():
    user = Tap(5, STM32_BSC, user={0b00010: (8, 0xA5)})
    taps = [Tap(4, DP), user]
    chain = FakeChain(taps)
    jc.reset_tap(chain)
    out = jc.shift_device(chain, [4, 5], 1, ir=0b00010, dr_value=0x3C, dr_length=8)
    assert out == {"ir_capture": 0b01, "dr_capture": 0xA5}
    assert taps[0].ir == 0b1111 and user.ir == 0b00010
    assert user.updates[-1] == ("DR", 0x3C)
    with pytest.raises(ValueError):
        jc.shift_device(chain, [4, 5], 1, ir=0b100000)
    with pytest.raises(ValueError):
        jc.shift_device(chain, [4, 5], 2, ir=0)


def test_idcode_is_read_through_shift_device():
    chain = FakeChain([Tap(4, DP), Tap(5, STM32_BSC, idcode_instruction=0b00001)])
    jc.reset_tap(chain)
    assert jc.shift_device(chain, [4, 5], 1, ir=0b00001, dr_length=32)["dr_capture"] == STM32_BSC
    assert jc.shift_device(chain, [4, 5], 0, ir=0b1110, dr_length=32)["dr_capture"] == DP


def test_raw_clock_and_bit_text():
    chain = FakeChain([Tap(4, DP)])
    jc.reset_tap(chain)
    tms = jc.bits_from_text("100" + "0" * 31 + "1" + "10")
    tdo = jc.clock(chain, tms, [0] * len(tms))
    assert sum(b << k for k, b in enumerate(tdo[3:35])) == DP
    with pytest.raises(ValueError):
        jc.bits_from_text("012")
    with pytest.raises(ValueError):
        jc.clock(chain, [0, 1], [0])


def test_sequences_are_at_most_64_clocks():
    class Counting(FakeChain):
        def sequence(self, cycles, tms, tdi, capture):
            assert cycles <= 64
            return super().sequence(cycles, tms, tdi, capture)
    jc.scan_chain(Counting([Tap(4, DP)]))
