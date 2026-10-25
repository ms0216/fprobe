from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_event_collector(tmp_path):
    run_cocotb(toplevel="event_collector", sources=rtl("core/event_collector.vhd"),
               test_module="tests.tb.event_collector_tb", build_dir=tmp_path)
