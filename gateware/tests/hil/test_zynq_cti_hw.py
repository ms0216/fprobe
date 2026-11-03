"""実機: Primer の JTAG（PJTAG）で、PYNQ-Z2 の Cortex-A9 × 2 を CTI で同時に止める（`ArmACores` + `plugins.zynq_cti`）。

FPROBE_ZYNQ_JTAG=1 で走る。配線は docs/boards/pynq-z2.md「PS の DAP の JTAG」。PYNQ は independent で起動し、Primer には fprobe_jtag
が載っていること。**Linux 全体が止まる**（両コアを止める）ので、各回すぐ再開する。止まった時間差の上限は、この試験では測らない
（目標の側で測る。ここで渡す 1000 ns は報告に載るだけの引数）。CTI は AHB-AP のシステムのアドレス（apb_offset = 0）で触る。
"""
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FPROBE_ZYNQ_JTAG"), reason="FPROBE_ZYNQ_JTAG が無い")
ROUNDS = int(os.environ.get("FPROBE_ZYNQ_ROUNDS", "20"))                      # 連続試験は 500 などを入れる（2026-11-01: 500/500）
JTAG_HZ = 2_000_000


@pytest.fixture()
def cores():
    from fprobe.arm_a_target import arm_a_part
    from fprobe.linux_halt.backend_arma import ArmACores
    from fprobe.session import Session
    session = Session.open(None)
    a = ArmACores.open(session.dap_access(), arm_a_part("zynq7000"), frequency_hz=JTAG_HZ,
                       page_offset=0xC0000000, phys_offset=0, linear_end=0xE0000000)
    try:
        yield a
    finally:
        try:
            if any(a.halted()):                       # 試験が途中で落ちても、止まったまま残さない
                a.resume_all()
        finally:
            a.close()
            session.close()


def test_cti_halts_both_cores_with_one_request_every_time(cores):
    import time
    from fprobe.linux_halt.cti import ApbIo
    from fprobe.plugins import zynq_cti
    cross = zynq_cti.cross_halt(zynq_cti.ZYNQ7000, ApbIo(cores._dap, cores.debug_ap), apb_offset=zynq_cti.ZYNQ7000.apb_offset_for(cores.discovery), ack_timeout_polls=100)
    cores.use_cross_halt(cross, request_skew_upper_bound_ns=1000, event_skew_upper_bound_ns=None, event_trigin=None,
                         hardware_halt_wait_s=0.05)
    for _ in range(ROUNDS):
        report = cores.halt_all()
        assert report.method == "simultaneous" and report.cores_halted == (0, 1), report
        cores.resume_all()
        assert cores.halted() == (False, False)
        time.sleep(0.2)
