from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_xtrig_out(tmp_path):
    # 動作 4 本で試す（Primer の構成は 2 本。ベクタの組の位置の取り違えを 2 本より広く確かめる）
    run_cocotb(toplevel="xtrig_out", sources=rtl("core/xtrig_out.vhd"), test_module="tests.tb.xtrig_out_tb",
               generics={"COUNT": 4}, build_dir=tmp_path)


def test_xtrig_out_random(tmp_path):
    run_cocotb(toplevel="xtrig_out", sources=rtl("core/xtrig_out.vhd"), test_module="tests.tb.xtrig_out_random_tb",
               generics={"COUNT": 3}, build_dir=tmp_path)
