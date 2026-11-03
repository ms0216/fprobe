"""デバッグ対象のプロセスの写像の事実: 実行ファイルの読み込み位置（load bias）と、共有ライブラリの名前・読み込み位置。

推定はしない。カーネルが exec のときに保存した補助ベクタ（mm->saved_auxv）と、プロセスのメモリ（動的リンカが書いた
r_debug / link_map の鎖）を読んだ値と、ELF のプログラムヘッダから決まる値だけを返す。仕様
docs/superpowers/specs/2026-11-03-linux-halt-ai-tools-design.md §4。
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Callable

from elftools.elf.elffile import ELFFile

from .mmu import Unmapped

AT_NULL, AT_PHDR, AT_PHNUM, AT_ENTRY = 0, 3, 5, 9
DT_NULL, DT_DEBUG = 0, 21
PATH_MAX = 4096                     # Linux の limits.h の PATH_MAX（link_map の l_name の長さの上限として使う）
MASK = 0xFFFFFFFF

ReadVirt = Callable[[int, int], bytes]


class ElfImage:
    """ELF のファイルの事実（32 ビット ARM のリトルエンディアンだけ）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.size = len(data)
        with open(self.path, "rb") as f:
            elf = ELFFile(f)
            if elf.elfclass != 32 or not elf.little_endian or elf["e_machine"] != "EM_ARM":
                raise ValueError("%s: only 32-bit little-endian ARM ELF files are handled (class %d, machine %s)"
                                 % (self.path, elf.elfclass, elf["e_machine"]))
            self.type = elf["e_type"]
            self.entry = elf["e_entry"]
            self.phnum = elf["e_phnum"]
            phoff, phentsize = elf["e_phoff"], elf["e_phentsize"]
            self.phdr_bytes = data[phoff:phoff + phentsize * self.phnum]
            self.phdr_vaddr = None
            self.dynamic = None                                        # (p_vaddr, p_memsz)
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_PHDR":
                    self.phdr_vaddr = seg["p_vaddr"]
                elif seg["p_type"] == "PT_DYNAMIC":
                    self.dynamic = (seg["p_vaddr"], seg["p_memsz"])
            self.has_dwarf = elf.has_dwarf_info()
            # Thumb の関数の先頭（リンク時の番地）: .symtab の STT_FUNC の st_value の bit 0（ARM の ELF の約束）
            self.thumb_functions = frozenset()
            symtab = elf.get_section_by_name(".symtab")
            if symtab is not None:
                self.thumb_functions = frozenset(sym["st_value"] & ~1 for sym in symtab.iter_symbols()
                                                 if sym["st_info"]["type"] == "STT_FUNC" and sym["st_value"] & 1)

    def facts(self) -> dict:
        return {"path": str(self.path), "bytes": self.size, "sha256": self.sha256, "elf_type": self.type,
                "e_entry": self.entry, "has_dwarf": self.has_dwarf}


def parse_auxv(raw: bytes) -> dict[int, int]:
    """補助ベクタ（(a_type, a_val) の u32 の対の並び、AT_NULL まで）→ {型: 値}。"""
    out = {}
    for i in range(0, len(raw) - 7, 8):
        t, v = struct.unpack_from("<II", raw, i)
        if t == AT_NULL:
            break
        out[t] = v
    return out


def executable_image(image: ElfImage, auxv: bytes, read: ReadVirt) -> dict:
    """実行ファイルの読み込み位置と、ELF が動いているものと同じかの事実（一致しなくても断らない）。

    load_bias = AT_PHDR − PT_PHDR.p_vaddr（PT_PHDR が無いか AT_PHDR が無ければ AT_ENTRY − e_entry）。
    phdr_match: AT_PHDR の番地のプロセスのメモリと ELF のプログラムヘッダの表のバイト列が同じか（読めなければ None と理由）。
    entry_match: AT_ENTRY == e_entry + load_bias か（load_bias を AT_ENTRY から求めたときは None: 同じ式で必ず一致するので）。"""
    av = parse_auxv(auxv)
    at_phdr, at_entry = av.get(AT_PHDR), av.get(AT_ENTRY)
    out = {"at_phdr": at_phdr, "at_entry": at_entry, "at_phnum": av.get(AT_PHNUM), "e_entry": image.entry,
           "elf_type": image.type, "pt_phdr_vaddr": image.phdr_vaddr, "load_bias": None, "basis": None,
           "phdr_match": None, "phdr_unreadable": None, "entry_match": None}
    if at_phdr is not None and image.phdr_vaddr is not None:
        out["load_bias"], out["basis"] = (at_phdr - image.phdr_vaddr) & MASK, "AT_PHDR - PT_PHDR.p_vaddr"
        if at_entry is not None:
            out["entry_match"] = at_entry == (image.entry + out["load_bias"]) & MASK
    elif at_entry is not None:
        out["load_bias"], out["basis"] = (at_entry - image.entry) & MASK, "AT_ENTRY - e_entry"
    if at_phdr is not None:
        try:
            out["phdr_match"] = read(at_phdr, len(image.phdr_bytes)) == image.phdr_bytes
        except Unmapped as e:
            out["phdr_unreadable"] = "the program headers at 0x%x are not mapped (%s)" % (at_phdr, e)
    return out


def _cstring(read: ReadVirt, va: int, limit: int) -> tuple[str, bool]:
    """va からの NUL で終わる文字列（ページをまたいで読む）。(文字列, NUL まで読めたか)。"""
    out = bytearray()
    while len(out) < limit:
        n = min(limit - len(out), 0x1000 - ((va + len(out)) & 0xFFF))
        chunk = read(va + len(out), n)
        end = chunk.find(b"\0")
        if end >= 0:
            out += chunk[:end]
            return out.decode("utf-8", "replace"), True
        out += chunk
    return out.decode("utf-8", "replace"), False


def shared_libraries(image: ElfImage, load_bias: int, read: ReadVirt) -> dict:
    """動的リンカが書いた鎖（実行ファイルの DT_DEBUG → r_debug.r_map → link_map の l_next）をたどった各項の
    名前・l_addr（読み込み位置のずれ）・link_map の番地。最初の項は実行ファイル自身（名前は空のことが多い）。
    鎖をたどれなければ libraries = None と reason。循環は検出して止め、reason に書く（それまでの項は返す）。"""
    out = {"r_debug": None, "libraries": None, "reason": None}
    if image.dynamic is None:
        out["reason"] = "the ELF has no PT_DYNAMIC (statically linked)"
        return out
    vaddr, memsz = image.dynamic
    dyn_at = (vaddr + load_bias) & MASK
    try:
        raw = read(dyn_at, memsz)
    except Unmapped as e:
        out["reason"] = "the dynamic section at 0x%x is not mapped (%s)" % (dyn_at, e)
        return out
    debug = None
    for i in range(0, len(raw) - 7, 8):
        tag, val = struct.unpack_from("<iI", raw, i)
        if tag == DT_NULL:
            break
        if tag == DT_DEBUG:
            debug = val
    if debug is None:
        out["reason"] = "the dynamic section has no DT_DEBUG entry"
        return out
    if debug == 0:
        out["reason"] = "DT_DEBUG is 0 (the dynamic linker has not written r_debug yet)"
        return out
    out["r_debug"] = debug
    libs, seen = [], set()
    try:
        node = struct.unpack("<iI", read(debug, 8))[1]
        while node:
            if node in seen:
                out["reason"] = "the link_map chain has a cycle at 0x%x (stopped there)" % node
                break
            seen.add(node)
            l_addr, l_name, _l_ld, l_next, _l_prev = struct.unpack("<IIIII", read(node, 20))
            name, complete = _cstring(read, l_name, PATH_MAX) if l_name else ("", True)
            entry = {"name": name, "load_bias": l_addr, "link_map": node}
            if not complete:
                entry["name_truncated_at"] = PATH_MAX
            libs.append(entry)
            node = l_next
    except Unmapped as e:
        out["reason"] = "the link_map chain is not readable past %d entries (%s)" % (len(libs), e)
    out["libraries"] = libs
    return out
