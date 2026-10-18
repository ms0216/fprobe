"""Probe の Flash のブレークポイント（fprobe.probe_flash_bp）: 道具からの使い方、接続・切断で戻す、Flash の書き込み・
消去との関係、GDB の Z0。偽の Cortex-M（thumb_fake）。"""

import json
from types import SimpleNamespace

import pytest
from flash_fakes import FLASH, SMALL
from intelhex import IntelHex
from thumb_fake import ThumbChip, thumb

from fprobe.flash_bp import BKPT_CODE, FlashBreakpoints
from fprobe.mcp_server import Probe
from probe_fakes import FakeDevice

LOOP = FLASH + 0x100
PROGRAM = thumb(0x2000, 0x3001, 0x3001, 0x3001, 0x3001, 0xE7FA)
A, B, C, D = LOOP + 2, LOOP + 4, LOOP + 6, LOOP + 8


def erases(chip) -> int:
    return sum(op[0] == "erase" for op in chip.ops)


def new_chip(hw: int = 0) -> ThumbChip:
    chip = ThumbChip(hw_breakpoints=hw)
    chip.load(LOOP, PROGRAM)
    chip.regs["pc"] = LOOP
    return chip


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def probe(home):
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    p._stop_observing()


def connect(probe, chip) -> None:
    """target_connect を偽のセッションで通す（記録からの復元も行われる）。"""
    probe._session.debug = lambda **kw: SimpleNamespace(target=chip, open=lambda: None, close=lambda: None,
                                                        options={"frequency": 4_000_000})
    return probe.target_connect("fake_target")


def test_breakpoints_beyond_the_comparators_go_to_flash(probe):
    chip = new_chip(hw=1)
    idle = FLASH + 0x40                                      # 実行されない場所（比較器を 1 つ使い切る）
    chip.load(idle, thumb(0xBF00))
    connect(probe, chip)
    assert isinstance(probe.target, FlashBreakpoints)
    probe.breakpoint_set(hex(idle))
    probe.breakpoint_set(hex(C))
    kinds = {b["address"]: b["kind"] for b in probe.breakpoint_list()["breakpoints"]}
    assert kinds == {idle: "hw", C: "flash"}
    stop = probe.run_until_halt(timeout_s=1)
    assert stop["pc"] == C and stop["reason"] == "breakpoint"
    for _ in range(2):
        assert probe.run_until_halt(timeout_s=1)["pc"] == C
    assert erases(chip) == 1                                 # 書いたのは最初の 1 回だけ
    status = probe.flash_breakpoints()
    assert status["available"] and status["flash"][0]["address"] == C and status["flash"][0]["state"] == "written"
    assert status["stats"]["displaced"] == 2 and "where" in status["flash"][0]


def test_targets_without_flash_are_not_wrapped(probe):
    from cpu_fake import FakeCpu
    cpu = FakeCpu({}, pc=0x100)
    probe._debug = SimpleNamespace(target=cpu, close=lambda: None)
    assert probe.target is cpu
    assert probe.flash_breakpoints()["available"] is False


def test_disconnect_restores_flash_and_removes_the_journal(probe, home):
    chip = new_chip()
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    probe.run_until_halt(timeout_s=1)
    journal = home / "flash_breakpoints" / "fake_target-unused.json"   # ターゲット-プローブ
    assert json.loads(journal.read_text())["entries"][0]["address"] == C
    probe.target_disconnect()
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x3001) and not journal.exists()


def test_connect_recovers_breakpoints_left_by_a_crash(probe, home):
    chip = new_chip()
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    probe.run_until_halt(timeout_s=1)
    probe._flash_bp = None                                   # 異常終了: 戻さずに消えた
    probe._debug = None
    again = Probe("unused")
    again._session = SimpleNamespace(device=FakeDevice())
    out = connect(again, chip)
    assert out["flash_breakpoints_recovered"] == {"restored": [C], "skipped": []}
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x3001)
    again._stop_observing()


def test_restore_tool_writes_originals_and_keeps_the_breakpoint(probe):
    chip = new_chip()
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    probe.run_until_halt(timeout_s=1)
    assert probe.flash_breakpoints_restore() == {"restored": 1}
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x3001)
    assert probe.flash_breakpoints()["flash"][0]["state"] == "pending"


def test_disabling_needs_the_flash_breakpoints_removed_first(probe):
    chip = new_chip()
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    with pytest.raises(ValueError):
        probe.flash_breakpoints(enabled=False)
    probe.breakpoint_clear()
    assert probe.flash_breakpoints(enabled=False)["enabled"] is False
    with pytest.raises(RuntimeError):                        # 比較器が尽きた: 置けない
        probe.breakpoint_set(hex(C))


def test_reprogramming_flash_keeps_the_breakpoints_and_the_new_code(probe, tmp_path):
    chip = new_chip()
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    probe.run_until_halt(timeout_s=1)
    ih = IntelHex()
    ih.puts(LOOP, PROGRAM[:2])                               # C の無い先頭だけ書き直す（同じセクタ）
    path = tmp_path / "fw.hex"
    ih.write_hex_file(str(path))
    out = probe.flash(str(path), reset=False)
    assert out["verify"]["match"]
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x3001)    # 書いた後は元の中身
    assert probe.flash_breakpoints()["flash"][0]["state"] == "pending"
    chip.regs["pc"] = LOOP
    assert probe.run_until_halt(timeout_s=1)["pc"] == C      # 次の再開で書き直して止まる


def test_erasing_forgets_the_written_breakpoints(probe):
    chip = new_chip()
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    probe.run_until_halt(timeout_s=1)
    probe.flash_erase("range", LOOP, 2)
    assert chip.flash[C - FLASH] == 0xFF and probe.flash_breakpoints()["flash"][0]["state"] == "pending"
    assert probe.flash_breakpoints()["dormant"] == []


def test_gdb_software_breakpoints_use_flash_and_step_without_rewriting(probe):
    chip = new_chip()
    connect(probe, chip)
    probe.gdb_breakpoint("g1", True, "break", C, 2)
    probe.gdb_resume()
    assert probe.gdb_stop()["pc"] == C and probe.gdb_stop()["breakpoint"] == "sw"
    probe.gdb_step()
    assert chip.regs["pc"] == D and erases(chip) == 1
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == BKPT_CODE


def test_line_far_from_loop_in_another_sector(probe):
    chip = new_chip()
    far = FLASH + SMALL + 0x10
    chip.load(far, thumb(0xBF00))
    connect(probe, chip)
    probe.breakpoint_set(hex(C))
    probe.breakpoint_set(hex(far))
    probe.run_until_halt(timeout_s=1)
    assert erases(chip) == 2                                 # セクタごとに 1 回
