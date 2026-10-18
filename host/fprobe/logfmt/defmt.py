"""defmt（Rust の遅延フォーマットのログ。PRS-04・PRS-11）の解読。ワイヤ形式 "4"（defmt 0.3 / 1.x）。

表（ELF）:
- 名前が `.defmt` か `.defmt.` で始まる区画のシンボルが表。名前は JSON（package・tag・data・disambiguator・
  crate_name）、番地が番号（`.defmt` は番地 1 から。0 は予約）。tag は defmt_info 等のログの重要度、defmt_println、
  defmt_prim / defmt_derived / defmt_write / defmt_str（入れ子の書式・文字列）、defmt_timestamp（時刻の書式。1 つ）、
  defmt_bitflags_value（16 バイトの値）。
- 形式の版は `_defmt_version_ = 4`、枠は `_defmt_encoding_ = rzcobs | raw` という名前のシンボル。
- ソースの場所は DWARF の `DEFMT_LOG_STATEMENT` という変数（linkage_name か番地で表の番号に対応付ける）。

1 つのかたまり: u16 LE の番号 → 時刻の書式の引数（番号は送られない）→ ログの書式の引数。引数は「番号の順」
（`{1=u16} {0=u8}` なら u8 → u16）、同じ番号のビットの欄は 1 つの値にまとめて読む。

表示は公式の解読器（decoder/src/frame.rs）に合わせる。確かでない点: 浮動小数は Rust の ryu の最短表記に合わせた
（指数の形 `1e20`）が、すべての値で一致するかは確かめていない。`:cbor` の表示は持たない（16 進のバイト列で出す）。
出典: https://github.com/knurling-rs/defmt（decoder/src/lib.rs・decoder.rs・frame.rs・elf2table、parser/src/lib.rs）
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from .framing import ZeroDelimited, rzcobs_decode
from .record import LogRecord

__all__ = ["DefmtDecoder", "DefmtEntry", "DefmtError", "DefmtTable", "Hint", "Param", "parse_format"]

LOG_TAGS = ("trace", "debug", "info", "warn", "error", "println")
VERSIONS = ("3", "4")
_INTS = {f"{s}{b}": (b, s == "i") for s in "ui" for b in (8, 16, 32, 64, 128)}
_INTS.update(usize=(32, False), isize=(32, True))
_HINT_KINDS = ("x", "X", "b", "o", "a", "?", "cbor", "us", "ms", "tus", "tms", "ts", "iso8601ms", "iso8601s")


class DefmtError(Exception):
    """かたまりが表・形式に合わない（Malformed）。"""


class _Incomplete(DefmtError):
    """かたまりが途中で終わった（raw の流れでは続きを待つ）。"""


# --- 書式 ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Hint:
    kind: str | None = None             # x X b o a ? cbor us ms tus tms ts iso8601ms iso8601s bitflags unknown
    alternate: bool = False             # '#'
    zero_pad: int | None = None         # '0N'
    bitflags: str | None = None         # __internal_bitflags_NAME@… の NAME


@dataclass(frozen=True)
class Param:
    index: int
    type: str                           # u8…i128 usize isize f32 f64 bool char str istr format [?] [u8]
                                        # [u8;N] [?;N] bits
    hint: Hint | None = None            # __internal_Debug __internal_Display __internal_FormatSequence
    bits: tuple[int, int] | None = None
    length: int | None = None           # [u8;N] / [?;N] の N


_PARAM = re.compile(r"\{(\d+)?(?:=([^:}]*))?(?::([^}]*))?\}")


def _hint(text: str | None) -> Hint | None:
    if text is None:
        return None
    alternate = text.startswith("#")
    text = text[1:] if alternate else text
    zero_pad = None
    if m := re.match(r"0(\d+)", text):
        zero_pad, text = int(m[1]), text[m.end():]
    if text.startswith("__internal_bitflags_"):
        return Hint("bitflags", alternate, zero_pad, text[len("__internal_bitflags_"):].split("@")[0])
    if text == "":
        return Hint(None, alternate, zero_pad)
    return Hint(text if text in _HINT_KINDS else "unknown", alternate, zero_pad)


def parse_format(fmt: str) -> list[str | Param]:
    """書式 → 文字と Param の並び。`{{` `}}` は文字の { }。番号の無い Param には出てきた順に 0, 1, … を振る。"""
    out: list[str | Param] = []
    counter = 0
    i = 0
    text: list[str] = []
    while i < len(fmt):
        c = fmt[i]
        if c in "{}" and fmt[i:i + 2] in ("{{", "}}"):
            text.append(c)
            i += 2
            continue
        if c == "{":
            m = _PARAM.match(fmt, i)
            if m is None:
                raise DefmtError(f"書式の誤り: {fmt!r}")
            if text:
                out.append("".join(text))
                text = []
            explicit, kind, hint = m[1], (m[2] or "").replace(" ", ""), m[3]
            if explicit is not None:
                index = int(explicit)
            else:
                index, counter = counter, counter + 1
            out.append(_param(index, kind, _hint(hint)))
            i = m.end()
            continue
        text.append(c)
        i += 1
    if text:
        out.append("".join(text))
    return out


def _param(index: int, kind: str, hint: Hint | None) -> Param:
    if kind in ("", "?"):
        return Param(index, "format", hint)
    if m := re.fullmatch(r"(\d+)\.\.(\d+)", kind):
        start, end = int(m[1]), int(m[2])
        if not (start < end <= 128):
            raise DefmtError(f"ビットの欄の範囲の誤り: {kind}")
        return Param(index, "bits", hint, bits=(start, end))
    if m := re.fullmatch(r"\[(u8|\?);(\d+)\]", kind):
        return Param(index, f"[{m[1]};{m[2]}]", hint, length=int(m[2]))
    if kind in _INTS or kind in ("f32", "f64", "bool", "char", "str", "istr", "[?]", "[u8]", "__internal_Debug",
                                 "__internal_Display", "__internal_FormatSequence"):
        return Param(index, kind, hint)
    raise DefmtError(f"知らない型: {kind}")


# --- 表 -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DefmtEntry:
    tag: str                            # info / derived / prim / str / write / … （defmt_ を除いたもの）
    data: str                           # 書式
    location: dict | None = None        # file・line・module
    package: str | None = None
    crate: str | None = None

    @property
    def fragments(self) -> list[str | Param]:
        cached = _FRAGMENTS.get(self.data)
        if cached is None:
            cached = _FRAGMENTS[self.data] = parse_format(self.data)
        return cached


_FRAGMENTS: dict[str, list[str | Param]] = {}


@dataclass
class DefmtTable:
    entries: dict[int, DefmtEntry]
    timestamp: str | None = None
    encoding: str = "rzcobs"
    version: str | None = None
    bitflags: dict[str, list[tuple[str, int]]] = field(default_factory=dict)   # 型の名前 → [(フラグ, 値)]
    path: str | None = None

    def find(self, data: str) -> int:
        for index, e in self.entries.items():
            if e.data == data:
                return index
        raise KeyError(data)

    @classmethod
    def from_elf(cls, path: str | Path) -> "DefmtTable":
        with open(path, "rb") as f:
            elf = ELFFile(f)
            sections = {i: s for i, s in enumerate(elf.iter_sections())
                        if s.name == ".defmt" or s.name.startswith(".defmt.")}
            entries: dict[int, DefmtEntry] = {}
            by_name: dict[str, int] = {}
            timestamp = version = encoding = None
            bitflag_values: list[tuple[dict, int]] = []
            for symtab in elf.iter_sections():
                if not isinstance(symtab, SymbolTableSection):
                    continue
                for s in symtab.iter_symbols():
                    name = s.name.strip('"')
                    if name.startswith("_defmt_version_ = "):
                        version = name.split("=", 1)[1].strip()
                        continue
                    if name.startswith("_defmt_encoding_ = "):
                        encoding = name.split("=", 1)[1].strip()
                        continue
                    if s["st_shndx"] not in sections or not s.name or \
                            s.name.startswith(("$d", "_defmt", "__DEFMT_MARKER")):
                        continue
                    try:
                        meta = json.loads(s.name)
                    except json.JSONDecodeError:
                        continue
                    tag = str(meta.get("tag", "")).removeprefix("defmt_")
                    if tag == "timestamp":
                        if timestamp is not None:
                            raise DefmtError("時刻の書式（defmt::timestamp!）が 2 つある")
                        timestamp = meta["data"]
                        continue
                    if tag == "bitflags_value":
                        section = sections[s["st_shndx"]]
                        offset = s["st_value"] - section["sh_addr"]
                        raw = section.data()[offset:offset + 16]
                        bitflag_values.append((meta, int.from_bytes(raw.ljust(16, b"\0"), "little")))
                        continue
                    entries[s["st_value"]] = DefmtEntry(tag, meta.get("data", ""), None, meta.get("package"),
                                                        meta.get("crate_name"))
                    by_name[s.name] = s["st_value"]
            if not entries and timestamp is None:
                raise DefmtError(f"{path}: defmt の表（.defmt の区画）が無い")
            locations = _locations(elf, by_name, set(entries)) if elf.has_dwarf_info() else {}
        for index, loc in locations.items():
            e = entries[index]
            entries[index] = DefmtEntry(e.tag, e.data, loc, e.package, e.crate)
        if version is not None and version not in VERSIONS:
            raise DefmtError(f"defmt の形式の版 {version} は解読できない（{' / '.join(VERSIONS)}）")
        bitflags: dict[str, list[tuple[str, int]]] = {}
        for meta, value in bitflag_values:
            parts = str(meta.get("data", "")).split("::")
            if len(parts) == 3:
                bitflags.setdefault(parts[0], []).append((parts[2], value))
        return cls(entries, timestamp, encoding or "rzcobs", version, bitflags, str(path))

    # --- 解読 -------------------------------------------------------------------------------

    def decode_frame(self, frame: bytes) -> tuple[LogRecord, int]:
        """1 つのかたまり → (ログの 1 行, 使ったバイト数)。合わなければ DefmtError（途中で終われば _Incomplete）。"""
        r = _Reader(frame, self)
        index = r.u(16)
        entry = self.entries.get(index)
        if entry is None:
            raise DefmtError(f"表に無い番号 {index}")
        if entry.tag not in LOG_TAGS:
            raise DefmtError(f"番号 {index} はログでない（{entry.tag}）")
        ts_value = ts_text = None
        if self.timestamp is not None:
            ts = r.format_args(parse_format(self.timestamp))
            ts_text = _render(ts, None, self)
            first = ts.args.get(0)
            ts_value = first if isinstance(first, (int, float)) else None
        message = r.format_args(entry.fragments)
        text = _render(message, None, self)
        args = tuple(_plain(message.args[k]) for k in sorted(message.args))
        return LogRecord(text, "defmt", entry.tag, index, entry.data, args, ts_value, ts_text, entry.location), r.pos


def _locations(elf: ELFFile, by_name: dict[str, int], indices: set[int]) -> dict[int, dict]:
    """DWARF の DEFMT_LOG_STATEMENT → 番号ごとの file・line・module。"""
    out: dict[int, dict] = {}
    dwarf = elf.get_dwarf_info()
    for cu in dwarf.iter_CUs():
        top = cu.get_top_DIE()
        comp_dir = top.attributes.get("DW_AT_comp_dir")
        comp_dir = comp_dir.value.decode(errors="replace") if comp_dir else ""
        line_program = None
        for die in cu.iter_DIEs():
            if die.tag != "DW_TAG_variable":
                continue
            name = die.attributes.get("DW_AT_name")
            if name is None or name.value != b"DEFMT_LOG_STATEMENT":
                continue
            index = None
            linkage = die.attributes.get("DW_AT_linkage_name")
            if linkage is not None:
                index = by_name.get(linkage.value.decode(errors="replace"))
            location = die.attributes.get("DW_AT_location")
            if index is None and location is not None and isinstance(location.value, list) and \
                    len(location.value) == 5 and location.value[0] == 0x03:                    # DW_OP_addr
                index = int.from_bytes(bytes(location.value[1:5]), "little")
            if index is None or index not in indices:
                continue
            loc: dict = {}
            line = die.attributes.get("DW_AT_decl_line")
            if line is not None:
                loc["line"] = line.value
            file_attr = die.attributes.get("DW_AT_decl_file")
            if file_attr is not None:
                if line_program is None:
                    line_program = dwarf.line_program_for_CU(cu)
                loc["file"] = _file_name(line_program, file_attr.value, comp_dir)
            module = []
            parent = die.get_parent()
            while parent is not None:
                if parent.tag == "DW_TAG_namespace" and "DW_AT_name" in parent.attributes:
                    module.append(parent.attributes["DW_AT_name"].value.decode(errors="replace"))
                parent = parent.get_parent()
            if module:
                loc["module"] = "::".join(reversed(module))
            out[index] = loc
    return out


def _file_name(lp, number: int, comp_dir: str) -> str | None:
    if lp is None:
        return None
    files = lp["file_entry"]
    version = lp["version"]
    i = number if version >= 5 else number - 1
    if not 0 <= i < len(files):
        return None
    entry = files[i]
    name = entry.name.decode(errors="replace") if isinstance(entry.name, bytes) else str(entry.name)
    dirs = lp["include_directory"]
    d = entry.dir_index if version >= 5 else entry.dir_index - 1
    directory = ""
    if 0 <= d < len(dirs):
        directory = dirs[d].decode(errors="replace") if isinstance(dirs[d], bytes) else str(dirs[d])
    if directory and not Path(directory).is_absolute() and comp_dir:
        directory = str(Path(comp_dir) / directory)
    if Path(name).is_absolute() or not directory:
        return name
    return str(Path(directory) / name).replace("\\", "/")


# --- 引数の読み ---------------------------------------------------------------------------------

@dataclass
class _Formatted:
    """解読した 1 つの書式（入れ子の Format も）: 並びと、番号 → 値。"""
    fragments: list
    args: dict


class _Reader:
    def __init__(self, data: bytes, table: DefmtTable):
        self.data, self.pos, self.table = data, 0, table

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise _Incomplete("かたまりが途中で終わった")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u(self, bits: int) -> int:
        return int.from_bytes(self.take(bits // 8), "little")

    def s(self, bits: int) -> int:
        return int.from_bytes(self.take(bits // 8), "little", signed=True)

    def nested_entry(self) -> DefmtEntry:
        index = self.u(16)
        entry = self.table.entries.get(index)
        if entry is None:
            raise DefmtError(f"表に無い番号 {index}（入れ子）")
        if entry.tag in LOG_TAGS:
            raise DefmtError(f"入れ子の番号 {index} がログを指している")
        return entry

    def format_args(self, fragments: list) -> _Formatted:
        params = [p for p in fragments if isinstance(p, Param)]
        merged: dict[int, Param] = {}
        for p in params:
            if p.type == "bits" and p.index in merged and merged[p.index].type == "bits":
                a, b = merged[p.index].bits
                merged[p.index] = Param(p.index, "bits", None, bits=(min(a, p.bits[0]), max(b, p.bits[1])))
            else:
                merged.setdefault(p.index, p)
        args = {i: self.value(merged[i]) for i in sorted(merged)}
        return _Formatted(fragments, args)

    def value(self, p: Param):
        t = p.type
        if t in _INTS:
            bits, signed = _INTS[t]
            return self.s(bits) if signed else self.u(bits)
        if t == "f32":
            return struct.unpack("<f", self.take(4))[0]
        if t == "f64":
            return struct.unpack("<d", self.take(8))[0]
        if t == "bool":
            b = self.u(8)
            if b > 1:
                raise DefmtError(f"bool の値が {b}")
            return bool(b)
        if t == "char":
            code = self.u(32)
            try:
                return chr(code)
            except ValueError:
                raise DefmtError(f"char の値が {code:#x}") from None
        if t == "str":
            return self._utf8(self.take(self.u(32)))
        if t == "[u8]":
            return bytes(self.take(self.u(32)))
        if t.startswith("[u8;"):
            return bytes(self.take(p.length))
        if t == "istr":
            entry = self.nested_entry()
            return entry.data
        if t == "format":
            return self.format_value(self.nested_entry())
        if t == "[?]":
            return self.format_slice(self.u(32))
        if t.startswith("[?;"):
            return self.format_slice(p.length)
        if t in ("__internal_Debug", "__internal_Display"):
            end = self.data.find(b"\xff", self.pos)
            if end < 0:
                raise _Incomplete("__internal_Debug の終わり（0xFF）が無い")
            return self._utf8(self.take(end - self.pos + 1)[:-1])
        if t == "__internal_FormatSequence":
            items = []
            while True:
                index = self.u(16)
                if index == 0:
                    return items
                entry = self.table.entries.get(index)
                if entry is None:
                    raise DefmtError(f"表に無い番号 {index}（並び）")
                items.append(self.format_args(entry.fragments))
        if t == "bits":
            start, end = p.bits
            lo, hi = start // 8, (end - 1) // 8
            size = hi - lo + 1
            width = 1 if size == 1 else 2 if size == 2 else 4 if size <= 4 else 8 if size <= 8 else 16
            return self.u(width * 8) << (lo * 8)
        raise DefmtError(f"知らない型 {t}")

    def format_value(self, entry: DefmtEntry, variants: list | None = None):
        """Format の値: enum（書式に | がある derived）は判別子で枝を選ぶ。"""
        variants = variants if variants is not None else _variants(entry)
        if variants is None:
            return self.format_args(entry.fragments)
        n = len(variants) - 1
        width = 8 if n <= 0xFF else 16 if n <= 0xFFFF else 32
        d = self.u(width)
        if d >= len(variants):
            raise DefmtError(f"enum の判別子 {d} が枝の数 {len(variants)} を超えている")
        return self.format_args(parse_format(variants[d]))

    def format_slice(self, count: int) -> list:
        if count == 0:
            return []
        entry = self.nested_entry()                                   # 書式の番号は 1 回だけ（全要素で共有）
        variants = _variants(entry)
        return [self.format_value(entry, variants) for _ in range(count)]

    @staticmethod
    def _utf8(data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise DefmtError("文字列が UTF-8 でない") from None


def _variants(entry: DefmtEntry) -> list[str] | None:
    if entry.tag != "derived" or "|" not in entry.data:
        return None
    return entry.data.split("|")


def _plain(value):
    """記録の args 用の素朴な値（入れ子は文字に）。"""
    if isinstance(value, _Formatted):
        return _render(value, None, None)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --- 表示 ---------------------------------------------------------------------------------------

def _effective(hint: Hint | None, parent: Hint | None) -> Hint | None:
    if hint is None or hint.kind in (None, "?") and not hint.zero_pad and not hint.alternate:
        return parent if parent is not None else hint
    if hint.kind == "?" and parent is not None:
        return parent
    return hint


def _render(f: _Formatted, parent: Hint | None, table: DefmtTable | None) -> str:
    out = []
    for frag in f.fragments:
        if isinstance(frag, str):
            out.append(frag)
            continue
        value = f.args.get(frag.index)
        out.append(_render_value(frag, value, parent, table))
    return "".join(out)


def _render_value(p: Param, value, parent: Hint | None, table) -> str:
    hint = _effective(p.hint, parent)
    t = p.type
    if t == "bits":
        start, end = p.bits
        return _int(value >> start & ((1 << (end - start)) - 1), end - start, False, hint, table)
    if t in _INTS:
        bits, signed = _INTS[t]
        return _int(value, bits, signed, hint, table)
    if t in ("f32", "f64"):
        return _float(value, t == "f32")
    if t == "bool":
        return "true" if value else "false"
    if t == "char":
        return _rust_debug_char(value) if hint is not None and hint.kind == "?" else value
    if t in ("str", "istr", "__internal_Debug", "__internal_Display"):
        if t == "str" and hint is not None and hint.kind == "?":
            return _rust_debug_str(value)
        return value
    if t == "[u8]" or t.startswith("[u8;"):
        return _bytes(value, hint)
    if t == "format":
        inner = parent if parent is not None and parent.kind == "a" else hint
        return _render(value, inner, table)
    if t == "[?]" or t.startswith("[?;"):
        inner = parent if parent is not None and parent.kind == "a" else hint
        if inner is not None and inner.kind == "a" and value and \
                all(len(v.args) == 1 and isinstance(v.args.get(0), int) and "{=u8" in "".join(
                    x if isinstance(x, str) else f"{{={x.type}" for x in v.fragments) for v in value):
            return _bytes(bytes(v.args[0] for v in value), inner)
        return "[" + ", ".join(_render(v, inner, table) for v in value) + "]"
    if t == "__internal_FormatSequence":
        return "".join(_render(v, hint, table) for v in value)
    return str(value)


def _int(value: int, bits: int, signed: bool, hint: Hint | None, table) -> str:
    kind = hint.kind if hint is not None else None
    if kind in ("x", "X", "b", "o"):
        if value < 0:
            value &= (1 << bits) - 1                                  # 元の幅の 2 の補数
        digits = format(value, kind)
        prefix = {"x": "0x", "X": "0x", "b": "0b", "o": "0o"}[kind] if hint.alternate else ""
        if hint.zero_pad:
            digits = digits.rjust(max(hint.zero_pad - len(prefix), 0), "0")
        return prefix + digits
    if kind == "us":
        return f"{value // 1_000_000}.{value % 1_000_000:06}"
    if kind == "ms":
        return f"{value // 1_000}.{value % 1_000:03}"
    if kind in ("tus", "tms", "ts"):
        return _clock(value, kind)
    if kind in ("iso8601ms", "iso8601s"):
        ms = value if kind == "iso8601ms" else value * 1000
        dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
        frac = f".{ms % 1000:03}" if kind == "iso8601ms" else ""
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z"
    if kind == "bitflags" and table is not None:
        flags = [name for name, v in table.bitflags.get(hint.bitflags, []) if v and value & v == v]
        return " | ".join(flags) if flags else "(empty)"
    text = str(value)
    if hint is not None and hint.zero_pad:
        sign = "-" if value < 0 else ""
        text = sign + str(abs(value)).rjust(hint.zero_pad - len(sign), "0")
    return text


def _clock(value: int, kind: str) -> str:
    per = {"tus": 1_000_000, "tms": 1_000, "ts": 1}[kind]
    seconds, frac = divmod(value, per)
    minutes, s = divmod(seconds, 60)
    hours, m = divmod(minutes, 60)
    days, h = divmod(hours, 24)
    text = f"{h:02}:{m:02}:{s:02}"
    if days:
        text = f"{days}:{text}"
    if kind == "tus":
        text += f".{frac:06}"
    elif kind == "tms":
        text += f".{frac:03}"
    return text


def _float(value: float, single: bool) -> str:
    """ryu の最短表記に寄せる（1.0 → "1.0"、1e20 → "1e20"）。f32 は 32 ビットで往復する最短の桁。"""
    if value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "inf" if value > 0 else "-inf"
    text = repr(value)
    if single:
        for digits in range(1, 10):
            candidate = f"{value:.{digits}g}"
            if struct.unpack("<f", struct.pack("<f", float(candidate)))[0] == value:
                text = repr(float(candidate))
                break
    if "e" in text:
        mantissa, exponent = text.split("e")
        mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
        return f"{mantissa}e{int(exponent)}"
    return text


def _bytes(data: bytes, hint: Hint | None) -> str:
    kind = hint.kind if hint is not None else None
    if kind == "a":
        out = []
        for b in data:
            c = chr(b)
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", '"': '\\"', "\\": "\\\\"}.get(c) or
                       (c if 0x20 <= b < 0x7F else f"\\x{b:02x}"))
        return 'b"' + "".join(out) + '"'
    if kind in ("x", "X", "b", "o"):
        h = Hint(kind, True, hint.zero_pad)
        return "[" + ", ".join(_int(b, 8, False, h, None) for b in data) + "]"
    return "[" + ", ".join(str(b) for b in data) + "]"


def _rust_debug_str(s: str) -> str:
    out = []
    for c in s:
        if c in "\"\\":
            out.append("\\" + c)
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\u{{{ord(c):x}}}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _rust_debug_char(c: str) -> str:
    return "'" + ("\\'" if c == "'" else _rust_debug_str(c)[1:-1].replace('\\"', '"')) + "'"


# --- 流れ ---------------------------------------------------------------------------------------

class DefmtDecoder:
    """流れ（RTT・SWO・UART のバイト）→ ログの行。rzcobs は 0x00 で区切ったかたまりごと（壊れたかたまりは
    undecoded にして次へ進む）。raw は枠が無いので、1 つでも合わなければ同期を失う（synced = False。以後は捨てる）。"""

    def __init__(self, table: DefmtTable):
        self.table = table
        self.encoding = table.encoding
        if self.encoding not in ("rzcobs", "raw"):
            raise DefmtError(f"知らない枠 {self.encoding}（rzcobs / raw）")
        self._frames = ZeroDelimited()
        self._raw = bytearray()
        self.synced = True
        self.frames = 0
        self.errors = 0

    def feed(self, data: bytes) -> list[LogRecord]:
        if self.encoding == "rzcobs":
            out = []
            for frame in self._frames.feed(data):
                try:
                    content = rzcobs_decode(frame)
                except ValueError as e:
                    self.errors += 1
                    out.append(LogRecord("", "defmt", error=str(e), raw=frame))
                    continue
                out.append(self._one(content))
            return out
        if not self.synced:
            return []
        self._raw += data
        out = []
        while self._raw:
            try:
                record, used = self.table.decode_frame(bytes(self._raw))
            except _Incomplete:
                break
            except DefmtError as e:
                self.errors += 1
                self.synced = False
                out.append(LogRecord("", "defmt", error=f"{e}（raw の枠は同期を失った。以後は解読しない）",
                                     raw=bytes(self._raw)))
                self._raw.clear()
                break
            self.frames += 1
            del self._raw[:used]
            out.append(record)
        return out

    def _one(self, content: bytes) -> LogRecord:
        try:
            record, _ = self.table.decode_frame(content)
        except DefmtError as e:
            self.errors += 1
            return LogRecord("", "defmt", error=str(e), raw=content)
        self.frames += 1
        return record
