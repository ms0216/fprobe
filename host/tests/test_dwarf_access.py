"""fprobe.dwarf.access: レジスタ番号 ⇄ 名前、ELF の初期値を読む ImageAccess。"""

import pytest

from fprobe.dwarf.access import AccessError, ImageAccess, register_name, register_number


def test_dwarf_register_numbers_follow_the_arm_abi():
    assert [register_name(n) for n in (0, 7, 12, 13, 14, 15)] == ["r0", "r7", "r12", "sp", "lr", "pc"]
    assert register_name(64) == "s0" and register_name(95) == "s31"
    assert register_name(256) == "d0" and register_name(271) == "d15"
    assert register_number("sp") == 13 and register_number("r13") == 13 and register_number("s16") == 80
    assert register_number("d8") == 264
    with pytest.raises(KeyError):
        register_name(200)


def test_image_reads_segments_and_overlays_writes():
    mem = ImageAccess([(0x1000, b"\x01\x02\x03\x04"), (0x2000, b"\xAA")], {"pc": 0x1234})
    assert mem.read_memory(0x1001, 3) == b"\x02\x03\x04"
    mem.write_memory(0x1002, b"\xFF\xEE")
    assert mem.read_memory(0x1000, 4) == b"\x01\x02\xFF\xEE"
    mem.write_memory(0x3000, b"\x55")                      # 区画の外にも書ける（スタックを組み立てる）
    assert mem.read_memory(0x3000, 1) == b"\x55"
    mem.write_u32(0x3004, 0x11223344)
    assert mem.read_memory(0x3004, 4) == bytes.fromhex("44332211")
    with pytest.raises(AccessError):
        mem.read_memory(0x1003, 2)                         # 一部が読めない
    assert mem.read_register("pc") == 0x1234
    with pytest.raises(AccessError):
        mem.read_register("r0")


def test_image_reads_double_registers_from_their_single_halves():
    mem = ImageAccess([], {"s16": 0x11111111, "s17": 0x22222222})
    assert mem.read_register("d8") == 0x2222222211111111
