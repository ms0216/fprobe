from fprobe_gw import ps_window
from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_ps_window_port(tmp_path):
    """窓 + la_ps_port: 位置の約束（流れの番号で 0 に戻す・古い番号の書きを捨てる・読みで写す）。"""
    run_cocotb(toplevel="ps_window_harness",
               sources=[CORE_GENERATED / "ps_window_pkg.vhd", *rtl("hostlink/zynq_frame_link.vhd", "la/la_ps_port.vhd"),
                        TEST_RTL / "ps_window_harness.vhd"],
               test_module="tests.tb.ps_window_tb", build_dir=tmp_path)


def test_the_register_table_is_consistent():
    regs = ps_window.REGS
    assert len({r.addr for r in regs}) == len(regs) and all(r.addr % 4 == 0 for r in regs)
    port = [r for r in regs if ps_window.PORT_FIRST <= r.addr <= ps_window.PORT_LAST]
    assert {r.name for r in port} >= {"STREAM_EPOCH", "LA_CONSUMED", "POS_TAG", "STALE_WRITES", "LA_WPTR"}
    assert all(r.addr < ps_window.PORT_FIRST or r.addr > ps_window.PORT_LAST or r in port for r in regs)
