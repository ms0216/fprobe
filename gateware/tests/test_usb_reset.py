from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_usb_reset(tmp_path):
    run_cocotb(toplevel="usb_reset", sources=rtl("usb/usb_timing_pkg.vhd", "usb/usb_reset.vhd"),
               test_module="tests.tb.usb_reset_tb",
               generics={"T_DETACH": 40, "T_RESET_SE0": 10, "T_CHIRP": 50, "T_LINE_STABLE": 5,
                         "T_HOST_CHIRP_TIMEOUT": 300, "T_HS_IDLE": 100, "T_SUSPEND_SAMPLE": 20,
                         "T_FS_IDLE": 400},
               build_dir=tmp_path)
