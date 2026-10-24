"""fprobe.decode.jtag: TAP の状態・IR / DR のシフトと、その上の ARM の JTAG-DP（ハードウェア不要）。"""

import pytest

from fprobe.decode import decode
from fprobe.decode.jtag import decode_jtag, decode_jtag_arm
from fprobe.decode.synth import JtagBus, Wave

TCK, TMS, TDI, TDO, TRST = 0, 1, 2, 3, 4
F = 1e6


def bus(trst=False):
    w = Wave(channels=6, sample_rate_hz=F * 8, idle={TCK: 0, TRST: 1})
    return w, JtagBus(w, tck=TCK, tms=TMS, tdi=TDI, tdo=TDO, clock_hz=F, t=2e-6)


def run(w, b, **kw):
    return decode_jtag(w.build(end_s=b.t + 2e-6), tck=TCK, tms=TMS, tdi=TDI, tdo=TDO, **({"trst": None} | kw))


def test_reset_ir_and_dr_scans():
    w, b = bus()
    b.reset()
    b.shift_ir(0xE, 4, tdo_value=0b0001)
    b.shift_dr(0, 32, tdo_value=0x4BA00477)
    d = run(w, b, ir_names="arm_dp")
    assert len(d.of("reset")) == 1
    (ir,) = d.of("ir")
    assert (ir.value, ir.fields["name"], ir.fields["tdo"], ir.text) == (0xE, "IDCODE", 0b0001, "IR 0xE IDCODE")
    (dr,) = d.of("dr")
    assert (dr.fields["bits"], dr.fields["tdo"], dr.value) == (32, 0x4BA00477, 0)
    states = [a.text for a in d.of("state")]
    assert states[:3] == ["test_logic_reset", "run_test_idle", "select_dr_scan"]
    assert "shift_ir" in states and "update_dr" in states and states[-1] == "run_test_idle"
    assert d.errors() == []


def test_unknown_start_waits_for_five_tms_high_and_pause_joins_a_scan():
    w, b = bus()
    b.shift_dr(0x5, 3)                                       # 状態が分からない間（読まない）
    b.reset()
    b.shift_dr(0x1234_5678, 32, tdo_value=0x9ABC_DEF0, pause_after=12)
    d = run(w, b)
    (dr,) = d.of("dr")
    assert (dr.value, dr.fields["tdo"], dr.fields["bits"]) == (0x1234_5678, 0x9ABC_DEF0, 32)
    assert "pause_dr" in [a.text for a in d.of("state")]


def test_initial_state_given_and_trst_and_truncation():
    w, b = bus()
    b.shift_ir(0xA, 4)
    w.set(TRST, 0, b.t)
    w.set(TRST, 1, b.t + 1e-6)
    b.t += 2e-6
    b.clock(0)
    b.shift_dr(0xFF, 8)
    tr = w.build(end_s=b.t - 3 / F)                         # 最後のシフトの途中で切る
    d = decode_jtag(tr, tck=TCK, tms=TMS, tdi=TDI, tdo=TDO, trst=TRST, initial_state="run_test_idle")
    assert [a.value for a in d.of("ir")] == [0xA]
    assert len(d.of("reset")) == 1
    (dr,) = d.of("dr")
    assert dr.error == "truncated"
    with pytest.raises(ValueError):
        decode_jtag(tr, tck=TCK, tms=TMS, initial_state="nowhere", tdi=None, tdo=None, trst=None)


def test_arm_dp_on_top_reads_dpacc_and_apacc_with_posted_reads():
    w, b = bus()
    b.reset()
    b.shift_ir(0xA, 4)                                       # DPACC
    b.shift_dr((0x0500_0000 << 3) | (0x8 >> 2 << 1) | 0, 35, tdo_value=0b010)          # W SELECT
    b.shift_ir(0xB, 4)                                       # APACC
    b.shift_dr((0 << 3) | (0xC >> 2 << 1) | 1, 35, tdo_value=0b010)                    # R AP 0xC
    b.shift_dr((0 << 3) | (0xC >> 2 << 1) | 1, 35, tdo_value=(0x1122_3344 << 3) | 0b001)  # WAIT
    b.shift_ir(0xA, 4)
    b.shift_dr((0 << 3) | (0xC >> 2 << 1) | 1, 35, tdo_value=(0xCAFE_F00D << 3) | 0b010)  # R RDBUFF
    d = decode("jtag_arm", w.build(end_s=b.t + 2e-6),
               jtag={"tck": TCK, "tms": TMS, "tdi": TDI, "tdo": TDO, "trst": None}, ir_offset=0, dr_offset=0)
    acc = d.of("access")
    assert [a.fields["port"] for a in acc] == ["DP", "AP", "AP", "DP"]
    assert acc[0].text == "DP W SELECT = 0x05000000" and acc[0].error is None
    assert acc[2].error == "wait"
    assert acc[3].fields["previous"] == ["AP", "0xC"] and acc[3].fields["returned"] == 0xCAFE_F00D
    assert d.params["jtag"]["tck"] == TCK
    with pytest.raises(ValueError):
        decode_jtag_arm(d, ir_offset=0, dr_offset=0)


def test_arm_dp_in_a_chain_with_a_bypassed_tap_on_the_tdi_side():
    # STM32F4: TDO 側が DP（IR 4）、TDI 側がバウンダリスキャン（IR 5、BYPASS = 0x1F）
    w, b = bus()
    b.reset()
    b.shift_ir(0x1F << 4 | 0xE, 9)
    b.shift_dr(0, 33, tdo_value=0x4BA00477)
    b.shift_ir(0x1F << 4 | 0xA, 9)
    b.shift_dr((0x0000_00F0 << 3) | (0x8 >> 2 << 1), 36, tdo_value=0b010)
    d = decode("jtag_arm", w.build(end_s=b.t + 2e-6),
               jtag={"tck": TCK, "tms": TMS, "tdi": TDI, "tdo": TDO, "trst": None}, ir_offset=0, dr_offset=0)
    assert d.of("idcode")[0].value == 0x4BA00477
    (acc,) = d.of("access")
    assert acc.text == "DP W SELECT = 0x000000F0"
