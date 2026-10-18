"""fprobe.memview: メモリの読み手・ELF の像・16 進ダンプ・差分。"""

from pathlib import Path

import pytest

from fprobe.memview import (BytesMemory, Change, ElfImage, MemoryReadError, TargetMemory, diff, format_hexdump,
                            hexdump)

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
SRAM_DEMO = FIRMWARE / "sram_demo" / "sram_demo.elf"
DISASM_DEMO = FIRMWARE / "disasm_demo" / "disasm_demo.elf"


# --- 読み手 ---------------------------------------------------------------------------

def test_bytes_memory_reads_inside_and_rejects_outside():
    mem = BytesMemory(0x1000, bytes(range(16)))
    assert mem.read_memory(0x1004, 4) == bytes([4, 5, 6, 7])
    with pytest.raises(MemoryReadError):
        mem.read_memory(0x100E, 4)
    with pytest.raises(MemoryReadError):
        mem.read_memory(0x0FFF, 1)


class FakeTarget:
    """pyOCD の target の一部（メモリの読み書き）。"""

    def __init__(self):
        self.mem = bytearray(64)
        self.log = []

    def read_memory_block8(self, address, length):
        return list(self.mem[address:address + length])

    def read_memory(self, address, transfer_size=32):
        self.log.append(("read", address, transfer_size))
        return int.from_bytes(self.mem[address:address + transfer_size // 8], "little")

    def write_memory(self, address, value, transfer_size=32):
        self.log.append(("write", address, transfer_size))
        self.mem[address:address + transfer_size // 8] = value.to_bytes(transfer_size // 8, "little")

    def flush(self):
        self.log.append(("flush",))


def test_target_memory_uses_the_register_width():
    target = FakeTarget()
    mem = TargetMemory(target)
    mem.write(0x10, 16, 0xBEEF)
    assert mem.read(0x10, 16) == 0xBEEF
    assert mem.read_memory(0x10, 2) == b"\xef\xbe"
    assert ("write", 0x10, 16) in target.log and ("read", 0x10, 16) in target.log and ("flush",) in target.log
    with pytest.raises(ValueError):
        mem.read(0x10, 24)


# --- ELF の像 -------------------------------------------------------------------------

def test_elf_image_reads_sections_by_run_address():
    image = ElfImage(SRAM_DEMO)
    assert image.read_memory(0x20000040, 2) == bytes.fromhex("80b5")          # reached_milestone: push {r7, lr}
    assert image.read_memory(0x200000C4, 4) == (0x22222222).to_bytes(4, "little")   # table[1]（.data）
    assert image.contains(0x20000000, 0x40) and not image.contains(0x200000D0)       # .bss は中身が無い
    with pytest.raises(MemoryReadError):
        image.read_memory(0x200000D0, 4)


def test_code_map_from_mapping_symbols():
    image = ElfImage(DISASM_DEMO)
    code = image.code_map
    assert code.kind_at(0x20000014) == "t"
    assert code.kind_at(0x2000002C) == "d" and code.kind_at(0x2000003B) == "d"     # mix のリテラルプール
    assert code.kind_at(0x2000003C) == "t"
    assert code.kind_at(0x20000000) is None                                          # .vectors（印なし）
    assert code.region_end(0x2000002C) == 0x2000003C
    assert code.last_code_start(0x20000046) == 0x2000003C


# --- 16 進ダンプ・差分 -------------------------------------------------------------------

def test_hexdump_bytes_with_ascii():
    data = b"Hello, fprobe!\x00\x01" + bytes(range(0x41, 0x45))
    rows = hexdump(data, 0x20000000, width=16)
    assert [r.address for r in rows] == [0x20000000, 0x20000010]
    assert rows[0].values[:2] == (0x48, 0x65) and rows[0].text == "Hello, fprobe!.."
    assert rows[1].values == (0x41, 0x42, 0x43, 0x44) and rows[1].text == "ABCD"
    text = format_hexdump(rows, unit=8)
    assert text.splitlines()[0].startswith("20000000: 48 65 6c 6c 6f")
    assert text.splitlines()[0].endswith("|Hello, fprobe!..|")


def test_hexdump_words_little_endian_and_changes():
    before = (0x11111111).to_bytes(4, "little") + (0x22222222).to_bytes(4, "little")
    after = (0x11111111).to_bytes(4, "little") + (0x22222223).to_bytes(4, "little")
    rows = hexdump(after, 0x100, width=8, unit=32, previous=before)
    assert rows[0].values == (0x11111111, 0x22222223)
    assert rows[0].changed == (False, True)
    line = format_hexdump(rows, unit=32)
    assert "00000100: 11111111  22222223*" in line
    halves = hexdump(after, 0x100, width=8, unit=16)
    assert halves[0].values == (0x1111, 0x1111, 0x2223, 0x2222)
    with pytest.raises(ValueError):
        hexdump(after, 0x100, width=6, unit=32)
    with pytest.raises(ValueError):
        hexdump(after, 0x100, unit=12)


def test_diff_reports_contiguous_changes():
    before = bytes(16)
    after = bytearray(before)
    after[2:4] = b"\x01\x02"
    after[10] = 0xFF
    assert diff(before, bytes(after), 0x400) == [Change(0x402, b"\x00\x00", b"\x01\x02"),
                                                 Change(0x40A, b"\x00", b"\xff")]
    assert diff(before, before, 0) == []
    with pytest.raises(ValueError):
        diff(before, before[:8], 0)
