"""FTDI D2XX による FT2232 系デバイスの列挙と、USB シリアル番号からの接続位置の特定。

どちらも読み取りのみで、デバイスは開かない（OS とドライバへの問い合わせだけ）。
"""

import ctypes
import re
import subprocess
import sys
from dataclasses import dataclass

__all__ = ["FtdiDeviceInfo", "FtdiUnavailable", "list_ftdi_devices", "usb_port_chain"]


class FtdiUnavailable(RuntimeError):
    """D2XX ドライバ（ftd2xx.dll）が使えない。"""


@dataclass(frozen=True)
class FtdiDeviceInfo:
    index: int
    flags: int
    device_type: int
    usb_id: int
    location: int
    serial: str
    description: str

    @property
    def port_chain(self) -> tuple[int, ...]:
        """USB のポート経路（ルートハブから順）。

        D2XX の位置 ID は 16 進の各桁がバス番号・ポート経路・チャンネル（A=1, B=2）を表す
        （例: 0x2121 = バス 2・ポート 1→2・チャンネル A。実機の Windows の接続位置と照合済み）。
        """
        digits = f"{self.location:x}"
        return tuple(int(d, 16) for d in digits[1:-1])

    @property
    def busy(self) -> bool:
        """他プロセスが開いているデバイスは D2XX から説明もシリアルも読めない。"""
        return not self.serial and not self.description


def _load_d2xx():
    if sys.platform != "win32":
        raise FtdiUnavailable("D2XX 列挙は Windows でのみ対応")
    for name in ("ftd2xx64.dll", "ftd2xx.dll"):
        try:
            return ctypes.WinDLL(name)
        except OSError:
            continue
    raise FtdiUnavailable("ftd2xx.dll を読み込めない（FTDI ドライバ未導入）")


def list_ftdi_devices() -> list[FtdiDeviceInfo]:
    d2xx = _load_d2xx()
    count = ctypes.c_ulong()
    status = d2xx.FT_CreateDeviceInfoList(ctypes.byref(count))
    if status != 0:
        raise FtdiUnavailable(f"FT_CreateDeviceInfoList 失敗 (status={status})")
    devices = []
    for i in range(count.value):
        flags, dtype, usb_id, loc = (ctypes.c_ulong() for _ in range(4))
        handle = ctypes.c_void_p()
        serial = ctypes.create_string_buffer(64)
        desc = ctypes.create_string_buffer(64)
        status = d2xx.FT_GetDeviceInfoDetail(
            ctypes.c_ulong(i), ctypes.byref(flags), ctypes.byref(dtype), ctypes.byref(usb_id),
            ctypes.byref(loc), serial, desc, ctypes.byref(handle))
        if status != 0:
            raise FtdiUnavailable(f"FT_GetDeviceInfoDetail({i}) 失敗 (status={status})")
        devices.append(FtdiDeviceInfo(
            index=i, flags=flags.value, device_type=dtype.value, usb_id=usb_id.value,
            location=loc.value, serial=serial.value.decode("ascii", "replace"),
            description=desc.value.decode("ascii", "replace")))
    return devices


def usb_port_chain(usb_id: int, serial: str) -> tuple[int, ...]:
    """USB ID（0xVVVVPPPP）とシリアル番号の機器が接続されているポート経路を OS から得る。

    接続中の機器だけを対象にする（切断済み機器の記録は使わない）。見つからなければ FtdiUnavailable。
    """
    if sys.platform != "win32":
        raise FtdiUnavailable("USB 接続位置の取得は Windows でのみ対応")
    instance = f"USB\\VID_{usb_id >> 16:04X}&PID_{usb_id & 0xFFFF:04X}\\{serial}"
    script = (f"$d = Get-PnpDevice -PresentOnly -InstanceId '{instance}' -ErrorAction SilentlyContinue; "
              f"if ($d) {{ (Get-PnpDeviceProperty -InstanceId '{instance}' "
              f"-KeyName DEVPKEY_Device_LocationPaths).Data[0] }}")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=30)
    path = result.stdout.strip()
    # 例: PCIROOT(0)#PCI(0701)#PCI(0003)#USBROOT(0)#USB(1)#USB(2)
    ports = re.findall(r"#USB\((\d+)\)", path)
    if result.returncode != 0 or "USBROOT" not in path or not ports:
        raise FtdiUnavailable(f"シリアル番号 {serial!r} の USB 機器が接続されていない")
    return tuple(int(p) for p in ports)
