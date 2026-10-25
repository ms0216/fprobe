"""不具合の再現試験（検出専用。修正されるまで xfail(strict)）: Flash ブレークポイント（fprobe.flash_bp）。
台帳: docs/reports/bugs/flash-snapshot-gdb.md"""

import pytest
from flash_fakes import FLASH
from pyocd.core.target import Target
from thumb_fake import ThumbChip, thumb

from fprobe.flash_bp import FlashBreakpoints, Journal

LOOP = FLASH + 0x100
PROGRAM = thumb(0x2000, 0x3001, 0x3001, 0x3001, 0x3001, 0xE7FA)
A = LOOP + 2
HW, SW = Target.BreakpointType.HW, Target.BreakpointType.SW


@pytest.fixture
def chip():
    c = ThumbChip(hw_breakpoints=2)
    c.load(LOOP, PROGRAM)
    c.regs["pc"] = LOOP
    return c


def test_bug_corrupt_journal_makes_recover_raise(chip, tmp_path):
    path = tmp_path / "j.json"
    path.write_text("{ not json", encoding="utf-8")
    fbp = FlashBreakpoints(chip, journal=Journal(path, "fake"))
    fbp.recover()                                          # 例外でなく「記録が読めない」を返す・退避する等のはず


@pytest.mark.xfail(strict=True, reason="BUG-FBP-002: Flash に置いた番地へ HW を重ねて求めると比較器が記録されず、1 回外しても比較器が残る")
def test_bug_hw_request_on_a_flash_breakpoint_leaks_a_comparator(chip):
    fbp = FlashBreakpoints(chip)
    assert fbp.set_breakpoint(A, SW)                      # Flash の BKPT として置く（Z0）
    assert fbp.set_breakpoint(A, HW)                      # 同じ番地に比較器も（Z1）
    fbp.remove_breakpoint(A)
    assert chip.find_breakpoint(A) is None                # 外したのに比較器が残る
