"""実機: PYNQ-Z2（Zynq-7000、Cortex-A9 × 2）の PS の DAP を、Primer の JTAG（fprobe_jtag）で止めて読む。

FPROBE_ZYNQ_JTAG=1 で走る。配線は docs/boards/pynq-z2.md「PS の DAP の JTAG」、PYNQ は independent で起動
（JTAG_JP1 の MODE3。MIO2 = 1）し、zynq_dut の回路（PS の PJTAG を端子へ素通し）が載っていること。
操作はデーモン（--lan、7820）経由で行う（Primer の USB をデーモンが握っているため。画面の操作の記録に ai として残る）。
FPROBE_ZYNQ_SSH（例 xilinx@192.168.77.2）と FPROBE_ZYNQ_KEY があれば、最後に SSH で PYNQ が生きていることを確かめる。
止めるのはコア 1 だけ（Linux のコア 0 は走らせ続ける）。どの経路でも最後に再開する。"""
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FPROBE_ZYNQ_JTAG"), reason="FPROBE_ZYNQ_JTAG が無い")
CORE = 1
LINEAR = 0xC0008000                 # カーネルの線形の写しの頭（PAGE_OFFSET 0xC0000000 + 物理 0x8000）
ZYNQ_DAP_IDCODE = 0x4BA00477


def _alive() -> None:
    who, key = os.environ.get("FPROBE_ZYNQ_SSH"), os.environ.get("FPROBE_ZYNQ_KEY")
    if not who:
        return
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *(["-i", key] if key else []), who, "uptime"]
    r = subprocess.run(args, capture_output=True, timeout=30)
    print("pynq:", r.stdout.decode().strip())
    assert r.returncode == 0


@pytest.fixture(scope="module")
def daemon():
    from fprobe.api.client import DaemonClient
    url = os.environ.get("FPROBE_DAEMON_URL", "ws://127.0.0.1:7820/api")
    with DaemonClient(url, "claude", "ai", token=os.environ.get("FPROBE_DAEMON_TOKEN"), source="hil-zynq") as c:
        yield c


def test_scan_finds_the_zynq_dap(daemon):
    scan = daemon.call("jtag.scan", frequency_hz=2_000_000)
    ids = [d["idcode"] & 0x0FFFFFFF for d in scan["devices"]]
    assert ids == [ZYNQ_DAP_IDCODE & 0x0FFFFFFF], (
        f"{scan}: PS の DAP が見えない。PYNQ が independent（MIO2 = 1）で起動したか、zynq_dut の回路が書かれているか、"
        "JTAG の 5 本の配線を確かめる")


def test_ap_map_is_recorded(daemon):
    """AP 0〜3 の IDR を表に出す（AHB-AP の有無を調べる試験。assert はしない。結果は spec に書き戻す）。"""
    daemon.call("target.connect", target="zynq7000", frequency_hz=2_000_000, connect_mode="attach", core=CORE)
    try:
        for ap in range(4):
            try:
                idr = daemon.call("target.ap.read", ap=ap, register=0xFC)
            except Exception as e:                  # 無い AP の読みは断られてよい
                print(f"AP{ap}: {e}")
                continue
            print(f"AP{ap}: IDR {idr}")
    finally:
        daemon.call("target.disconnect")


def test_halt_and_resume_core1_only_and_linux_stays_alive(daemon):
    daemon.call("target.connect", target="zynq7000", frequency_hz=2_000_000, connect_mode="attach", core=CORE)
    try:
        daemon.call("target.halt")
        status = daemon.call("target.status")
        assert status["state"] == "HALTED", status
        regs = daemon.call("target.registers.read", names=["pc", "cpsr"])
        print("core1:", regs)
    finally:
        daemon.call("target.resume")
        daemon.call("target.disconnect")
    _alive()


def test_memory_read_while_halted(daemon):
    daemon.call("target.connect", target="zynq7000", frequency_hz=2_000_000, connect_mode="attach", core=CORE)
    try:
        daemon.call("target.halt")
        data = daemon.call("target.memory.read", address=LINEAR, length=256)
        assert len(bytes.fromhex(data["data"] if isinstance(data, dict) else data)) == 256
    finally:
        daemon.call("target.resume")
        daemon.call("target.disconnect")
    _alive()
