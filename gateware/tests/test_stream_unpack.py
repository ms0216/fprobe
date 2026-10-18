from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_stream_unpack(tmp_path):
    run_cocotb(toplevel="stream_unpack", sources=rtl("common/stream_unpack.vhd"), generics={"BYTES": 4},
               test_module="tests.tb.stream_unpack_tb", build_dir=tmp_path)
