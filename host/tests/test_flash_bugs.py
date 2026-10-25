"""不具合の再現試験（検出専用。修正されるまで xfail(strict)）。台帳: docs/reports/bugs/flash-snapshot-gdb.md"""

from types import SimpleNamespace

import pytest
from flash_fakes import FLASH, FakeChip

from fprobe.flash import FlashMemory, Image, Progress, RegisterGuard


def test_bug_flash_guard_enter_failure_leaves_primask_set():
    chip = FakeChip()
    chip.regs["primask"] = 0
    chip.read32 = lambda a: 0
    chip.write32 = lambda a, v: None
    guard = RegisterGuard([{"op": "wait", "address": 0x20003000, "mask": 1, "value": 1}], [], tries=1)
    memory = FlashMemory(chip, guards=[guard])
    with pytest.raises(RuntimeError):
        memory.erase(FLASH, 1)
    assert chip.regs["primask"] == 0


@pytest.mark.xfail(strict=True, reason="BUG-FLASH-002: 変更が空の patch(preserve=True) が IndexError")
def test_bug_patch_with_no_changes_raises_index_error():
    chip = FakeChip(running=True)
    FlashMemory(chip).patch([])


@pytest.mark.xfail(strict=True, reason="BUG-FLASH-003: 進捗の終わり（浮動小数点の和が 1.0 に僅かに届かない）が間引かれる")
def test_bug_progress_end_is_throttled_when_fraction_is_just_below_one():
    sent, now = [], [0.0]
    p = Progress(sent.append, "program", interval_s=0.1, clock=lambda: now[0])
    p.report("erase", 0.5)
    share_sum = 0.0                          # program() の offset + f * share と同じく順に足す: 0.9999999999999999
    for _ in range(10):
        share_sum += 0.1
    assert share_sum != 1.0 and round(share_sum, 4) == 1.0
    p.report("erase", share_sum)
    assert sent[-1]["fraction"] == 1.0 and len(sent) == 2


@pytest.mark.xfail(strict=True, reason="BUG-FLASH-004: 先頭が ':' (0x3A) の BIN を Intel HEX と誤認する")
def test_bug_bin_starting_with_colon_is_taken_for_hex(tmp_path):
    (tmp_path / "fw.bin").write_bytes(b":\x01\x02\x03")
    image = Image.load(tmp_path / "fw.bin", address=0x08000000)
    assert image.format == "bin" and image.segments[0].data == b":\x01\x02\x03"


def test_bug_negative_read_length_is_accepted():
    with pytest.raises(ValueError):
        FlashMemory(FakeChip()).read(FLASH, -5)


def test_bug_flash_read_past_the_end_is_an_empty_success(tmp_path, monkeypatch):
    from probe_fakes import FakeDevice

    from fprobe.mcp_server import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._debug = SimpleNamespace(target=FakeChip(), options={"frequency": 4_000_000}, close=lambda: None)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            p.flash_read(str(tmp_path / "x.bin"), address=FLASH + 0x10000)
    finally:
        p._stop_observing()
