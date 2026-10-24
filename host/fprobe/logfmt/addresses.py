"""ログ（UART・RTT・SWO の文字）の中の 0x の 16 進の数のうち、ELF の実行できる区画に入る値を、その番地の関数・
ファイル:行にする（ESP-IDF の idf_monitor の番地の自動解読に相当。ESP-09）。

- 見るのは C の 16 進の書き方 `0x…`（桁数によらない）。値が ELF の実行できる区画（SHF_EXECINSTR）に入るものだけを
  返す。それが番地として書かれた数かどうかは決めない（区画に入る長さ・データの値もありうる。判断は使う側）。
- ARM の ELF（e_machine = EM_ARM）では、値の bit 0 は Thumb の印（AAELF の Thumb のコードの番地の決まり）なので
  落として引く（value に書かれた値、address に引いた番地）。ほかの ELF ではそのまま引く。戻り番地を呼んだ命令に
  直すことはしない（行が呼び出しの次の行になることがある）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from elftools.elf.elffile import ELFFile

from ..symbols import Symbols

__all__ = ["AddressAnnotator", "AddressHit"]

_HEX = re.compile(r"\b0[xX]([0-9a-fA-F]+)\b")
_SHF_EXECINSTR = 0x4


@dataclass(frozen=True)
class AddressHit:
    start: int                 # 文字の中の位置 [start, end)
    end: int
    value: int                 # 書かれていた値
    address: int               # 引いた番地（ARM の ELF なら bit 0 を落とした値）
    function: str | None
    offset: int | None
    file: str | None
    line: int | None
    where: str

    def to_dict(self) -> dict:
        out = {"value": self.value, "address": self.address, "where": self.where}
        for key in ("function", "offset", "file", "line"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


class AddressAnnotator:
    def __init__(self, symbols: Symbols):
        self.symbols = symbols
        with open(symbols.path, "rb") as f:
            elf = ELFFile(f)
            self.thumb_bit = elf["e_machine"] == "EM_ARM"         # bit 0 が Thumb の印（AAELF）
            self.code = sorted((s["sh_addr"], s["sh_addr"] + s["sh_size"]) for s in elf.iter_sections()
                               if s["sh_flags"] & _SHF_EXECINSTR and s["sh_size"])

    def is_code(self, address: int) -> bool:
        """address が ELF の実行できる区画に入るか。"""
        return any(lo <= address < hi for lo, hi in self.code)

    def find(self, text: str) -> list[AddressHit]:
        out = []
        for m in _HEX.finditer(text):
            value = int(m[1], 16)
            address = value & ~1 if self.thumb_bit else value
            if not self.is_code(address):
                continue
            loc = self.symbols.describe(address)
            out.append(AddressHit(m.start(), m.end(), value, address, loc.function, loc.offset, loc.file, loc.line,
                                  str(loc)))
        return out

    def annotate(self, text: str) -> str:
        """実行区画に入る値の後ろに [関数+オフセット ファイル:行] を挟んだ文字。"""
        hits = self.find(text)
        if not hits:
            return text
        parts, pos = [], 0
        for h in hits:
            parts.append(text[pos:h.end])
            label = f"{h.function}+{h.offset}" if h.function is not None else "?"
            if h.file is not None:
                label += f" {h.file}:{h.line}"
            parts.append(f" [{label}]")
            pos = h.end
        parts.append(text[pos:])
        return "".join(parts)
