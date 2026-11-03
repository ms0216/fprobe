"""fprobe.linux_halt.image: 補助ベクタ（mm->saved_auxv）とプロセスのメモリから、実行ファイルの読み込み位置と共有ライブラリを
事実として求める。仕様 2026-11-03-linux-halt-ai-tools-design.md §4。"""
import struct
from pathlib import Path

import pytest

from fprobe.linux_halt.image import AT_ENTRY, AT_PHDR, AT_PHNUM, ElfImage, executable_image, shared_libraries
from fprobe.linux_halt.mmu import Unmapped

ELF = Path(__file__).resolve().parents[1] / "data" / "linux_halt" / "race_arm"
BIAS = 0x00400000


class Mem:
    """疎な仮想メモリ（ページ単位。書いたページの中の書いていないバイトは 0、書いていないページは Unmapped）。"""
    def __init__(self):
        self.b, self.pages = {}, set()

    def write(self, va, data):
        for i, x in enumerate(data):
            self.b[va + i] = x
            self.pages.add((va + i) >> 12)

    def read(self, va, n):
        for a in range(va, va + n):
            if a >> 12 not in self.pages:
                raise Unmapped(a, 2)
        return bytes(self.b.get(a, 0) for a in range(va, va + n))


def auxv(pairs):
    return b"".join(struct.pack("<II", t, v) for t, v in pairs) + struct.pack("<II", 0, 0)


@pytest.fixture(scope="module")
def image():
    return ElfImage(ELF)


def loaded(image, bias=BIAS):
    mem = Mem()
    mem.write(image.phdr_vaddr + bias, image.phdr_bytes)
    return mem, auxv([(AT_PHDR, image.phdr_vaddr + bias), (AT_PHNUM, image.phnum), (AT_ENTRY, image.entry + bias)])


def test_the_elf_facts(image):
    assert image.type == "ET_DYN" and image.entry == 0x10244 and image.phdr_vaddr == 0x34 and image.phnum == 9
    assert image.dynamic == (0x20268, 0x40)


def test_the_bias_is_at_phdr_minus_the_pt_phdr_address_and_both_checks_match(image):
    mem, av = loaded(image)
    facts = executable_image(image, av, mem.read)
    assert facts["load_bias"] == BIAS and facts["basis"] == "AT_PHDR - PT_PHDR.p_vaddr"
    assert facts["phdr_match"] is True and facts["entry_match"] is True
    assert (facts["at_phdr"], facts["at_entry"], facts["e_entry"], facts["elf_type"]) == (
        0x34 + BIAS, 0x10244 + BIAS, 0x10244, "ET_DYN")


def test_another_program_is_reported_as_a_mismatch_not_refused(image):
    mem, av = loaded(image)
    mem.write(0x34 + BIAS, b"\xff" * 4)                               # 別のプログラムのプログラムヘッダ
    facts = executable_image(image, auxv([(AT_PHDR, 0x34 + BIAS), (AT_ENTRY, 0x12345 + BIAS)]), mem.read)
    assert facts["phdr_match"] is False and facts["entry_match"] is False and facts["load_bias"] == BIAS


def test_unreadable_program_headers_say_why(image):
    facts = executable_image(image, auxv([(AT_PHDR, 0x34 + BIAS), (AT_ENTRY, 0x10244 + BIAS)]), Mem().read)
    assert facts["phdr_match"] is None and "0x400034" in facts["phdr_unreadable"]


def test_without_at_phdr_the_entry_is_used_and_said(image):
    mem, _ = loaded(image)
    facts = executable_image(image, auxv([(AT_ENTRY, 0x10244 + BIAS)]), mem.read)
    assert facts["load_bias"] == BIAS and facts["basis"] == "AT_ENTRY - e_entry" and facts["entry_match"] is None


def test_no_dt_debug_says_so(image):
    mem, _ = loaded(image)
    dyn = struct.pack("<iI", 30, 8) + struct.pack("<iI", 0, 0)       # DT_FLAGS, DT_NULL
    mem.write(0x20268 + BIAS, dyn.ljust(0x40, b"\0"))
    r = shared_libraries(image, BIAS, mem.read)
    assert r["libraries"] is None and "DT_DEBUG" in r["reason"]


def link_chain(mem, names, cycle=False):
    """r_debug と link_map の鎖を書く。-> r_debug の番地。"""
    r_debug, base = 0x50000000, 0x50001000
    nodes = [base + 0x100 * i for i in range(len(names))]
    for i, (name, l_addr) in enumerate(names):
        nxt = nodes[i + 1] if i + 1 < len(nodes) else (nodes[0] if cycle else 0)
        prev = nodes[i - 1] if i else 0
        name_at = nodes[i] + 0x40
        mem.write(nodes[i], struct.pack("<IIIII", l_addr, name_at, 0, nxt, prev))
        mem.write(name_at, name.encode() + b"\0")
    mem.write(r_debug, struct.pack("<iI", 1, nodes[0]))
    return r_debug


def test_the_link_map_chain_gives_each_library_name_and_bias(image):
    mem, _ = loaded(image)
    r_debug = link_chain(mem, [("", BIAS), ("/lib/libc.so.6", 0xB6E00000), ("/lib/ld-linux-armhf.so.3", 0xB6F00000)])
    mem.write(0x20268 + BIAS, (struct.pack("<iI", 21, r_debug) + struct.pack("<iI", 0, 0)).ljust(0x40, b"\0"))
    r = shared_libraries(image, BIAS, mem.read)
    assert r["r_debug"] == r_debug and r["reason"] is None
    assert r["libraries"] == [{"name": "", "load_bias": BIAS, "link_map": 0x50001000},
                              {"name": "/lib/libc.so.6", "load_bias": 0xB6E00000, "link_map": 0x50001100},
                              {"name": "/lib/ld-linux-armhf.so.3", "load_bias": 0xB6F00000, "link_map": 0x50001200}]


def test_a_cycle_in_the_chain_is_stopped_and_said(image):
    mem, _ = loaded(image)
    r_debug = link_chain(mem, [("", BIAS), ("/lib/libc.so.6", 0xB6E00000)], cycle=True)
    mem.write(0x20268 + BIAS, (struct.pack("<iI", 21, r_debug) + struct.pack("<iI", 0, 0)).ljust(0x40, b"\0"))
    r = shared_libraries(image, BIAS, mem.read)
    assert len(r["libraries"]) == 2 and "cycle" in r["reason"]


def test_dt_debug_zero_means_the_dynamic_linker_has_not_written_it(image):
    mem, _ = loaded(image)
    mem.write(0x20268 + BIAS, (struct.pack("<iI", 21, 0) + struct.pack("<iI", 0, 0)).ljust(0x40, b"\0"))
    r = shared_libraries(image, BIAS, mem.read)
    assert r["libraries"] is None and "DT_DEBUG is 0" in r["reason"]
