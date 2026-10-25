"""不具合の再現試験（検出専用。修正されるまで xfail(strict)）: GDB サーバ・RSP・Flash ブレークポイントとの関係。
台帳: docs/reports/bugs/flash-snapshot-gdb.md"""

from types import SimpleNamespace

import pytest
from flash_fakes import FLASH
from gdb_fakes import FakeCore, make_probe
from probe_fakes import FakeDevice
from thumb_fake import ThumbChip, thumb

from fprobe.gdb import rsp
from fprobe.gdb.description import Region, fill_gaps
from fprobe.mcp_server import Probe

LOOP = FLASH + 0x100
PROGRAM = thumb(0x2000, 0x3001, 0x3001, 0x3001, 0x3001, 0xE7FA)
C = LOOP + 6


def new_chip(hw: int = 0) -> ThumbChip:
    chip = ThumbChip(hw_breakpoints=hw)
    chip.load(LOOP, PROGRAM)
    chip.regs["pc"] = LOOP
    return chip


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    p._stop_observing()


def connect(probe, chip) -> None:
    probe._session.debug = lambda **kw: SimpleNamespace(target=chip, open=lambda: None, close=lambda: None,
                                                        options={"frequency": 4_000_000})
    probe.target_connect("fake_target")


def test_bug_gdb_flash_does_not_fold_or_forget_flash_breakpoints(probe):
    chip = new_chip()
    connect(probe, chip)
    probe._debug.board = SimpleNamespace(target=chip)                  # pyOCD の MemoryLoader が要る
    probe._debug.options = {"hide_programming_progress": True}
    chip.debug_sequence_delegate = None
    chip.selected_core = SimpleNamespace(node_name=None)
    probe.breakpoint_set(hex(C))
    probe.run_until_halt(timeout_s=1)                                  # C に BKPT を書いて止まる
    block = bytearray(chip.flash[:0x400])
    block[C - FLASH:C - FLASH + 2] = thumb(0x2005)                     # 新しい中身（C を別の命令に）
    probe.gdb_flash([(FLASH, bytes(block))])
    assert bytes(probe.target.read_memory_block8(C, 2)) == thumb(0x2005)    # 新しい中身が見えるはず


@pytest.mark.xfail(strict=True, reason="BUG-GDB-002: Z0 と Z1 を同じ番地に置いて片方を外すと、もう一方のブレークポイントも消える")
def test_bug_gdb_z0_and_z1_on_same_address_share_one_entry():
    probe = make_probe(FakeCore())
    probe.gdb_breakpoint("o", True, "break", C, 2)
    probe.gdb_breakpoint("o", True, "hbreak", C, 2)
    probe.gdb_breakpoint("o", False, "break", C, 2)                    # z0 だけ外す
    assert C in probe.dbg.breakpoints                                  # z1 はまだ要る


def test_bug_gdb_watchpoints_of_different_kinds_on_same_address():
    probe = make_probe(FakeCore())
    probe.gdb_breakpoint("o", True, "write", 0x20000100, 4)
    probe.gdb_breakpoint("o", True, "read", 0x20000100, 4)
    assert probe.dbg.watches[0x20000100].kind == "access"             # 書きと読みの両方で止まる


@pytest.mark.xfail(strict=True, reason="BUG-GDB-004: RSP の受信が '$' で読み直さず、途中で切れたパケットの次のパケットまで巻き込んで捨てる")
def test_bug_rsp_reader_does_not_restart_on_dollar():
    reader = rsp.PacketReader()
    events = reader.feed(b"$ab" + rsp.frame(b"OK"))                    # 途中で切れたパケットの後に、新しいパケット
    assert events[-1] == rsp.Packet(b"OK")


@pytest.mark.xfail(strict=True, reason="BUG-GDB-005: 部分的に重なるメモリ区画の後ろの部分が fill_gaps で消える")
def test_bug_fill_gaps_drops_the_tail_of_a_partially_overlapping_region():
    regions = fill_gaps([Region("ram", 0x1000, 0x100), Region("rom", 0x1080, 0x100)])
    assert any(r.kind == "rom" for r in regions)                       # 0x1100..0x1180 は rom のはず


@pytest.mark.xfail(strict=True, reason="BUG-GDB-006: 末尾が '}' で終わる(不完全な逃がし)パケットの unescape が NUL を1バイト捏造する")
def test_bug_unescape_dangling_escape_invents_a_nul_byte():
    assert rsp.unescape(b"ab}") == b"ab"


def test_flash_benchmark_keeps_flash_breakpoint_ledger_consistent(probe):
    """疑い S1: flash_benchmark(program_address=) が Flash BKPT の記録を合わせず、BKPT が消えて記録だけが残らない。"""
    chip = new_chip()
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    probe.run_until_halt(timeout_s=1)
    assert C in probe.target._written
    probe.flash_benchmark(ram_length=0, read_length=0, program_address=FLASH, program_length=0x400)
    assert bytes(probe.target.read_memory_block8(C, 2)) == PROGRAM[C - LOOP:C - LOOP + 2]
    assert C not in probe.target._written or bytes(chip.flash[C - FLASH:C - FLASH + 2]) != PROGRAM[C - LOOP:C - LOOP + 2]
