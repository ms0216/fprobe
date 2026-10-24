"""Compact logging: 書式の文字列をターゲットから送らず、ELF から引くログ。

1. 番地のログ（`AddressLogDecoder`。TZ-11。Percepio の xTracePrintCompactF・SystemView の PRINT_ELF と同じ考え）:
   流れは 32 ビットの語（リトルエンディアン）の列で、1 つのログ = 書式の文字列の番地（ELF の読み込む区画にある
   文字列）+ 書式が取る引数の語（`ll` の整数と double は 2 語、%s は文字列の番地）。書式の番地でない語（ELF の
   読み込む区画の中で、区画の終わりまでに NUL で終わる 1 文字以上の列を指さない語）は読み飛ばして同期を取り直す
   （読み飛ばした語は undecoded の 1 行にまとめる）。書式の番地かは ELF の中身だけで決め、文字の見た目や長さでは
   決めない（同期を取り直す所で、引数の語がたまたまそういう番地なら書式として読む。結果の行で分かる）。ターゲットは
   `fprobe_log(fmt, ...)` のように、文字列の番地と引数を RTT・ITM（32 ビットの書き込み）・UART に書くだけでよい。
2. Memfault の compact log（`MemfaultDecoder`。MF-07。memfault-firmware-sdk の compact_log_*、ホストの
   mflt-compact-log）: 書式は読み込まない区画 `log_fmt` に "引数の数;ファイル;行;書式" の NUL 終端で並び、先頭に
   8 バイトの見出し（"LOGf"・版 1）。ターゲットは CBOR の配列 [区画の先頭からの位置, 引数…] を送る（引数は
   int32 = CBOR の整数、int64 = 1 要素の配列、double、文字列）。大きすぎて送れなかったものは
   [位置, {0: 1, 1: 本来の長さ}]。文字で送るときは `ML:<base64(CBOR)>:` の行。
"""

from __future__ import annotations

import base64
import bisect
import math
import re
import struct
from pathlib import Path

from elftools.elf.elffile import ELFFile

from ..symbols import Symbols
from .printf import Values, Words, conversions, format_c
from .record import LogRecord

__all__ = ["AddressLogDecoder", "ElfStrings", "MemfaultDecoder", "MemfaultTable", "cbor_decode", "cbor_encode"]

_SHF_ALLOC = 0x2
_LOG_FMT_MAGIC = 0x66474F4C              # "LOGf"
_ML = re.compile(r"ML:([A-Za-z0-9+/=]+):")


class ElfStrings:
    """ELF の読み込む区画（SHF_ALLOC で中身のあるもの）の、NUL 終端の文字列を番地で引く。"""

    def __init__(self, path: str | Path):
        self._ranges: list[tuple[int, bytes]] = []
        with open(path, "rb") as f:
            elf = ELFFile(f)
            self.pointer_bits = elf.elfclass                    # ELF の EI_CLASS（32 / 64）: ポインタ・long の大きさ
            for s in elf.iter_sections():
                if s["sh_flags"] & _SHF_ALLOC and s["sh_type"] == "SHT_PROGBITS" and s["sh_size"]:
                    self._ranges.append((s["sh_addr"], s.data()))
        self._ranges.sort()
        self._starts = [r[0] for r in self._ranges]

    def _locate(self, address: int) -> tuple[bytes, int] | None:
        i = bisect.bisect_right(self._starts, address) - 1
        if i < 0:
            return None
        base, data = self._ranges[i]
        offset = address - base
        return (data, offset) if offset < len(data) else None

    def get(self, address: int) -> str | None:
        """番地から NUL までの文字列（UTF-8。誤ったバイトは置き換える）。区画の外・区画の終わりまでに NUL が無ければ None。"""
        found = self._locate(address)
        if found is None:
            return None
        data, offset = found
        end = data.find(b"\0", offset)
        if end < 0:
            return None
        return data[offset:end].decode("utf-8", errors="replace")

    def has_string(self, address: int) -> bool:
        """番地に、区画の終わりまでに NUL で終わる 1 文字以上の列があるか（中身の文字・長さ・前のバイトは問わない）。"""
        found = self._locate(address)
        if found is None:
            return False
        data, offset = found
        return data.find(b"\0", offset) > offset


class AddressLogDecoder:
    def __init__(self, symbols: Symbols):
        self.symbols = symbols
        self.strings = ElfStrings(symbols.path)
        if self.strings.pointer_bits != 32:
            raise ValueError(f"番地のログは 32 ビットの語で書式の番地を送る形式: ELF が {self.strings.pointer_bits} ビット")
        self._buf = bytearray()
        self._skipped = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[LogRecord]:
        self._buf += data
        out: list[LogRecord] = []
        while len(self._buf) >= 4:
            address = struct.unpack_from("<I", self._buf)[0]
            fmt = self.strings.get(address) if self.strings.has_string(address) else None
            if fmt is None:
                self._skipped += self._buf[:4]
                del self._buf[:4]
                continue
            need = sum(2 if c.bits > 32 else 1 for c in conversions(fmt, pointer_bits=32))
            if len(self._buf) < 4 * (1 + need):
                break
            if self._skipped:
                self.errors += 1
                out.append(LogRecord("", "compact", error=f"書式の番地でない語 {len(self._skipped) // 4} 個を読み飛ばした",
                                     raw=bytes(self._skipped)))
                self._skipped.clear()
            args = list(struct.unpack_from(f"<{need}I", self._buf, 4))
            del self._buf[:4 * (1 + need)]
            text = format_c(fmt, Words(args, string=self.strings.get), pointer_bits=32)
            sym = self.symbols.symbol_at(address)
            location = {"symbol": sym[0].name} if sym is not None and sym[1] == 0 else None
            out.append(LogRecord(text, "compact", None, address, fmt, tuple(args), location=location))
        return out


# --- CBOR（RFC 8949 の小さな部分: 整数・バイト列・文字列・配列・連想・浮動小数・真偽・null） ---------------

class _CborIncomplete(Exception):
    pass


def cbor_encode(value) -> bytes:
    def head(major: int, n: int) -> bytes:
        if n < 24:
            return bytes([major << 5 | n])
        for extra, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | extra]) + n.to_bytes(size, "big")
        raise ValueError("CBOR: 整数が大きすぎる")
    if value is True or value is False:
        return bytes([0xF5 if value else 0xF4])
    if value is None:
        return b"\xf6"
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        data = value.encode()
        return head(3, len(data)) + data
    if isinstance(value, (list, tuple)):
        return head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor_encode(k) + cbor_encode(v) for k, v in value.items())
    raise TypeError(f"CBOR にできない値 {value!r}")


def cbor_decode(data: bytes, pos: int = 0):
    """(値, 次の位置)。途中で終われば _CborIncomplete、形が誤っていれば ValueError。"""
    def need(n: int) -> None:
        if pos + n > len(data):
            raise _CborIncomplete
    need(1)
    first = data[pos]
    pos += 1
    major, info = first >> 5, first & 0x1F
    if major == 7:
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        if info in (22, 23):
            return None, pos
        if info == 25:
            need(2)
            return _half(int.from_bytes(data[pos:pos + 2], "big")), pos + 2
        if info == 26:
            need(4)
            return struct.unpack(">f", data[pos:pos + 4])[0], pos + 4
        if info == 27:
            need(8)
            return struct.unpack(">d", data[pos:pos + 8])[0], pos + 8
        if info < 20:
            return info, pos
        raise ValueError(f"CBOR: 知らない単純値 0x{first:02X}")
    if info < 24:
        n = info
    elif info <= 27:
        size = 1 << (info - 24)
        need(size)
        n = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    elif info == 31 and major in (2, 3, 4, 5):
        n = None                                            # 長さを決めない
    else:
        raise ValueError(f"CBOR: 長さの誤り 0x{first:02X}")
    if major == 0:
        return n, pos
    if major == 1:
        return -1 - n, pos
    if major in (2, 3):
        if n is None:
            parts = []
            while True:
                need(1)
                if data[pos] == 0xFF:
                    pos += 1
                    break
                part, pos = cbor_decode(data, pos)
                parts.append(part)
            joined = b"".join(p if isinstance(p, bytes) else p.encode() for p in parts)
            return (joined if major == 2 else joined.decode("utf-8", errors="replace")), pos
        need(n)
        raw = bytes(data[pos:pos + n])
        return (raw if major == 2 else raw.decode("utf-8", errors="replace")), pos + n
    if major == 4:
        items = []
        while n is None or len(items) < n:
            if n is None:
                need(1)
                if data[pos] == 0xFF:
                    pos += 1
                    break
            item, pos = cbor_decode(data, pos)
            items.append(item)
        return items, pos
    if major == 5:
        out = {}
        while n is None or len(out) < n:
            if n is None:
                need(1)
                if data[pos] == 0xFF:
                    pos += 1
                    break
            key, pos = cbor_decode(data, pos)
            out[key], pos = cbor_decode(data, pos)
        return out, pos
    tagged, pos = cbor_decode(data, pos)                    # major 6: 札は無視して中身
    return tagged, pos


def _half(h: int) -> float:
    sign = -1.0 if h & 0x8000 else 1.0
    exp, frac = h >> 10 & 0x1F, h & 0x3FF
    if exp == 0:
        return sign * math.ldexp(frac, -24)
    if exp == 31:
        return sign * math.inf if frac == 0 else math.nan
    return sign * math.ldexp(frac + 1024, exp - 25)


# --- Memfault -----------------------------------------------------------------------------------

class MemfaultTable:
    def __init__(self, entries: dict[int, tuple[int, str, int, str]], *, pointer_bits: int, path: str | None = None):
        self.entries = entries                              # 位置 → (引数の数, ファイル, 行, 書式)
        self.pointer_bits = pointer_bits                    # ポインタ・long の大きさ（ELF の EI_CLASS）
        self.path = path

    @classmethod
    def from_elf(cls, path: str | Path, section: str = "log_fmt") -> "MemfaultTable":
        with open(path, "rb") as f:
            elf = ELFFile(f)
            s = elf.get_section_by_name(section)
            if s is None:
                raise ValueError(f"{path}: Memfault の compact log の区画 {section} が無い")
            if s["sh_flags"] & _SHF_ALLOC:
                raise ValueError(f"{section} が読み込む区画になっている（リンカスクリプトで INFO にする）")
            data = s.data()
            pointer_bits = elf.elfclass
        magic, version = struct.unpack_from("<IB", data)
        if magic != _LOG_FMT_MAGIC:
            raise ValueError(f"{section} の見出しが 'LOGf' でない")
        entries: dict[int, tuple[int, str, int, str]] = {}
        offset = 8
        for part in data[8:].split(b"\0"):
            if part:
                fields = part.decode("utf-8", errors="replace").split(";", 3)
                if len(fields) == 4 and fields[0].isdigit() and fields[2].isdigit():
                    entries[offset] = (int(fields[0]), fields[1], int(fields[2]), fields[3])
            offset += len(part) + 1
        return cls(entries, pointer_bits=pointer_bits, path=str(path))


class MemfaultDecoder:
    """transport: cbor（CBOR の値が続く流れ）/ text（ML:…: の行。ほかの行は format "text" の行として出す）。"""

    def __init__(self, table: MemfaultTable, transport: str = "cbor"):
        if transport not in ("cbor", "text"):
            raise ValueError("transport は cbor か text")
        self.table, self.transport = table, transport
        self._buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[LogRecord]:
        self._buf += data
        return self._cbor() if self.transport == "cbor" else self._text()

    def _cbor(self) -> list[LogRecord]:
        out = []
        while self._buf:
            try:
                value, used = cbor_decode(bytes(self._buf))
            except _CborIncomplete:
                break
            except ValueError as e:
                self.errors += 1
                out.append(LogRecord("", "memfault", error=f"{e}（1 バイト読み飛ばした）", raw=bytes(self._buf[:1])))
                del self._buf[:1]
                continue
            raw = bytes(self._buf[:used])
            del self._buf[:used]
            out.append(self._record(value, raw))
        return out

    def _text(self) -> list[LogRecord]:
        out = []
        while (end := self._buf.find(b"\n")) >= 0:
            line = self._buf[:end].rstrip(b"\r").decode("utf-8", errors="replace")
            del self._buf[:end + 1]
            m = _ML.search(line)
            if m is None:
                out.append(LogRecord(line, "text"))
                continue
            try:
                raw = base64.b64decode(m[1])
                value, _ = cbor_decode(raw)
            except (ValueError, _CborIncomplete) as e:
                self.errors += 1
                out.append(LogRecord("", "memfault", error=f"ML の行を解読できない: {e}", raw=line.encode()))
                continue
            out.append(self._record(value, raw))
        return out

    def _record(self, value, raw: bytes) -> LogRecord:
        if not isinstance(value, list) or not value or not isinstance(value[0], int):
            self.errors += 1
            return LogRecord("", "memfault", error="compact log の形（[位置, 引数…]）でない", raw=raw)
        offset = value[0]
        entry = self.table.entries.get(offset)
        if entry is None:
            self.errors += 1
            return LogRecord("", "memfault", error=f"位置 {offset} の書式が log_fmt に無い（ELF が違う）", raw=raw)
        n_args, file, line, fmt = entry
        location = {"file": file, "line": line}
        rest = value[1:]
        if len(rest) == 1 and isinstance(rest[0], dict):
            size = rest[0].get(1)
            return LogRecord(format_c(fmt, Values([]), pointer_bits=self.table.pointer_bits), "memfault", None, offset, fmt, (), location=location,
                             notes=(f"引数が大きすぎて送られなかった（本来 {size} バイト）",))
        args = tuple(v[0] if isinstance(v, list) and len(v) == 1 else v for v in rest)
        notes = () if len(args) == n_args else (f"引数の数 {len(args)} が書式の {n_args} と合わない",)
        return LogRecord(format_c(fmt, Values(args), pointer_bits=self.table.pointer_bits), "memfault", None, offset, fmt, args, location=location,
                         notes=notes)
