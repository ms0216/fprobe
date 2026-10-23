from fprobe_gw.rtl import target_ip
import pytest

from fprobe_gw.toolchain.sim import SimulationFailed, run_cocotb

SOURCES = ("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd")
GENERICS = {"GROUPS": 2, "FIFO_DEPTH": 16, "CLOCK_HZ": 50_000_000}


def test_obs_core(tmp_path):
    run_cocotb(toplevel="obs_core", sources=target_ip(*SOURCES), generics=GENERICS,
               test_module="tests.tb.obs_core_tb", build_dir=tmp_path, test_filter="^(?!.*the_lost_count_stops)")


def test_obs_core_lost_count_saturates(tmp_path):
    # 失った数の幅を小さくして（LOST_BITS 4）上限で止まることを見る
    run_cocotb(toplevel="obs_core", sources=target_ip(*SOURCES), generics={**GENERICS, "LOST_BITS": 4},
               test_module="tests.tb.obs_core_tb", build_dir=tmp_path,
               test_filter="the_lost_count_stops|id_and_registers")


@pytest.mark.parametrize("depth, ok", [(8, True), (12, False), (2, False), (65536, False)])
def test_the_fifo_depth_must_be_a_power_of_two_from_4_to_32768(tmp_path, depth, ok):
    # 500 を渡すと実際は 512 なのに 500 を申告し、2 では再同期の空き（4 語）が永遠にそろわなかった。見出しの count は 16 ビット
    def run():
        run_cocotb(toplevel="obs_core", sources=target_ip(*SOURCES), generics={**GENERICS, "FIFO_DEPTH": depth},
                   test_module="tests.tb.obs_core_tb", build_dir=tmp_path,
                   test_filter="the_header_carries_a_mark")
    if ok:
        run()
    else:
        with pytest.raises(SimulationFailed):
            run()
