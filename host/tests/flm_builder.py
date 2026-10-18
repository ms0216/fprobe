"""試験用: 小さな CMSIS の書き込みアルゴリズム（.FLM = ELF32）を作る。中身の命令は動かない（構造の試験だけに使う）。

pyOCD の PackFlashAlgo が読むもの: 区画 PrgCode（番地 0）・PrgData、記号 Init / UnInit / EraseSector / ProgramPage と
FlashDevice（構造体 FlashDevice とその後のセクタの並び）、それらを読むための PT_LOAD。
"""

import struct
from pathlib import Path

SHT_PROGBITS, SHT_SYMTAB, SHT_STRTAB = 1, 2, 3
SHF_WRITE, SHF_ALLOC, SHF_EXECINSTR = 1, 2, 4
DEVICE_AT = 0x1000


def write_flm(path: Path, start: int, size: int, page: int, sectors: list[tuple[int, int]],
              name: bytes = b"Test QSPI", empty: int = 0xFF) -> Path:
    """sectors: [(セクタの大きさ, 領域の先頭からの番地), …]。"""
    code = struct.pack("<16H", *([0x2000, 0x4770] * 8)) * 2          # movs r0, #0; bx lr（0x40 バイト）
    data = b"\x00" * 4
    device = struct.pack("<H128sHLLLLBxxxLL", 0x0101, name, 5, start, size, page, 0, empty, 100, 3000)
    device += b"".join(struct.pack("<LL", s, a) for s, a in sectors) + struct.pack("<LL", 0xFFFFFFFF, 0xFFFFFFFF)
    loads = [("PrgCode", 0x0, code, SHF_ALLOC | SHF_EXECINSTR), ("PrgData", len(code), data, SHF_ALLOC | SHF_WRITE),
             ("DevDscr", DEVICE_AT, device, SHF_ALLOC)]
    symbols = [("Init", 0x01, 1, 2), ("UnInit", 0x05, 1, 2), ("EraseSector", 0x09, 1, 2), ("ProgramPage", 0x0D, 1, 2),
               ("EraseChip", 0x11, 1, 2), ("FlashDevice", DEVICE_AT, 3, 1)]

    strtab = b"\x00"
    sym_entries = [b"\x00" * 16]
    for sym, value, shndx, kind in symbols:
        sym_entries.append(struct.pack("<IIIBBH", len(strtab), value, 4, (1 << 4) | kind, 0, shndx))
        strtab += sym.encode() + b"\x00"
    symtab = b"".join(sym_entries)
    names = ["", *(n for n, *_ in loads), ".symtab", ".strtab", ".shstrtab"]
    shstrtab, name_at = b"", {}
    for n in names:
        name_at[n] = len(shstrtab)
        shstrtab += n.encode() + b"\x00"

    ehsize, phentsize, shentsize = 52, 32, 40
    phoff = ehsize
    offset = phoff + phentsize * len(loads)
    blobs, section_headers, program_headers = b"", [b"\x00" * shentsize], b""
    for n, address, content, flags in loads:
        section_headers.append(struct.pack("<10I", name_at[n], SHT_PROGBITS, flags, address, offset + len(blobs),
                                           len(content), 0, 0, 4, 0))
        program_headers += struct.pack("<8I", 1, offset + len(blobs), address, address, len(content), len(content),
                                       5, 4)
        blobs += content
    for n, kind, content, link, info, entsize in ((".symtab", SHT_SYMTAB, symtab, len(loads) + 2, 1, 16),
                                                  (".strtab", SHT_STRTAB, strtab, 0, 0, 0),
                                                  (".shstrtab", SHT_STRTAB, shstrtab, 0, 0, 0)):
        section_headers.append(struct.pack("<10I", name_at[n], kind, 0, 0, offset + len(blobs), len(content),
                                           link, info, 4, entsize))
        blobs += content
    shoff = offset + len(blobs)
    header = (b"\x7fELF\x01\x01\x01" + b"\x00" * 9
              + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, phoff, shoff, 0x05000000, ehsize, phentsize, len(loads),
                            shentsize, len(section_headers), len(section_headers) - 1))
    path.write_bytes(header + program_headers + blobs + b"".join(section_headers))
    return path
