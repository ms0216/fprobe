"""obs_soft_tap（obs_soft_core 経由、1 クロック版・2 クロック版）を IEEE 1149.1 の 16 状態の基準モデルとランダムな TMS 列で
突き合わせる（状態・TDO・tdo_oe。IR・IDCODE・BYPASS のシフトと Capture/Update を含む）。"""
import random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles
from tests.tb.obs_soft_core_tb import Jtag

NEXT = {
 "TLR":("RTI","TLR"),"RTI":("RTI","SELD"),"SELD":("CAPD","SELI"),"CAPD":("SHD","EX1D"),"SHD":("SHD","EX1D"),
 "EX1D":("PAD","UPD"),"PAD":("PAD","EX2D"),"EX2D":("SHD","UPD"),"UPD":("RTI","SELD"),
 "SELI":("CAPI","TLR"),"CAPI":("SHI","EX1I"),"SHI":("SHI","EX1I"),"EX1I":("PAI","UPI"),"PAI":("PAI","EX2I"),
 "EX2I":("SHI","UPI"),"UPI":("RTI","SELD")}
CODE = {"TLR":0xF,"RTI":0xC,"SELD":7,"CAPD":6,"SHD":2,"EX1D":1,"PAD":3,"EX2D":0,"UPD":5,"SELI":4,"CAPI":0xE,"SHI":0xA,
        "EX1I":9,"PAI":0xB,"EX2I":8,"UPI":0xD}


@cocotb.test()
async def random_tms(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.rst.value = 1
    dut.probes.value = 0
    j = Jtag(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    rng = random.Random(1)
    ir = 1; irs = 0; ids = 0; byp = 0
    for _ in range(6):
        await j.clock(1)
    st = "TLR"
    errs = 0
    for n in range(4000):
        tms = rng.randrange(2)
        tdi = rng.randrange(2)
        exp_oe = st in ("SHI", "SHD")
        if st == "SHI":
            exp = irs & 1
        elif st == "SHD":
            exp = (ids & 1) if ir == 1 else (byp if ir != 2 else None)
        else:
            exp = None
        tdo = await j.clock(tms, tdi)
        if (tdo is not None) != exp_oe:
            errs += 1
            dut._log.error(f"n={n} oe mismatch st={st} tdo={tdo}")
        elif exp is not None and tdo != exp:
            errs += 1
            dut._log.error(f"n={n} tdo st={st} exp={exp} got={tdo}")
        if st == "TLR":
            ir = 1
        elif st == "CAPI":
            irs = 1
        elif st == "SHI":
            irs = (tdi << 3) | (irs >> 1)
        elif st == "UPI":
            ir = irs
        if st == "CAPD":
            ids = 0x0F0B5001; byp = 0
        elif st == "SHD":
            ids = (tdi << 31) | (ids >> 1); byp = tdi
        st = NEXT[st][tms]
        real = int((dut.one_g.tap if hasattr(dut, "one_g") else dut.two_g.tap).state.value)
        if real != CODE[st]:
            errs += 1
            dut._log.error(f"n={n} state exp={st} real={real:x}")
        if errs > 10:
            break
    assert errs == 0
