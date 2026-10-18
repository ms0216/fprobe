from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_stream_slice(tmp_path):
    run_cocotb(toplevel="stream_slice", sources=rtl("common/stream_slice.vhd"),
               test_module="tests.tb.stream_slice_tb", generics={"WIDTH": 8}, build_dir=tmp_path)
