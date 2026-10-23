from pathlib import Path

import pytest

from fprobe.linux.ptrace_plan import BreakpointRefused, plan_breakpoint, plan_from_bytes
from fprobe.linux.uprobe import Site, site_of

THUMB = Site("/a", 0x10, 0x10, True, "EM_ARM")
ARM = Site("/a", 0x10, 0x10, False, "EM_ARM")
DATA = Path(__file__).parent / "data" / "linux"


def test_plan_thumb_push():
    p = plan_from_bytes(bytes.fromhex("10b5"), THUMB)
    assert p == {"path": "/a", "offset": 0x10, "thumb": True, "length": 2, "insn": "push {r4, lr}"}


def test_plan_arm_32bit():
    p = plan_from_bytes(bytes.fromhex("10402de9"), ARM)
    assert p["length"] == 4 and p["thumb"] is False


def test_plan_thumb2_wide():
    assert plan_from_bytes(bytes.fromhex("2de9f041"), THUMB)["length"] == 4


def test_plan_refuses_pc_writing_first_insn():
    with pytest.raises(BreakpointRefused, match="PC"):
        plan_from_bytes(bytes.fromhex("fee7"), THUMB)                 # b.n（自分へ）
    with pytest.raises(BreakpointRefused, match="PC"):
        plan_from_bytes(bytes.fromhex("1eff2fe1"), ARM)               # bx lr


def test_plan_refuses_aarch64():
    with pytest.raises(BreakpointRefused, match="32 ビット"):
        plan_from_bytes(bytes.fromhex("fd7bbfa9"), Site("/a", 0, 0, False, "EM_AARCH64"))


def test_plan_breakpoint_reads_elf():
    site = site_of(DATA / "tick_thumb", "/tmp/tick_thumb", symbol="tick")
    p = plan_breakpoint(DATA / "tick_thumb", site)
    assert p["thumb"] is True and p["offset"] == 0x5cc and p["length"] in (2, 4)
