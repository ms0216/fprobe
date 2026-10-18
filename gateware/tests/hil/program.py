"""HIL の共通部品: 構成をビルドして Primer の SRAM に書き込み、fprobe が USB で応答するまで待つ。"""

import subprocess
import sys
import time
import warnings

from fprobe_gw.usb.fprobe_device import STR_SERIAL, STRINGS

SERIAL = STRINGS[STR_SERIAL - 1]


def _wait_responsive(timeout_s: float) -> bool:
    """VID/PID で現れるのを待ち（列挙中の機器には触らない）、落ち着いてから開いてケイパビリティを読む。"""
    import usb.core
    from libusb_package import get_libusb1_backend

    from fprobe import generated as g
    from fprobe.session import Session

    deadline = time.monotonic() + timeout_s
    while usb.core.find(idVendor=g.USB_VID, idProduct=g.USB_PID, backend=get_libusb1_backend()) is None:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    while time.monotonic() < deadline:
        time.sleep(1.0)
        try:
            with Session.open(SERIAL) as s:
                s.device.capabilities()
            return True
        except Exception:                                   # noqa: BLE001
            pass
    return False


def program(design: str = "fprobe", *, timeout_s: float = 10.0) -> None:
    """書き込み直後の USB の列挙がまれに失敗する（文字列記述子が読めないまま。既知の問題:
    docs/reports/2026-10-16-day.md）。応答しなければ 1 回だけ書き込み直し、そのことを警告として残す。"""
    for attempt in range(2):
        subprocess.run([sys.executable, "-m", "fprobe_gw", "build", design, "--program"], check=True)
        if _wait_responsive(timeout_s):
            return
        warnings.warn(f"{design} を書き込んだ後 fprobe が応答しない（{attempt + 1} 回目）", stacklevel=2)
    raise TimeoutError(f"{design} を 2 回書き込んでも fprobe が応答しない")
