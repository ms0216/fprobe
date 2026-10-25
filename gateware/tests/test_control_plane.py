from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_control_plane(tmp_path):
    run_cocotb(toplevel="control_plane_harness",
               sources=[*rtl("core/csr_bus_pkg.vhd"), CORE_GENERATED / "csr_map_pkg.vhd",
                        *rtl("core/csr_decoder.vhd", "core/control_plane.vhd"),
                        TEST_RTL / "control_plane_harness.vhd"],
               test_module="tests.tb.control_plane_tb", build_dir=tmp_path)


def test_control_plane_fuzz(tmp_path):
    run_cocotb(toplevel="control_plane_harness",
               sources=[*rtl("core/csr_bus_pkg.vhd"), CORE_GENERATED / "csr_map_pkg.vhd",
                        *rtl("core/csr_decoder.vhd", "core/control_plane.vhd"),
                        TEST_RTL / "control_plane_harness.vhd"],
               test_module="tests.tb.control_plane_fuzz_tb", build_dir=tmp_path)
