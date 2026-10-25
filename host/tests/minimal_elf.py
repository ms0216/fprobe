"""試験用の最小の ELF32（リトルエンディアン・ARM）を作る: シンボル表だけ（DWARF なし）。

symbols = [(名前, 番地, 大きさ, "func" | "object", "global" | "local" | "weak")]
"""

import struct


def make_elf(symbols, path):
    strtab = b"\0"
    entries = [struct.pack("<IIIBBH", 0, 0, 0, 0, 0, 0)]
    locals_ = [s for s in symbols if s[4] == "local"]
    others = [s for s in symbols if s[4] != "local"]
    for name, address, size, kind, bind in locals_ + others:
        offset = len(strtab)
        strtab += name.encode() + b"\0"
        b = {"local": 0, "global": 1, "weak": 2}[bind]
        t = {"func": 2, "object": 1}[kind]
        entries.append(struct.pack("<IIIBBH", offset, address, size, (b << 4) | t, 0, 1))
    symtab = b"".join(entries)
    shstr = b"\0.symtab\0.strtab\0.shstrtab\0.text\0"
    n_symtab, n_strtab, n_shstr, n_text = 1, 9, 17, 27
    body_off = 52
    sym_off = body_off
    str_off = sym_off + len(symtab)
    shs_off = str_off + len(strtab)
    sh_off = (shs_off + len(shstr) + 3) & ~3
    sections = [
        struct.pack("<IIIIIIIIII", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        # section 1: .text 代わり（シンボルの shndx=1 を満たす空の PROGBITS）
        struct.pack("<IIIIIIIIII", n_text, 1, 6, 0, 0, 0, 0, 0, 2, 0),
        struct.pack("<IIIIIIIIII", n_symtab, 2, 0, 0, sym_off, len(symtab), 3, 1 + len(locals_), 4, 16),
        struct.pack("<IIIIIIIIII", n_strtab, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0),
        struct.pack("<IIIIIIIIII", n_shstr, 3, 0, 0, shs_off, len(shstr), 0, 0, 1, 0),
    ]
    header = (b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
              + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, sh_off, 0x05000000, 52, 0, 0, 40, len(sections), 4))
    data = header + symtab + strtab + shstr
    data += bytes(sh_off - len(data)) + b"".join(sections)
    path.write_bytes(data)
    return path
