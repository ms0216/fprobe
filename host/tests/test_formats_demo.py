"""試験用ファーム host/firmware/formats_demo（zig cc で Cortex-M4 向け。U14・U15 の実機試験用）の ELF: defmt の表・
RTT の 4 つの上り・Event Recorder の制御ブロック・CTF の RAM のバッファ・TraceX の見出しのポインタ・compact の書式が
解読の部品から読めること（実際の出力は実機試験 gateware/tests/hil/test_formats_hw.py）。"""

from pathlib import Path

import pytest

from fprobe.logfmt.compact import ElfStrings
from fprobe.logfmt.defmt import DefmtTable
from fprobe.rtt import Rtt
from fprobe.symbols import Symbols
from fprobe.tracefmt.eventrecorder import INFO_SIZE, parse_info

ELF = Path(__file__).resolve().parents[1] / "firmware" / "formats_demo" / "formats_demo.elf"
SRAM, SRAM_SIZE = 0x2000_0000, 0x8000


@pytest.fixture(scope="module")
def syms():
    return Symbols(ELF)


class Image:
    def __init__(self, syms: Symbols):
        self.ram = bytearray(SRAM_SIZE)
        for seg in syms.segments:
            self.ram[seg.address - SRAM:seg.address - SRAM + len(seg.data)] = seg.data

    def read_memory(self, address: int, length: int) -> bytes:
        return bytes(self.ram[address - SRAM:address - SRAM + length])

    def write_memory(self, address: int, data: bytes) -> None:
        self.ram[address - SRAM:address - SRAM + len(data)] = data


def test_defmt_table():
    table = DefmtTable.from_elf(ELF)
    assert (table.version, table.encoding, table.timestamp) == ("4", "rzcobs", "{=u32:us}")
    tick = table.entries[table.find("tick {=u32} seq={=u16}")]
    late = table.entries[table.find("late {=i16} ms")]
    assert (tick.tag, late.tag) == ("info", "warn") and tick.location["file"].endswith("formats_demo.c")


def test_rtt_channels(syms):
    cb = Rtt(Image(syms), syms.variable("_SEGGER_RTT").address).open()
    assert [c.name for c in cb.up] == ["Terminal", "defmt", "SysView", "Compact"]
    assert [c.name for c in cb.down] == ["Terminal", "SysView"]


def test_event_recorder_control_block(syms):
    image = Image(syms)
    info = parse_info(image.read_memory(syms.variable("EventRecorderInfo").address, INFO_SIZE))
    assert info.record_count == 16 and info.ts_source == 1
    assert SRAM <= info.buffer < SRAM + SRAM_SIZE and SRAM <= info.status < SRAM + SRAM_SIZE


def test_ram_buffers_and_compact_format(syms):
    assert syms.variable("ram_tracing").size == 2048
    assert syms.variable("_tx_trace_header_ptr").size == 4
    fmt = syms.variable("compact_fmt").address
    strings = ElfStrings(ELF)
    assert strings.has_string(fmt) and strings.get(fmt) == "compact seq=%u t=%u ms"
