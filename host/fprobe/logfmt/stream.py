"""ログの形式の選び方と、流れ → 行の組み立て（解読 + 番地の解読）。

    stream = LogStream.create("defmt", elf="app.elf")
    records = stream.feed(bytes_from_rtt)

形式: text（UTF-8 の行）/ defmt（ELF）/ trice（til.json・li.json）/ compact（ELF の書式の番地）/ memfault（ELF の
log_fmt）。ELF があれば、どの形式でも本文の中の 0x の数のうち実行区画に入る値を関数・ファイル:行にする（ESP-09）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..symbols import Symbols
from .addresses import AddressAnnotator
from .compact import AddressLogDecoder, MemfaultDecoder, MemfaultTable
from .defmt import DefmtDecoder, DefmtTable
from .record import LogRecord
from .framing import FRAME_LIMIT
from .trice import TriceDecoder, TriceTable

__all__ = ["FORMATS", "LogStream", "TextDecoder"]

FORMATS = ("text", "defmt", "trice", "compact", "memfault")
LINE_LIMIT = 4096                 # TextDecoder の line_limit の既定（切った行には cut_at に使った上限）


class TextDecoder:
    """UTF-8 の行（\\n で区切る。\\r は落とす）。改行の来ないまま line_limit バイトを超えた行はそこで切り、その行の
    cut_at に上限を、続きの行の continued に True を入れる（切ったことが見えるように）。"""

    def __init__(self, line_limit: int = LINE_LIMIT):
        if line_limit < 1:
            raise ValueError("line_limit は 1 以上")
        self.line_limit = line_limit
        self._buf = bytearray()
        self._continued = False                    # いまの行は前に切った行の続き
        self.errors = 0

    def feed(self, data: bytes) -> list[LogRecord]:
        self._buf += data
        out = []
        while True:
            end = self._buf.find(b"\n")
            cut = None
            if end < 0:
                if len(self._buf) > self.line_limit:
                    end = cut = self.line_limit
                else:
                    break
            line = bytes(self._buf[:end]).rstrip(b"\r")
            del self._buf[:end + 1 if end < len(self._buf) and self._buf[end:end + 1] == b"\n" else end]
            out.append(LogRecord(line.decode("utf-8", errors="replace"), "text", cut_at=cut,
                                 continued=self._continued))
            self._continued = cut is not None
        return out


class LogStream:
    def __init__(self, format: str, decoder, annotator: AddressAnnotator | None = None):
        self.format, self.decoder, self.annotator = format, decoder, annotator

    @classmethod
    def create(cls, format: str, elf: str | Path | None = None, symbols: Symbols | None = None,
               til: str | Path | None = None, li: str | Path | None = None, framing: str | None = None,
               big_endian: bool = False, transport: str | None = None, annotate: bool = True,
               channel_levels: dict[str, str | None] | None = None, line_limit: int = LINE_LIMIT,
               frame_limit: int = FRAME_LIMIT) -> "LogStream":
        if format not in FORMATS:
            raise ValueError(f"format は {' / '.join(FORMATS)}")
        if symbols is None and elf is not None:
            symbols = Symbols(elf)
        elf_path = symbols.path if symbols is not None else None
        if format == "text":
            decoder = TextDecoder(line_limit)
        elif format == "defmt":
            if elf_path is None:
                raise ValueError("defmt には ELF が要る（elf か debug_elf_load）")
            table = DefmtTable.from_elf(elf_path)
            if framing is not None:
                table.encoding = framing
            decoder = DefmtDecoder(table, frame_limit)
        elif format == "trice":
            if til is None:
                raise ValueError("trice には til.json が要る（til）")
            decoder = TriceDecoder(TriceTable.load(til, li), framing=framing or "tcobs", big_endian=big_endian,
                                   levels=channel_levels, frame_limit=frame_limit)
        elif format == "compact":
            if symbols is None:
                raise ValueError("compact には ELF が要る")
            decoder = AddressLogDecoder(symbols)
        else:
            if elf_path is None:
                raise ValueError("memfault には ELF が要る")
            decoder = MemfaultDecoder(MemfaultTable.from_elf(elf_path), transport=transport or "cbor")
        annotator = AddressAnnotator(symbols) if annotate and symbols is not None else None
        return cls(format, decoder, annotator)

    @property
    def errors(self) -> int:
        return getattr(self.decoder, "errors", 0)

    def feed(self, data: bytes) -> list[LogRecord]:
        records = self.decoder.feed(data)
        if self.annotator is None:
            return records
        out = []
        for r in records:
            hits = tuple(self.annotator.find(r.text)) if r.text else ()
            out.append(dataclasses.replace(r, addresses=hits) if hits else r)
        return out
