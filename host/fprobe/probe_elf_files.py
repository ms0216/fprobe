"""Probe の道具: 画面からの ELF・書き込むファイル。ブラウザで選んだファイルをデーモンへ送って読む・置く（遠隔の画面でも使える）、同梱のデモの
ELF を読む（機器・ELF が無くても各パネルの働きが見える）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の elf_load を使う。機器には触らない。
仕様: docs/superpowers/specs/2026-10-18-ui-layout-design.md §4
"""

import base64
import binascii
import re
from pathlib import Path

from .home import fprobe_home
from .flash import image_format

__all__ = ["DEMO_ELF", "ElfFileTools", "UPLOAD_LIMIT"]

DEMO_ELF = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
UPLOAD_LIMIT = 64 << 20                     # 送れるファイルの大きさの上限（バイト）
_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')


def _uploads() -> Path:
    return fprobe_home() / "uploads"


class ElfFileTools:
    def elf_load_demo(self) -> dict:
        """同梱のデモの ELF（host/firmware/sram_demo/sram_demo.elf。RAM で動く STM32 の小さなプログラム）を読む。"""
        return self.elf_load(str(DEMO_ELF))

    def elf_upload(self, name: str, data_base64: str) -> dict:
        """画面で選んだ ELF（中身を base64）を FPROBE_HOME/uploads/<名前> に置いて読む。名前の中の場所は捨てる。"""
        path = _store_upload(name, data_base64,
                             lambda data: data.startswith(b"\x7fELF") or "ELF でない（先頭が \\x7fELF でない）")
        return self.elf_load(str(path))

    def flash_upload(self, name: str, data_base64: str) -> dict:
        """画面で選んだ書き込むファイル（ELF / HEX / BIN）を FPROBE_HOME/uploads/<名前> に置き、その場所・大きさ・形式を
        返す（読まない。Flash の画面がこのパスを target.flash・target.flash.verify に渡す。BIN は番地が要る）。"""
        path = _store_upload(name, data_base64, lambda data: bool(data) or "中身が空")
        return {"path": str(path), "bytes": path.stat().st_size, "format": image_format(path)}


def _store_upload(name: str, data_base64: str, check) -> Path:
    """送られたファイルを uploads に置く（名前の中の場所は捨てる）。check(data) が True でなければ（理由の文字列なら）
    書く前に断る。"""
    base = re.split(r"[\\/]", name)[-1]
    if not base or base in (".", "..") or _BAD.search(base):
        raise ValueError(f"ファイルの名前にできない: {name!r}")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("data_base64 が base64 でない") from None
    if len(data) > UPLOAD_LIMIT:
        raise ValueError(f"大きすぎる（{len(data)} バイト、上限 {UPLOAD_LIMIT}）")
    verdict = check(data)
    if verdict is not True:
        raise ValueError(verdict)
    folder = _uploads()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / base
    path.write_bytes(data)
    return path
