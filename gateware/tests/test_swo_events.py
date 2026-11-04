import pytest

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


@pytest.mark.parametrize("pack", [False, True], ids=["one_byte", "packed"])
def test_swo_events(tmp_path, monkeypatch, pack):
    monkeypatch.setenv("SWO_EVENTS_PACK", "1" if pack else "0")
    run_cocotb(toplevel="swo_events_harness",
               sources=[*rtl("core/generated/event_pkg.vhd", "common/util_pkg.vhd", "common/sync_bit.vhd", "serial/uart_rx.vhd", "debug/swo_manchester_rx.vhd",
                             "debug/swo_receiver.vhd", "core/timebase.vhd", "core/event_byte_pack.vhd",
                             "core/event_collector.vhd"), TEST_RTL / "swo_events_harness.vhd"],
               test_module="tests.tb.swo_events_tb", generics={"BYTE_PACK": pack}, build_dir=tmp_path)
