"""FPGA への安全な書き込み。

対象は「ボード層が宣言した USB シリアル番号の機器が接続されているポート経路にあり、
FTDI 説明が完全一致するちょうど 1 チャンネル」に限定し、書き込み前に IDCODE を照合する。
同じ説明の機器（他の Tang ボード等）や本プロジェクト対象外の機器（PYNQ-Z2 等）を
誤って操作しないための安全装置であり、条件を緩めてはならない。
"""

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ftdi import FtdiDeviceInfo, list_ftdi_devices, usb_port_chain
from .gowin import GowinToolchain

__all__ = ["GowinProgrammer", "JtagIdentity", "ProgrammerError", "UnsafeTargetError",
           "select_jtag_location"]

# programmer_cli --run の操作番号
OP_READ_IDCODE = 0
OP_SRAM_PROGRAM = 2
OP_FLASH_PROGRAM = 36   # exFlash Erase,Program thru GAO-Bridge（Primer 20K の外部 Flash）

PROGRAMMER_TIMEOUT_S = 300


class UnsafeTargetError(RuntimeError):
    """書き込み対象を安全に一意特定できない。"""


class ProgrammerError(RuntimeError):
    """programmer_cli が失敗した。"""


@dataclass(frozen=True)
class JtagIdentity:
    board: str
    usb_id: int             # 0xVVVVPPPP
    usb_serial: str         # USB のシリアル番号（OS から接続位置を引く）
    ftdi_description: str   # D2XX が返す説明（完全一致で照合）
    gowin_cable: str        # programmer_cli の --cable 名
    gowin_device: str       # programmer_cli の --device 名
    idcode: int
    jtag_frequency: str | None = None   # programmer_cli の --frequency（None は既定値）


def select_jtag_location(devices: Sequence[FtdiDeviceInfo], identity: JtagIdentity,
                         port_chain: tuple[int, ...]) -> int:
    """port_chain: identity.usb_serial の機器の USB ポート経路（usb_port_chain で得る）。"""
    matches = [d for d in devices
               if d.description == identity.ftdi_description and d.usb_id == identity.usb_id
               and d.location != 0 and d.port_chain == port_chain]
    busy = sum(1 for d in devices if d.busy)
    note = f"（他プロセス使用中で識別不能なデバイス {busy} 台は対象外）" if busy else ""
    if not matches:
        raise UnsafeTargetError(
            f"{identity.board} の JTAG（シリアル {identity.usb_serial}、USB ポート経路 "
            f"{'-'.join(map(str, port_chain))}、説明 '{identity.ftdi_description}'）が見つからない{note}")
    if len(matches) > 1:
        locations = ", ".join(str(d.location) for d in matches)
        raise UnsafeTargetError(
            f"{identity.board} の候補が {len(matches)} 台あり一意に決められない（USB 位置 {locations}）")
    (target,) = matches
    return target.location


class GowinProgrammer:
    def __init__(self, toolchain: GowinToolchain, identity: JtagIdentity, *,
                 list_devices: Callable[[], Sequence[FtdiDeviceInfo]] = list_ftdi_devices,
                 find_port_chain: Callable[[int, str], tuple[int, ...]] = usb_port_chain,
                 run=subprocess.run):
        self._toolchain = toolchain
        self._identity = identity
        self._list_devices = list_devices
        self._find_port_chain = find_port_chain
        self._run = run

    def locate(self) -> int:
        chain = self._find_port_chain(self._identity.usb_id, self._identity.usb_serial)
        return select_jtag_location(self._list_devices(), self._identity, chain)

    def _invoke(self, location: int, operation: int, *extra: str) -> str:
        args = [str(self._toolchain.programmer_cli),
                "--device", self._identity.gowin_device,
                "--cable", self._identity.gowin_cable,
                "--location", str(location),
                *(["--frequency", self._identity.jtag_frequency] if self._identity.jtag_frequency else []),
                "--run", str(operation), *extra]
        try:
            result = self._run(args, capture_output=True, text=True, timeout=PROGRAMMER_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise ProgrammerError(
                f"programmer_cli が {PROGRAMMER_TIMEOUT_S} 秒応答しない (op={operation})") from e
        output = (result.stdout or "") + (result.stderr or "")
        failed = (result.returncode != 0 or "Finished." not in output
                  or re.search(r"\b(error|unsupported)\b", output, re.I))
        if failed:
            raise ProgrammerError(f"programmer_cli 失敗 (op={operation}):\n{output.strip()}")
        return output

    def read_idcode(self, location: int) -> int:
        try:
            output = self._invoke(location, OP_READ_IDCODE)
        except ProgrammerError as e:
            raise ProgrammerError(f"IDCODE を読めない（接続・電源を確認）: {e}") from e
        m = re.search(r"ID Code is:\s*0x([0-9A-Fa-f]+)", output)
        if not m:
            raise ProgrammerError(f"IDCODE を読めない:\n{output.strip()}")
        return int(m.group(1), 16)

    def program(self, bitstream: Path, *, persistent: bool = False) -> None:
        # programmer_cli は相対パスを自身の場所基準で解釈するため絶対パスにする
        bitstream = Path(bitstream).resolve()
        if not bitstream.is_file():
            raise FileNotFoundError(bitstream)
        location = self.locate()
        idcode = self.read_idcode(location)
        if idcode != self._identity.idcode:
            raise UnsafeTargetError(
                f"USB 位置 {location} の IDCODE 0x{idcode:08X} が {self._identity.board} の "
                f"0x{self._identity.idcode:08X} と一致しないため中止")
        operation = OP_FLASH_PROGRAM if persistent else OP_SRAM_PROGRAM
        self._invoke(location, operation, "--fsFile", str(bitstream))
