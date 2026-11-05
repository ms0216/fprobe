"""dap_sampler から分けた部品（周期の時計 dap_sampler_timer・件の出力 dap_sampler_out）の単体試験。
dap_sampler 全体の振る舞いは test_dap_processor.py の test_dap_sampler が確かめる。"""

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_dap_sampler_timer(tmp_path):
    run_cocotb(toplevel="dap_sampler_timer", sources=rtl("debug/dap_sampler_timer.vhd"),
               test_module="tests.tb.dap_sampler_timer_tb", build_dir=tmp_path)


def test_dap_sampler_out(tmp_path):
    run_cocotb(toplevel="dap_sampler_out",
               sources=rtl("core/generated/event_pkg.vhd", "debug/dap_pkg.vhd", "debug/dap_sampler_pkg.vhd",
                           "debug/dap_sampler_out.vhd"),
               test_module="tests.tb.dap_sampler_out_tb", build_dir=tmp_path, generics={"CHECK_MAX": 4})
