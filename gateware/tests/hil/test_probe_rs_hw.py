"""標準ツール互換: probe-rs（CMSIS-DAP v2）で fprobe 経由の Nucleo を操作する。

- 8 / 25 MHz でコアを識別する（切断時の既知の警告以外が出ないこと）
- Flash の書き込み（最後のセクタ 0x08060000 だけを使い、元の内容を退避して必ず戻す）
probe-rs は fprobe を VID:PID:シリアルで直接指定する（一覧を取らない = 他のプローブに問い合わせない）。
"""

import os
import random
import shutil
import subprocess

import pytest

from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil

PROBE_RS = shutil.which("probe-rs") or (lambda p: p if os.path.exists(p) else None)(
    os.path.expanduser("~/.cargo/bin/probe-rs.exe"))
if PROBE_RS is None:
    pytest.skip("probe-rs が無い", allow_module_level=True)
COMMON = ["--chip", "STM32F401RETx", "--probe", f"1209:0001:{SERIAL}", "--protocol", "swd"]
SECTOR, SECTOR_SIZE = 0x08060000, 128 * 1024        # STM32F401RE の最後のセクタ（7）


def probe_rs(*args: str, speed_khz: int = 8000, timeout: float = 180) -> str:
    result = subprocess.run([PROBE_RS, *args[:1], *COMMON, "--speed", str(speed_khz), *args[1:]],
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout + result.stderr


def read_bytes(address: int, length: int) -> bytes:
    out = probe_rs("read", "b32", hex(address), str(length // 4))
    words = [int(w, 16) for line in out.splitlines() if ":" in line and not line.lstrip().startswith(("WARN", "\x1b"))
             for w in line.split(":", 1)[1].split()]
    return b"".join(w.to_bytes(4, "little") for w in words)


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


@pytest.mark.parametrize("speed_khz", [8000, 25000])
def test_identifies_the_core(programmed, speed_khz):
    out = probe_rs("read", "b32", "0xE000ED00", "1", speed_khz=speed_khz)
    assert "e000ed00: 410fc241" in out
    # 警告は切断時の CTRL/STAT のポーリング（probe-rs がデバッグ電源を落とす要求の後）で、まれに読み値の
    # パリティ異常が出るものだけを許す（本来の操作の後。ターゲット側の電源切り替え中の挙動とみている。
    # docs/reports/2026-10-16-day.md）。それ以外の警告は不合格
    warnings = [line for line in out.splitlines() if "WARN" in line]
    assert all("DpRegisterAddress { address: 4" in line for line in warnings), warnings


def test_flash_download_and_restore(programmed, tmp_path):
    backup = read_bytes(SECTOR, SECTOR_SIZE)
    assert len(backup) == SECTOR_SIZE
    image = random.Random(11).randbytes(4096)
    (tmp_path / "image.bin").write_bytes(image)
    (tmp_path / "backup.bin").write_bytes(backup)
    try:
        probe_rs("download", "--binary-format", "bin", "--base-address", hex(SECTOR), str(tmp_path / "image.bin"))
        assert read_bytes(SECTOR, len(image)) == image
    finally:
        probe_rs("download", "--binary-format", "bin", "--base-address", hex(SECTOR), str(tmp_path / "backup.bin"))
        assert read_bytes(SECTOR, SECTOR_SIZE) == backup
        # Flash の書き込みアルゴリズム（SRAM 0x20000000）で止まったままにしない（次の試験が PC を見る）
        probe_rs("reset")
