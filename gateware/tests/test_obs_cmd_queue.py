import pytest

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


@pytest.mark.parametrize("depth", [4, 3])
def test_obs_cmd_queue(tmp_path, monkeypatch, depth):
    monkeypatch.setenv("QUEUE_DEPTH", str(depth))
    run_cocotb(toplevel="obs_cmd_queue", sources=rtl("debug/obs_cmd_queue.vhd"), generics={"DEPTH": depth},
               test_module="tests.tb.obs_cmd_queue_tb", build_dir=tmp_path)
