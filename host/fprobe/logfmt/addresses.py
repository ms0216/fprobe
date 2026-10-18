"""ログ（UART・RTT・SWO の文字）の中のコードの番地を、ELF で関数・ファイル:行にする（ESP-IDF の idf_monitor の
番地の自動解読に相当。ESP-09）。

- 番地の候補は `0x` で始まる 4〜8 桁の 16 進。ELF の実行できる区画（SHF_EXECINSTR）の中にあるものだけを番地とみなす
  （データの番地・長さ・SP 等の数を誤って解読しない）。
- Thumb のビット（bit 0。LR・関数ポインタの値）は落として引く。戻り番地を呼んだ命令に直すことはしない（行が
  呼び出しの次の行になることがある）。
- パニックの `Backtrace: 0xPC:0xSP …` の対も、PC の側だけがコードの区画に入るので同じ規則で解読できる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from elftools.elf.elffile import ELFFile

from ..symbols import Symbols

__all__ = ["AddressAnnotator", "AddressHit"]

_HEX = re.compile(r"\b0[xX]([0-9a-fA-F]{4,8})\b")
_SHF_EXECINSTR = 0x4


@dataclass(frozen=True)
class AddressHit:
    start: int                 # 文字の中の位置 [start, end)
    end: int
    address: int               # 書かれていた値（Thumb のビットも含む）
    function: str | None
    offset: int | None
    file: str | None
    line: int | None
    where: str

    def to_dict(self) -> dict:
        out = {"address": self.address, "where": self.where}
        for key in ("function", "offset", "file", "line"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


class AddressAnnotator:
    def __init__(self, symbols: Symbols):
        self.symbols = symbols
        with open(symbols.path, "rb") as f:
            elf = ELFFile(f)
            self.code = sorted((s["sh_addr"], s["sh_addr"] + s["sh_size"]) for s in elf.iter_sections()
                               if s["sh_flags"] & _SHF_EXECINSTR and s["sh_size"])

    def is_code(self, address: int) -> bool:
        return any(lo <= address < hi for lo, hi in self.code)

    def find(self, text: str) -> list[AddressHit]:
        out = []
        for m in _HEX.finditer(text):
            value = int(m[1], 16)
            address = value & ~1
            if not self.is_code(address):
                continue
            loc = self.symbols.describe(address)
            out.append(AddressHit(m.start(), m.end(), value, loc.function, loc.offset, loc.file, loc.line, str(loc)))
        return out

    def annotate(self, text: str) -> str:
        """番地の後ろに [関数+オフセット ファイル:行] を挟んだ文字。"""
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
