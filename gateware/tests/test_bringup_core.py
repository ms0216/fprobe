import pytest

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import SimulationFailed, run_cocotb

SOURCES = rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/sync_fifo.vhd",
              "serial/uart_tx.vhd", "serial/uart_rx.vhd", "designs/bringup_core.vhd")


def test_bringup_uart_behaviour(tmp_path):
    run_cocotb(toplevel="bringup_core", sources=SOURCES, test_module="tests.tb.bringup_core_tb",
               generics={"SYS_CLK_HZ": 10_000_000, "BAUD": 100_000, "REVISION": 3714,
                         "ECHO_FIFO_DEPTH": 8},
               build_dir=tmp_path)


def test_bringup_heartbeat(tmp_path):
    run_cocotb(toplevel="bringup_core", sources=SOURCES,
               test_module="tests.tb.bringup_heartbeat_tb",
               generics={"SYS_CLK_HZ": 10_000, "BAUD": 100, "REVISION": 0}, build_dir=tmp_path)


def test_too_high_baud_for_clock_is_rejected(tmp_path):
    # 分周比が小さすぎて「送信を公称より速く」できない設定は elaboration で拒否する
    with pytest.raises(SimulationFailed):
        run_cocotb(toplevel="bringup_core", sources=SOURCES,
                   test_module="tests.tb.bringup_heartbeat_tb",
                   generics={"SYS_CLK_HZ": 1_000_000, "BAUD": 125_000, "REVISION": 0},
                   build_dir=tmp_path)
