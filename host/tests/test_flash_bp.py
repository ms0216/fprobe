"""fprobe.flash_bp: Flash 上の無制限ブレークポイント（ハードの比較器が尽きたら Flash の命令を BKPT に書き換える）。

偽の Cortex-M（thumb_fake: 本物の Thumb のバイト列を実行し、Flash は pyOCD の本物の FlashBuilder で書く）で、
書き換えの回数（セクタの消去の数）・踏み越え・切断と異常終了からの復元を確かめる。
"""

import json

import pytest
from flash_fakes import FLASH, SMALL
from pyocd.core.target import Target
from thumb_fake import BKPT, DFSR, MPU_CTRL, ThumbChip, thumb

from fprobe.debug import Debugger
from fprobe.flash import Image, Segment
from fprobe.flash_bp import BKPT_CODE, FlashBreakpoints, Journal
from fprobe.stepping import Runner

LOOP = FLASH + 0x100
# movs r0,#0 / adds r0,#1 ×4 / b LOOP+2（終わらないループ）
PROGRAM = thumb(0x2000, 0x3001, 0x3001, 0x3001, 0x3001, 0xE7FA)
A, B, C, D = LOOP + 2, LOOP + 4, LOOP + 6, LOOP + 8
FAR = FLASH + SMALL + 0x40                                   # 別のセクタ（実行されない）
AUTO, HW, SW = Target.BreakpointType.AUTO, Target.BreakpointType.HW, Target.BreakpointType.SW


def erases(chip) -> list[int]:
    return [op[1] for op in chip.ops if op[0] == "erase"]


@pytest.fixture
def chip():
    c = ThumbChip(hw_breakpoints=2)
    c.load(LOOP, PROGRAM)
    c.load(FAR, thumb(0xBF00, 0xBF00))
    c.regs["pc"] = LOOP
    return c


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "flash_bp.json", "fake")


def make(chip, journal=None, **kw) -> FlashBreakpoints:
    return FlashBreakpoints(chip, journal=journal, **kw)


def run(fbp) -> int:
    """再開して止まった PC（偽物は同期で走る）。"""
    fbp.resume()
    assert fbp.get_state() == Target.State.HALTED
    return fbp.read_core_register("pc")


# --- 置き方と書くとき ---------------------------------------------------------------------

def test_hardware_comparators_first_then_flash(chip):
    fbp = make(chip)
    assert fbp.set_breakpoint(A) and fbp.set_breakpoint(B)
    assert chip.hw == {A, B} and erases(chip) == []
    assert fbp.set_breakpoint(C)                             # 比較器は尽きた: Flash へ（まだ書かない）
    assert erases(chip) == [] and fbp.kind_of(C) == "flash"
    assert fbp.status()["flash"] == [{"address": C, "state": "pending"}]


def test_flash_breakpoint_is_written_just_before_resuming_and_stops_the_core(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    assert erases(chip) == []
    assert run(fbp) == C
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == BKPT_CODE and chip.dfsr & BKPT
    assert len(erases(chip)) == 1 and fbp.status()["stats"]["commits"] == 1


def test_a_pending_breakpoint_takes_a_freed_comparator_instead_of_writing(chip):
    fbp = make(chip)
    for a in (A, B, C):
        fbp.set_breakpoint(a)
    fbp.remove_breakpoint(A)                                 # 比較器が空いた: まだ書いていない C はハードへ
    assert run(fbp) == B
    assert fbp.kind_of(C) == "hw" and erases(chip) == []


def test_breakpoints_in_one_sector_are_written_with_one_erase(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    for a in (A, B, C, D):
        fbp.set_breakpoint(a)
    run(fbp)
    assert len(erases(chip)) == 1
    assert fbp.status()["stats"]["sector_writes"] == 1


def test_reads_show_the_original_instructions(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    assert bytes(fbp.read_memory_block8(LOOP, len(PROGRAM))) == PROGRAM
    assert fbp.read16(C) == 0x3001 and fbp.read32(C) == 0x30013001
    assert fbp.read_memory_block32(C, 1) == [0x30013001] and fbp.read_memory(C, 16) == 0x3001
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == BKPT_CODE


def test_resuming_from_a_flash_breakpoint_does_not_rewrite_flash(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    dbg = Debugger(fbp)
    dbg.breakpoint(C)
    assert dbg.run_until_halt(timeout_s=1).pc == C
    first = chip.regs["r0"]
    for _ in range(3):
        stop = dbg.run_until_halt(timeout_s=1)               # 踏み越え（RAM で 1 命令）→ 1 周して C
        assert stop.pc == C and stop.reason == "breakpoint"
    assert chip.regs["r0"] == first + 12                     # 1 周で adds が 4 回（踏み越えた命令も実行された）
    assert len(erases(chip)) == 1
    assert fbp.status()["stats"]["displaced"] == 3


# --- 外したもの・踏み越え ------------------------------------------------------------------

def test_a_removed_breakpoint_stays_in_flash_and_is_passed_transparently(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    fbp.remove_breakpoint(C)                                 # 書き戻さない（次に要るときまで）
    chip.run_limit = 4                                       # 以後、再開は 4 命令だけ進んで走ったまま
    fbp.resume()                                             # いまの PC の BKPT は踏み越える: C → D → b → A → B
    assert fbp.get_state() == Target.State.RUNNING
    chip.halt()
    chip.regs["pc"] = LOOP
    fbp.resume()                                             # LOOP → A → B → C の BKPT で止まる
    assert fbp.get_state() == Target.State.RUNNING           # 黙って踏み越えて続ける
    assert len(erases(chip)) == 1 and fbp.status()["dormant"] == [C]
    assert fbp.status()["stats"]["dormant_hits"] == 1


def test_a_dormant_breakpoint_hit_repeatedly_is_restored(chip):
    chip.hw_limit = 0
    fbp = make(chip, dormant_limit=3)
    fbp.set_breakpoint(C)
    run(fbp)
    fbp.remove_breakpoint(C)
    fbp.resume()
    assert fbp.get_state() == Target.State.RUNNING
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x3001)
    assert len(erases(chip)) == 2 and fbp.status()["dormant"] == []


def test_resuming_on_a_dormant_breakpoint_counts_as_a_hit(chip):
    """再開の時点で PC が休眠中の BKPT の上（模擬した BL の飛び先等）でも当たりとして数え、dormant_limit 回で戻す
    （2026-10-18 に実機で、BL の飛び先の休眠中の BKPT を黙って踏み越え続け、いつまでも戻さなかった）。"""
    chip.hw_limit = 0
    fbp = make(chip, dormant_limit=3)
    fbp.set_breakpoint(C)
    fbp.set_breakpoint(D)
    assert run(fbp) == C
    fbp.remove_breakpoint(C)
    for _ in range(3):
        fbp.write_core_register("pc", B)
        fbp.step()                                           # B → C（休眠中の BKPT の上に着く）
        assert fbp.read_core_register("pc") == C
        assert run(fbp) == D
    assert fbp.status()["dormant"] == []
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x3001)


def test_dormant_breakpoints_are_restored_along_with_a_write_to_the_same_sector(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    fbp.remove_breakpoint(C)
    fbp.set_breakpoint(D)
    assert run(fbp) == D                                     # 同じセクタを書くついでに C を戻す
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x3001)
    assert len(erases(chip)) == 2 and fbp.status()["dormant"] == []


def test_reinserting_a_dormant_breakpoint_costs_no_write(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    fbp.remove_breakpoint(C)
    fbp.set_breakpoint(C)
    assert fbp.status()["flash"] == [{"address": C, "state": "written"}]
    assert len(erases(chip)) == 1


def test_step_on_a_flash_breakpoint_runs_the_original_instruction(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    r0 = chip.regs["r0"]
    fbp.remove_breakpoint(C)
    fbp.step()
    fbp.set_breakpoint(C)
    assert chip.regs["pc"] == D and chip.regs["r0"] == r0 + 1
    assert len(erases(chip)) == 1                            # ステップでは Flash を書かない


def test_instruction_that_cannot_run_elsewhere_is_stepped_by_rewriting(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    chip.periph[MPU_CTRL] = 1                                # RAM で実行できない: 書き戻して進める
    fbp.step()
    assert chip.regs["pc"] == D and fbp.status()["stats"]["rewrites"] == 1
    assert fbp.status()["flash"] == [{"address": C, "state": "pending"}]     # 次の再開で書き直す
    assert run(fbp) == C and len(erases(chip)) == 3


def test_temporary_breakpoints_of_stepping_get_the_hardware(chip):
    user = {A: "a", B: "b"}
    fbp = make(chip, persistent=lambda a: a in user)
    for a in user:
        fbp.set_breakpoint(a)
    assert chip.hw == {A, B}
    runner = Runner(fbp, user)
    halt = runner.run_to({D: lambda regs: True})             # 踏み台 D: 利用者の 1 つを Flash へ移してハードを空ける
    assert halt.reason in ("arrived", "breakpoint")
    assert fbp.kind_of(D) is None                            # 踏み台は外した
    assert sum(fbp.kind_of(a) == "flash" for a in user) == 1 and len(erases(chip)) == 1
    runner.run_to({D: lambda regs: True})                    # 2 回目はハードが空いている: 書かない
    assert len(erases(chip)) == 1


def test_setting_while_running_writes_at_once(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    chip.run_limit = 0                                       # 走らせ直しても進まない（走ったまま）
    chip.state = Target.State.RUNNING
    assert fbp.set_breakpoint(C)
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == BKPT_CODE
    assert chip.state == Target.State.RUNNING                # 止めて書き、走らせ直す


def test_reset_writes_pending_breakpoints_first(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    fbp.reset()
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == BKPT_CODE and ("reset",) in chip.ops


def test_explicit_hardware_and_software_requests(chip):
    chip.hw_limit = 1
    fbp = make(chip)
    assert fbp.set_breakpoint(A, HW) and not fbp.set_breakpoint(B, HW)   # Z1: ハードだけ
    assert fbp.set_breakpoint(C, SW) and fbp.kind_of(C) == "flash"        # ソフト: 比較器を使わない
    assert fbp.find_breakpoint(C).type == SW and fbp.find_breakpoint(A).type == HW


def test_disabled_behaves_like_the_plain_target(chip):
    chip.hw_limit = 0
    fbp = make(chip, enabled=False)
    assert not fbp.set_breakpoint(C)


def test_odd_addresses_are_refused(chip):
    with pytest.raises(ValueError):
        make(chip).set_breakpoint(C + 1)


# --- 元に戻す ------------------------------------------------------------------------------

def test_close_restores_flash_and_removes_the_journal(chip, journal):
    chip.hw_limit = 0
    fbp = make(chip, journal)
    fbp.set_breakpoint(C)
    fbp.set_breakpoint(FAR)
    run(fbp)
    assert journal.path.is_file()
    entries = json.loads(journal.path.read_text())["entries"]
    assert sorted(e["address"] for e in entries) == [C, FAR]
    fbp.close()
    assert bytes(chip.flash[LOOP - FLASH:LOOP - FLASH + len(PROGRAM)]) == PROGRAM
    assert bytes(chip.flash[FAR - FLASH:FAR - FLASH + 2]) == thumb(0xBF00)
    assert not journal.path.exists()


def test_restore_does_not_overwrite_flash_that_no_longer_holds_our_bkpt(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    chip.flash[C - FLASH:C - FLASH + 2] = thumb(0x2007)       # 誰かが書き換えた
    fbp.close()
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == thumb(0x2007)


def test_journal_restores_breakpoints_left_by_a_crashed_process(chip, journal):
    chip.hw_limit = 0
    fbp = make(chip, journal)
    fbp.set_breakpoint(C)
    fbp.set_breakpoint(D)
    run(fbp)
    del fbp                                                  # 異常終了（元に戻さずに消えた）
    again = make(chip, Journal(journal.path, "fake"))
    out = again.recover()
    assert sorted(out["restored"]) == [C, D] and out["skipped"] == []
    assert bytes(chip.flash[LOOP - FLASH:LOOP - FLASH + len(PROGRAM)]) == PROGRAM
    assert not journal.path.exists()


def test_journal_leaves_reprogrammed_flash_alone(chip, journal):
    chip.hw_limit = 0
    fbp = make(chip, journal)
    fbp.set_breakpoint(C)
    run(fbp)
    chip.flash[B - FLASH:B - FLASH + 2] = thumb(0x2009)       # 別のファームウェアが書かれた（BKPT の近くが違う）
    out = make(chip, Journal(journal.path, "fake")).recover()
    assert out["restored"] == [] and out["skipped"] == [C]
    assert bytes(chip.flash[C - FLASH:C - FLASH + 2]) == BKPT_CODE
    assert not journal.path.exists()


def test_journal_of_another_target_is_not_used(chip, journal):
    chip.hw_limit = 0
    fbp = make(chip, journal)
    fbp.set_breakpoint(C)
    run(fbp)
    out = make(chip, Journal(journal.path, "other")).recover()
    assert out == {"restored": [], "skipped": []} and journal.path.exists()


# --- Flash の書き込み・消去との関係 -----------------------------------------------------------

def test_reprogramming_keeps_original_bytes_where_the_image_does_not_reach(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    image = Image([Segment(LOOP, thumb(0x2005))])            # C は書かないが、同じセクタを書く
    folded = fbp.fold(image)
    assert any(s.address <= C < s.end for s in folded.segments)
    fbp.forget(image)
    assert fbp.status()["flash"] == [{"address": C, "state": "pending"}]
    assert fbp.status()["dormant"] == []


def test_erasing_forgets_written_breakpoints(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    fbp.set_breakpoint(FAR)
    run(fbp)
    fbp.forget_range(FAR, 2)
    assert {e["address"]: e["state"] for e in fbp.status()["flash"]} == {C: "written", FAR: "pending"}
    fbp.forget_all()
    assert all(e["state"] == "pending" for e in fbp.status()["flash"])


def test_dfsr_bkpt_from_flash_is_reported_as_a_breakpoint(chip):
    chip.hw_limit = 0
    fbp = make(chip)
    fbp.set_breakpoint(C)
    run(fbp)
    assert fbp.get_halt_reason() == Target.HaltReason.BREAKPOINT and fbp.read32(DFSR) & BKPT
