"""試験用ファーム host/firmware/rtt_demo（zig cc で Cortex-M4 向け）の ELF: RTT の制御ブロックをシンボルから引け、
ELF の中身を偽のメモリに置くと走査・解析でき、セミホスティングの BKPT と設定用の変数があること。"""

import struct
from pathlib import Path

import pytest

from fprobe.rtt import ID, Rtt, RttMode, find_control_block, rtt_address_from_elf
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "rtt_demo" / "rtt_demo.elf"
SRAM, SRAM_SIZE = 0x20000000, 0x8000


class ImageMemory:
    """ELF の読み込む中身を置いた SRAM（.bss は 0）。読みの回数を数える。"""

    def __init__(self, syms: Symbols):
        self.ram = bytearray(SRAM_SIZE)
        for seg in syms.segments:
            self.ram[seg.address - SRAM:seg.address - SRAM + len(seg.data)] = seg.data
        self.reads = 0

    def read_memory(self, address: int, length: int) -> bytes:
        self.reads += 1
        return bytes(self.ram[address - SRAM:address - SRAM + length])

    def write_memory(self, address: int, data: bytes) -> None:
        self.ram[address - SRAM:address - SRAM + len(data)] = data


@pytest.fixture(scope="module")
def syms():
    return Symbols(ELF)


def test_control_block_symbol(syms):
    address = rtt_address_from_elf(ELF)
    cb = syms.variable("_SEGGER_RTT")
    assert address == cb.address and SRAM <= address < SRAM + SRAM_SIZE
    assert cb.size == 24 + 24 * 3                                        # 上り 2・下り 1


def test_id_appears_only_in_the_control_block(syms):
    image = b"".join(seg.data for seg in syms.segments)
    assert image.count(ID) == 1


def test_scan_and_parse_the_loaded_image(syms):
    mem = ImageMemory(syms)
    address = find_control_block(mem, [(SRAM, SRAM_SIZE)])
    assert address == syms.variable("_SEGGER_RTT").address
    cb = Rtt(mem, address).open()
    assert [(c.name, c.size, c.mode, c.buffer_address) for c in cb.up] == [
        ("Terminal", 512, RttMode.NO_BLOCK_SKIP, syms.variable("up0_buffer").address),
        ("Data", 256, RttMode.NO_BLOCK_TRIM, syms.variable("up1_buffer").address)]
    assert [(c.name, c.size, c.buffer_address) for c in cb.down] == [
        ("Terminal", 64, syms.variable("down0_buffer").address)]


def test_rings_follow_the_descriptors_so_a_poll_is_one_read(syms):
    """リング（.bss）は制御ブロック（.data）の後ろにあるので、推測読みで 1 回の読みにまとまる。"""
    mem = ImageMemory(syms)
    rtt = Rtt(mem, syms.variable("_SEGGER_RTT").address)
    cb = rtt.open()
    up0 = cb.up[0]
    mem.write_memory(up0.buffer_address, b"tick 100\n")                  # ターゲットの書き込みを模擬
    mem.write_memory(up0.descriptor_address + 12, struct.pack("<I", 9))
    mem.reads = 0
    assert rtt.poll() == {0: b"tick 100\n"} and mem.reads == 1


def test_semihosting_breakpoint_and_settings(syms):
    text = next(seg for seg in syms.segments if seg.address <= syms.resolve("reset_handler") < seg.address + len(seg.data))
    offsets = [i for i in range(0, len(text.data) - 1, 2) if text.data[i:i + 2] == b"\xAB\xBE"]   # BKPT 0xAB
    assert offsets
    mem = ImageMemory(syms)
    trace_config = struct.unpack("<I", mem.read_memory(syms.variable("trace_config").address, 4))[0]
    assert trace_config == 0x17                                           # SETUP・TIMESTAMPS・EXCEPTIONS・SYNC
    for name in ("semihosting_enabled", "semihosting_exit", "semihosting_done", "sh_clock", "sh_time", "ticks",
                 "sequence", "echo_count"):
        assert syms.variable(name).size == 4
