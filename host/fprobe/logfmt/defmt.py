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
分け方: 書式文字列の構文と表の 1 項目は defmt_format.py、引数の読みと表示は defmt_value.py、ここには表（ELF から作る）と流れの解読を置く。
出典: https://github.com/knurling-rs/defmt（decoder/src/lib.rs・decoder.rs・frame.rs・elf2table、parser/src/lib.rs）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from .defmt_format import (  # noqa: F401  (再公開)
    _HINT_KINDS, _INTS, LOG_TAGS, VERSIONS, DefmtEntry, DefmtError, Hint, Param, _Incomplete, _hint, _param, parse_format,
)
from .defmt_value import _plain, _Reader, _render  # noqa: F401  (_render 等は試験が読むことがある)
from .framing import FRAME_LIMIT, Dropped, ZeroDelimited, dropped_text, rzcobs_decode
from .record import LogRecord

__all__ = ["DefmtDecoder", "DefmtEntry", "DefmtError", "DefmtTable", "Hint", "Param", "parse_format"]

@dataclass
class DefmtTable:
    entries: dict[int, DefmtEntry]
    timestamp: str | None = None
    encoding: str | None = None         # 枠（ELF の _defmt_encoding_）。無ければ None（使う側が明示する）
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
        return cls(entries, timestamp, encoding, version, bitflags, str(path))

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


# --- 流れ ---------------------------------------------------------------------------------------

class DefmtDecoder:
    """流れ（RTT・SWO・UART のバイト）→ ログの行。rzcobs は 0x00 で区切ったかたまりごと（壊れたかたまりは
    undecoded にして次へ進む）。raw は枠が無いので、1 つでも合わなければ同期を失う（synced = False。以後は捨てる）。"""

    def __init__(self, table: DefmtTable, frame_limit: int = FRAME_LIMIT):
        self.table = table
        self.encoding = table.encoding
        if self.encoding is None:
            raise DefmtError("ELF に枠の記録（_defmt_encoding_）が無い: framing（rzcobs / raw）で明示する")
        if self.encoding not in ("rzcobs", "raw"):
            raise DefmtError(f"知らない枠 {self.encoding}（rzcobs / raw）")
        self._frames = ZeroDelimited(frame_limit)
        self._raw = bytearray()
        self.synced = True
        self.frames = 0
        self.errors = 0

    def feed(self, data: bytes) -> list[LogRecord]:
        if self.encoding == "rzcobs":
            out = []
            for frame in self._frames.feed(data):
                if isinstance(frame, Dropped):
                    self.errors += 1
                    out.append(LogRecord("", "defmt", error=dropped_text(frame), cut_at=frame.limit))
                    continue
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
