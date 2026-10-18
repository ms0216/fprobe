"""標準ツール互換: OpenOCD（CMSIS-DAP v2）で fprobe 経由の Nucleo を操作する。

- 25 MHz でコアを識別する
- Flash の書き込み・検証（最後のセクタ 0x08060000 だけを使い、元の内容を退避して必ず戻す）
- GDB サーバ（リモートシリアルプロトコル）でレジスタとメモリを読む
OpenOCD は fprobe の VID/PID とシリアルだけを対象にする（ST-LINK や他の機器には触れない）。
"""

import glob
import os
import random
import shutil
import socket
import subprocess
import time

import pytest

from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil


def _openocd() -> str | None:
    found = shutil.which("openocd") or next(iter(glob.glob(os.path.expanduser(
        "~/AppData/Local/Microsoft/WinGet/Packages/xpack-dev-tools.openocd-xpack*/*/bin/openocd.exe"))), None)
    return found


OPENOCD = _openocd()
if OPENOCD is None:
    pytest.skip("OpenOCD が無い", allow_module_level=True)
SCRIPTS = os.path.join(os.path.dirname(OPENOCD), "..", "openocd", "scripts")
ADAPTER = (f"adapter driver cmsis-dap; cmsis-dap vid_pid 0x1209 0x0001; adapter serial {SERIAL}; "
           "cmsis-dap backend usb_bulk; transport select swd")
SECTOR, SECTOR_SIZE = 0x08060000, 128 * 1024        # STM32F401RE の最後のセクタ（7）


def openocd(commands: str, *, speed_khz: int = 8000, timeout: float = 120) -> str:
    """コマンドの結果はバッチ実行では表示されないので、見たいものは echo [capture {...}] で出す。"""
    result = subprocess.run([OPENOCD, "-s", SCRIPTS, "-c", ADAPTER, "-f", "target/stm32f4x.cfg",
                             "-c", f"adapter speed {speed_khz}; init; {commands}; shutdown"],
                            capture_output=True, text=True, timeout=timeout)
    assert "shutdown command invoked" in result.stderr + result.stdout, result.stderr[-2000:]
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


def test_identifies_the_core_at_25mhz(programmed):
    out = openocd("halt; echo [mdw 0xE000ED00]; echo [mdw 0xE0042000]; resume", speed_khz=25000)
    assert "clock speed 25000 kHz" in out
    assert "0xe000ed00: 410fc241" in out                       # CPUID: Cortex-M4 r0p1
    assert "0xe0042000: 10016433" in out                       # DBGMCU: STM32F401xD/E


def test_flash_write_verify_and_restore(programmed, tmp_path):
    backup, image = tmp_path / "backup.bin", tmp_path / "image.bin"
    image.write_bytes(random.Random(7).randbytes(4096))
    openocd(f"reset halt; dump_image {backup.as_posix()} {SECTOR:#x} {SECTOR_SIZE}")
    assert backup.stat().st_size == SECTOR_SIZE
    try:
        out = openocd(f"reset halt; echo [capture {{flash write_image erase {image.as_posix()} {SECTOR:#x}}}]; "
                      f"echo [capture {{verify_image {image.as_posix()} {SECTOR:#x}}}]")
        assert "verified 4096 bytes" in out, out[-2000:]
    finally:
        out = openocd(f"reset halt; echo [capture {{flash write_image erase {backup.as_posix()} {SECTOR:#x}}}]; "
                      f"echo [capture {{verify_image {backup.as_posix()} {SECTOR:#x}}}]; reset run")
        assert f"verified {SECTOR_SIZE} bytes" in out, out[-2000:]


def _rsp(sock: socket.socket, packet: str) -> str:
    """GDB リモートシリアルプロトコルの 1 往復（ack モード）。"""
    payload = packet.encode()
    sock.sendall(b"$" + payload + b"#" + f"{sum(payload) & 0xFF:02x}".encode())
    data = b""
    while not (b"#" in data and len(data) >= data.index(b"#") + 3 and b"$" in data):
        data += sock.recv(4096)
    sock.sendall(b"+")
    start = data.index(b"$")
    return data[start + 1:data.index(b"#", start)].decode()


def test_gdb_server_reads_registers_and_memory(programmed):
    proc = subprocess.Popen([OPENOCD, "-s", SCRIPTS, "-c", ADAPTER, "-f", "target/stm32f4x.cfg",
                             "-c", "adapter speed 8000; gdb_port 3333; init; halt"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                sock = socket.create_connection(("127.0.0.1", 3333), timeout=5)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        with sock:
            assert "PacketSize" in _rsp(sock, "qSupported")
            assert _rsp(sock, "?").startswith(("S", "T"))           # 停止中
            regs = _rsp(sock, "g")
            pc = int.from_bytes(bytes.fromhex(regs[15 * 8:16 * 8]), "little")
            assert 0x08000000 <= pc < 0x08080000
            assert _rsp(sock, "me000ed00,4") == "41c20f41"           # CPUID（リトルエンディアン）
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    openocd("halt; resume")


@pytest.mark.parametrize("speed_khz", [1000, 8000])
def test_jtag_debug_with_openocd(programmed, speed_khz):
    # 成功基準「OpenOCD で JTAG 経由の Cortex-M デバッグ」。stm32f4x.cfg が JTAG のチェーン（Cortex-M4 の JTAG-DP
    # IR 4、バウンダリスキャン IR 5）を定義する。次の試験は SWD で接続し直す（JTAG → SWD の切り替えを送る）
    adapter = ADAPTER.replace("transport select swd", "transport select jtag")
    result = subprocess.run([OPENOCD, "-s", SCRIPTS, "-c", adapter, "-f", "target/stm32f4x.cfg",
                             "-c", f"adapter speed {speed_khz}; init; halt; "
                                   "echo [mdw 0xE000ED00]; mww 0x20004000 0x5A5AC3C3; echo [mdw 0x20004000]; "
                                   "resume; shutdown"],
                            capture_output=True, text=True, timeout=120)
    out = result.stdout + result.stderr
    assert "tap/device found: 0x4ba00477" in out.lower(), out[-2000:]
    assert "0xe000ed00: 410fc241" in out and "0x20004000: 5a5ac3c3" in out, out[-2000:]
