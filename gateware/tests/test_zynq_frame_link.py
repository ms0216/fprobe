from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_zynq_frame_link(tmp_path):
    run_cocotb(toplevel="zynq_frame_link", sources=rtl("hostlink/zynq_frame_link.vhd"),
               test_module="tests.tb.zynq_frame_link_tb", generics={"MEM_BYTES": 32 * 1024 * 1024},
               build_dir=tmp_path)
