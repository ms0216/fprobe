"""リポジトリ内 VHDL ソース（gateware/rtl）の場所。"""

from pathlib import Path

__all__ = ["CORE_GENERATED", "RTL_ROOT", "TARGET_IP_ROOT", "rtl", "target_ip"]

RTL_ROOT = Path(__file__).resolve().parent.parent / "rtl"
CORE_GENERATED = RTL_ROOT / "core" / "generated"     # CSR の生成物（fprobe_gw/generate.py）
# 対象の FPGA に入れる回路（Apache-2.0。gateware/ の GPL の回路と分ける）
TARGET_IP_ROOT = Path(__file__).resolve().parents[2] / "target_ip"


def rtl(*relative: str) -> list[Path]:
    """`RTL_ROOT` からの相対パスを絶対パスのリストにする。存在しなければ例外。"""
    paths = [RTL_ROOT / r for r in relative]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(map(str, missing)))
    return paths


def target_ip(*relative: str) -> list[Path]:
    """`TARGET_IP_ROOT` からの相対パスを絶対パスのリストにする。存在しなければ例外。"""
    paths = [TARGET_IP_ROOT / r for r in relative]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(map(str, missing)))
    return paths
