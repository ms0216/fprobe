"""Trice（ID ベースのバイナリログ。TRC-01・TRC-02・TRC-03）の解読。github.com/rokath/trice（v1.x、v2.0 も同じ
バイナリ形式と判断。出典: docs/rm/binary_encoding.md、internal/trexDecoder/trexDecoder.go、internal/id/generate.go）。

- 表: til.json（{"ID": {"Type": "TRICE16", "Strg": "..."}}。古い ID も残す累積の表なので、古いファームのログも読める）
  と li.json（{"ID": {"File": ..., "Line": ...}}）。Strg の `\\n` は C のソースのまま（2 文字）で、行の終わりを表す。
- 枠: TCOBS v1（既定）/ COBS / none（枠なし）。1 つの枠に複数の Trice が詰まっていることがある（パディング無し）。
  枠の末尾の 4 バイト未満の 0 は詰め物。
- 1 つの Trice: u16 の上位 2 ビットがセレクタ（01 = 時刻無し、10 = 16 ビットの時刻、11 = 32 ビットの時刻、
  00 = 利用者の定義 typeX0）、下位 14 ビットが ID。続いて時刻、u16 の NC（bit15 = 0 なら bit14..8 が長さ・
  bit7..0 が回の番号、bit15 = 1 なら bit14..0 が長さで回の番号なし）、ペイロード。すべて転送のエンディアン。
- 回の番号（cycle）は 0xC0 から 1 ずつ増える。飛んだら失った Trice があった（cycle_errors）。0xC0 はターゲットの
  リセットとみなして合わせ直す。最初の Trice の番号には合わせる（途中から受け始めた流れのため）。
- 書式は Go の fmt に近い（%u は符号なし、%x・%o・%b は既定で符号なし、%t は真偽、%p は %x、C の長さの修飾は
  無視）。行の先頭の `xxx:` はチャンネル（小文字だけのものは本文から除く。err・wrn・inf・dbg 等は重要度にする）。
確かでない点: typeX0（利用者の定義）の中身は解読しない（undecoded）。-d16（ID を 2 回送る形）は d16=True で読む。
`_F`（関数呼び出し）・`_C` の型はペイロードを 16 進で出すだけ。
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from .framing import ZeroDelimited, cobs_decode, tcobs_decode
from .record import LogRecord

__all__ = ["TriceDecoder", "TriceError", "TriceTable", "go_format", "trice_type"]

CYCLE_START = 0xC0
FRAMINGS = ("tcobs", "cobs", "none")
_TAG = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):")
_LEVELS = {"err": "error", "error": "error", "fatal": "error", "ftl": "error", "e": "error", "assert": "error",
           "wrn": "warn", "warn": "warn", "warning": "warn", "w": "warn", "inf": "info", "info": "info", "i": "info",
           "dbg": "debug", "debug": "debug", "d": "debug", "trace": "trace", "tr": "trace"}
_SPEC = re.compile(r"%([-+# 0]*)(\d+)?(?:\.(\d+))?(hh|h|ll|l|j|z|L)?([a-zA-Z%])")


class TriceError(Exception):
    pass


@dataclass(frozen=True)
class TriceFormat:
    type: str
    strg: str


class TriceTable:
    def __init__(self, formats: dict[int, TriceFormat], locations: dict[int, dict] | None = None,
                 path: str | None = None):
        self.formats = formats
        self.locations = locations or {}
        self.path = path

    @classmethod
    def load(cls, til: str | Path, li: str | Path | None = None) -> "TriceTable":
        data = json.loads(Path(til).read_text(encoding="utf-8"))
        formats = {int(k): TriceFormat(v.get("Type", ""), v.get("Strg", "")) for k, v in data.items()}
        locations: dict[int, dict] = {}
        if li is not None and Path(li).exists():
            for k, v in json.loads(Path(li).read_text(encoding="utf-8")).items():
                loc = {}
                if v.get("File") or v.get("Path"):
                    loc["file"] = v.get("File") or v.get("Path")
                if v.get("Line") is not None:
                    loc["line"] = int(v["Line"])
                locations[int(k)] = loc
        return cls(formats, locations, str(til))


def trice_type(kind: str, n_params: int, default_width: int) -> tuple[str, int]:
    """til.json の Type と書式の引数の数 → (形, 値の幅)。形: fixed（幅 × 数）/ string（S・N）/ buffer（B）/
    func（F）/ other（C 等）。"""
    t = kind.upper()
    if "ASSERT" in t:
        t = t[:t.index("ASSERT")]
    if not t.startswith("TRICE"):
        raise TriceError(f"知らない型 {kind}")
    rest = t[5:].lstrip("_")
    if rest in ("S", "N"):
        return "string", 0
    m = re.fullmatch(r"(8|16|32|64)?(?:_?([BFC]|\d+))?", rest)
    if m is None:
        raise TriceError(f"知らない型 {kind}")
    width = int(m[1]) if m[1] else default_width
    suffix = m[2]
    if suffix == "B":
        return "buffer", width
    if suffix == "F":
        return "func", width
    if suffix == "C":
        return "other", width
    if suffix is not None and int(suffix) != n_params:
        raise TriceError(f"型 {kind} の引数の数 {suffix} が書式の数 {n_params} と合わない")
    return "fixed", width


def _unescape(strg: str) -> str:
    return strg.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r").replace('\\"', '"').replace("\\\\", "\\")


def go_format(fmt: str, values: list[tuple], unsigned: bool = True) -> str:
    """Go の fmt に近い整形。values は (値, 幅のビット数)。整数は生の値（2 の補数）で渡し、動詞で符号を決める。"""
    out, pos, i = [], 0, 0
    for m in _SPEC.finditer(fmt):
        out.append(fmt[pos:m.start()])
        pos = m.end()
        flags, width, prec, _, verb = m[1], m[2] or "", m[3], m[4], m[5]
        if verb == "%":
            out.append("%")
            continue
        if i >= len(values):
            out.append("%!" + verb + "(MISSING)")
            continue
        value, bits = values[i]
        i += 1
        out.append(_go_one(flags, width, prec, verb, value, bits, unsigned))
    out.append(fmt[pos:])
    return "".join(out)


def _go_one(flags: str, width: str, prec: str | None, verb: str, value, bits: int, unsigned: bool) -> str:
    align = "<" if "-" in flags else ""
    zero = "0" if "0" in flags and not align else ""
    sign = "+" if "+" in flags else " " if " " in flags else ""
    alt = "#" if "#" in flags else ""
    precision = f".{prec}" if prec is not None else ""

    def as_int(signed: bool) -> int:
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            return ord(value[0]) if value else 0
        v = value & ((1 << bits) - 1) if bits else value
        return v - (1 << bits) if signed and bits and v >> (bits - 1) else v

    def pad(text: str) -> str:
        return format(text, f"{align or '>'}{width}") if width else text
    if verb in "di":
        return format(as_int(True), f"{align}{sign}{zero}{width}d")
    if verb in "uxXobp":
        conv = {"u": "d", "p": "x"}.get(verb, verb)
        return format(as_int(not unsigned and verb not in "up"), f"{align}{sign}{alt}{zero}{width}{conv}")
    if verb in "feEgG":
        real = value if isinstance(value, float) else _bits_to_float(as_int(False), bits)
        return format(real, f"{align}{sign}{zero}{width}{precision or ('.6' if verb in 'feE' else '')}{verb}")
    if verb == "c":
        return pad(chr(as_int(False)))
    if verb == "t":
        return pad("true" if as_int(False) else "false")
    if verb == "s":
        return pad(value if isinstance(value, str) else str(value))
    if verb == "q":
        text = value if isinstance(value, str) else chr(as_int(False))
        quote = '"' if isinstance(value, str) else "'"
        return pad(quote + text.replace("\\", "\\\\").replace(quote, "\\" + quote) + quote)
    return f"%!{verb}({value})"


def _bits_to_float(raw: int, bits: int) -> float:
    if bits == 32:
        return struct.unpack("<f", struct.pack("<I", raw & 0xFFFFFFFF))[0]
    if bits == 64:
        return struct.unpack("<d", struct.pack("<Q", raw & (1 << 64) - 1))[0]
    raise TriceError(f"{bits} ビットの浮動小数は無い")


class TriceDecoder:
    """流れ → ログの行（改行で終わる Trice までを 1 行にまとめる）。"""

    def __init__(self, table: TriceTable, framing: str = "tcobs", big_endian: bool = False, default_width: int = 32,
                 unsigned: bool = True, d16: bool = False):
        if framing not in FRAMINGS:
            raise TriceError(f"枠は {' / '.join(FRAMINGS)}")
        self.table, self.framing = table, framing
        self.endian = ">" if big_endian else "<"
        self.default_width, self.unsigned, self.d16 = default_width, unsigned, d16
        self._frames = ZeroDelimited()
        self._raw = bytearray()
        self._cycle: int | None = None
        self.cycle_errors = 0
        self.errors = 0
        self._line: list[tuple] = []           # 行の途中の Trice: (本文, ID, 書式, 値, 時刻, notes)

    def feed(self, data: bytes) -> list[LogRecord]:
        out: list[LogRecord] = []
        if self.framing == "none":
            self._raw += data
            while True:
                try:
                    used, record = self._one(bytes(self._raw), 0, allow_partial=True)
                except _Partial:
                    break
                except TriceError as e:
                    self.errors += 1
                    out.append(LogRecord("", "trice", error=f"{e}（枠なしの流れは同期を失った。以後は捨てる）",
                                         raw=bytes(self._raw)))
                    self._raw.clear()
                    break
                del self._raw[:used]
                if record is not None:
                    out.append(record)
            return out
        for frame in self._frames.feed(data):
            try:
                content = tcobs_decode(frame) if self.framing == "tcobs" else cobs_decode(frame)
            except ValueError as e:
                self.errors += 1
                out.append(LogRecord("", "trice", error=str(e), raw=frame))
                continue
            pos = 0
            while pos < len(content):
                rest = content[pos:]
                if len(rest) < 4 and not any(rest):
                    break                                                  # 詰め物
                try:
                    used, record = self._one(content, pos)
                except TriceError as e:
                    self.errors += 1
                    out.append(LogRecord("", "trice", error=str(e), raw=bytes(rest)))
                    break
                pos += used
                if record is not None:
                    out.append(record)
        return out

    # --- 1 つの Trice ----------------------------------------------------------------------------

    def _one(self, data: bytes, pos: int, allow_partial: bool = False) -> tuple[int, LogRecord | None]:
        e = self.endian

        def need(n: int) -> None:
            if pos + n > len(data):
                if allow_partial:
                    raise _Partial
                raise TriceError("Trice が枠の途中で終わった")
        start = pos
        need(2)
        head = struct.unpack_from(e + "H", data, pos)[0]
        pos += 2
        selector, trice_id = head >> 14, head & 0x3FFF
        if selector == 0:
            length = trice_id
            need(length)
            raise TriceError(f"typeX0（利用者の定義）のパケット（{length} バイト）は解読しない")
        if self.d16 and selector == 2:
            need(2)
            pos += 2
        ts = None
        if selector == 2:
            need(2)
            ts = struct.unpack_from(e + "H", data, pos)[0]
            pos += 2
        elif selector == 3:
            need(4)
            ts = struct.unpack_from(e + "I", data, pos)[0]
            pos += 4
        need(2)
        nc = struct.unpack_from(e + "H", data, pos)[0]
        pos += 2
        if nc & 0x8000:
            length, cycle = nc & 0x7FFF, None
        else:
            length, cycle = nc >> 8 & 0x7F, nc & 0xFF
        need(length)
        payload = data[pos:pos + length]
        pos += length
        notes = self._check_cycle(cycle)
        fmt = self.table.formats.get(trice_id)
        if fmt is None:
            raise TriceError(f"ID {trice_id} が til.json に無い（til.json が古い・別のファーム）")
        text, values = self._format(fmt, payload)
        return pos - start, self._line_piece(text, trice_id, fmt, values, ts, notes)

    def _check_cycle(self, cycle: int | None) -> list[str]:
        if cycle is None:
            return []
        expected, self._cycle = self._cycle, (cycle + 1) & 0xFF
        if expected is None:
            return []
        if cycle == CYCLE_START and expected != CYCLE_START:
            return ["回の番号が 0xC0 に戻った（ターゲットのリセットとみなして合わせ直した）"]
        if cycle != expected:
            self.cycle_errors += 1
            missing = (cycle - expected) & 0xFF
            return [f"回の番号が飛んだ: {missing} 個の Trice を失った（0x{expected:02X} を待って 0x{cycle:02X}）"]
        return []

    def _format(self, fmt: TriceFormat, payload: bytes) -> tuple[str, tuple]:
        strg = _unescape(fmt.strg)
        specs = [m for m in _SPEC.finditer(strg) if m[5] != "%"]
        shape, width = trice_type(fmt.type, len(specs), self.default_width)
        if shape == "string":
            text = payload.decode("utf-8", errors="replace")
            return go_format(strg, [(text, 0)], self.unsigned), (text,)
        if shape in ("func", "other"):
            return f"{strg} {payload.hex()}", (payload.hex(),)
        size = width // 8
        if len(payload) % size:
            raise TriceError(f"ペイロード {len(payload)} バイトが幅 {width} ビットの倍数でない")
        code = {1: "B", 2: "H", 4: "I", 8: "Q"}[size]
        raw = list(struct.unpack(f"{self.endian}{len(payload) // size}{code}", payload))
        if shape == "buffer":
            return "".join(go_format(strg, [(v, width)], self.unsigned) for v in raw), tuple(raw)
        if len(raw) != len(specs):
            raise TriceError(f"値の数 {len(raw)} が書式の数 {len(specs)} と合わない（til.json が古い・別のファーム）")
        values = []
        for v, m in zip(raw, specs):
            if m[5] in "feEgG":
                values.append(_bits_to_float(v, width))
            elif m[5] in "di" or (m[5] in "xXob" and not self.unsigned):
                values.append(v - (1 << width) if v >> (width - 1) else v)
            else:
                values.append(v)
        return go_format(strg, [(v, width) for v in raw], self.unsigned), tuple(values)

    def _line_piece(self, text: str, trice_id: int, fmt: TriceFormat, values: tuple, ts, notes) -> LogRecord | None:
        self._line.append((text, trice_id, fmt, values, ts, notes))
        if not text.endswith("\n"):
            return None
        pieces, self._line = self._line, []
        full = "".join(p[0] for p in pieces).rstrip("\n")
        first_id, first_fmt, first_ts = pieces[0][1], pieces[0][2], pieces[0][4]
        channel = level = None
        if m := _TAG.match(full):
            channel = m[1]
            level = _LEVELS.get(channel.lower())
            if channel.islower():
                full = full[m.end():].lstrip()
        args = tuple(v for p in pieces for v in p[3])
        all_notes = tuple(n for p in pieces for n in p[5])
        return LogRecord(full, "trice", level, first_id, first_fmt.strg, args, first_ts, None,
                         self.table.locations.get(first_id), channel, notes=all_notes)


class _Partial(Exception):
    pass
