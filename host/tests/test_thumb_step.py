"""fprobe.thumb_step: Flash の BKPT の下の元の命令を、Flash を書き換えずに 1 つ実行する（模擬・RAM での実行・書き換えが要る）。"""

import pytest
from flash_fakes import FLASH, RAM
from thumb_fake import CFSR, DFSR, HALTED, MPU_CTRL, XPSR_T, Z, ThumbChip, bl, thumb

from fprobe.thumb_step import NeedsRewrite, OutOfLine, invalidate_icache, plan

AT = FLASH + 0x100
SCRATCH = RAM + 0x2000


@pytest.mark.parametrize("hexcode, how", [
    ("00bf", "displace"),            # nop
    ("0520", "displace"),            # movs r0, #5
    ("7047", "displace"),            # bx lr（PC を書くだけ）
    ("00bd", "displace"),            # pop {pc}
    ("5df804fb", "displace"),        # ldr pc, [sp], #4
    ("01df", "displace"),            # svc #1（積まれた戻り先を直す）
    ("02e0", "emulate"),             # b
    ("01d0", "emulate"),             # beq
    ("40f00080", "emulate"),         # bne.w
    ("00f002f8", "emulate"),         # bl
    ("08b1", "emulate"),             # cbz
    ("9847", "emulate"),             # blx r3（LR に次の番地）
    ("dfe801f0", "emulate"),         # tbb [pc, r1]
    ("dfe811f0", "emulate"),         # tbh [pc, r1, lsl #1]
    ("0248", "emulate"),             # ldr r0, [pc, #8]
    ("dff80800", "emulate"),         # ldr.w r0, [pc, #8]
    ("01a0", "emulate"),             # adr r0, #4
    ("aff20400", "emulate"),         # subw r0, pc, #4（adr の後ろ向き）
    ("00be", "rewrite"),             # bkpt（元から BKPT）
    ("8744", "rewrite"),             # add pc, r0（PC を読む、模擬しない形）
    ("7846", "rewrite"),             # mov r0, pc
    ("dfe90201", "rewrite"),         # ldrd r0, r1, [pc, #8]
])
def test_plan_classifies_instructions(hexcode, how):
    assert plan(bytes.fromhex(hexcode), AT, XPSR_T).how == how


def test_pc_relative_instructions_inside_an_it_block_need_a_rewrite():
    itstate = 0b0100 << 10                                   # IT ブロックの途中（ITSTATE ≠ 0）
    assert plan(bytes.fromhex("01d0"), AT, XPSR_T | itstate).how == "rewrite"
    assert plan(bytes.fromhex("0520"), AT, XPSR_T | itstate).how == "displace"   # PC を読まない命令は RAM で


def test_undecodable_bytes_need_a_rewrite():
    assert plan(b"\xff\xff", AT, XPSR_T).how == "rewrite"


def chip_at(code: bytes) -> ThumbChip:
    chip = ThumbChip()
    chip.load(AT, code)
    chip.regs["pc"] = AT
    chip.dfsr = 2                                            # BKPT で止まっていた
    return chip


def run(chip: ThumbChip, code: bytes, read=None) -> str:
    return OutOfLine(chip, SCRATCH, read=read).step(AT, code)


# --- 模擬 ---

def test_branch_is_emulated_without_executing():
    code = thumb(0xE002)                                     # b AT+8
    chip = chip_at(code)
    assert run(chip, code) == "emulated"
    assert chip.regs["pc"] == AT + 8 and chip.steps == [] and chip.dfsr == 0


def test_conditional_branch_follows_the_flags():
    code = thumb(0xD001)                                     # beq AT+6
    chip = chip_at(code)
    chip.regs["xpsr"] = XPSR_T | Z
    run(chip, code)
    assert chip.regs["pc"] == AT + 6
    chip = chip_at(code)
    run(chip, code)
    assert chip.regs["pc"] == AT + 2


def test_bl_sets_the_link_register():
    code = bl(AT, AT + 0x40)
    chip = chip_at(code)
    run(chip, code)
    assert chip.regs["pc"] == AT + 0x40 and chip.regs["lr"] == (AT + 4) | 1


def test_blx_register_links_to_the_original_next_instruction():
    code = thumb(0x4798)                                     # blx r3
    chip = chip_at(code)
    chip.regs["r3"] = (FLASH + 0x400) | 1
    run(chip, code)
    assert chip.regs["pc"] == FLASH + 0x400 and chip.regs["lr"] == (AT + 2) | 1


def test_cbz_and_cbnz():
    code = thumb(0xB108)                                     # cbz r0, AT+6
    chip = chip_at(code)
    chip.regs["r0"] = 0
    run(chip, code)
    assert chip.regs["pc"] == AT + 6
    chip = chip_at(code)
    chip.regs["r0"] = 7
    run(chip, code)
    assert chip.regs["pc"] == AT + 2


def test_literal_load_reads_through_the_given_reader():
    code = thumb(0x4801)                                     # ldr r0, [pc, #4]
    chip = chip_at(code)
    literal = ((AT + 4) & ~3) + 4
    chip.load(literal, (0x12345678).to_bytes(4, "little"))
    seen = []

    def read(address, length):
        seen.append(address)
        return bytes(chip.read_memory_block8(address, length))

    run(chip, code, read)
    assert chip.regs["r0"] == 0x12345678 and chip.regs["pc"] == AT + 2 and seen == [literal]


def test_adr_and_table_branch():
    code = thumb(0xA001)                                     # adr r0, #4
    chip = chip_at(code)
    run(chip, code)
    assert chip.regs["r0"] == ((AT + 4) & ~3) + 4
    code = thumb(0xE8DF, 0xF001) + bytes([2, 5])             # tbb [pc, r1] と表
    chip = chip_at(code)
    chip.regs["r1"] = 1
    run(chip, code)
    assert chip.regs["pc"] == AT + 4 + 2 * 5


# --- RAM での実行 ---

def test_plain_instruction_runs_in_scratch_ram_and_returns_to_the_next_address():
    code = thumb(0x2005)                                     # movs r0, #5
    chip = chip_at(code)
    chip.load(SCRATCH, b"\x11" * 8)
    assert run(chip, code) == "displaced"
    assert chip.regs["r0"] == 5 and chip.regs["pc"] == AT + 2
    assert chip.steps == [SCRATCH] and chip.step_masks == [True] and chip.dfsr == HALTED
    assert bytes(chip.read_memory_block8(SCRATCH, 8)) == b"\x11" * 8      # 借りた RAM は元に戻す
    assert chip.icache_invalidations >= 1


def test_instruction_that_writes_pc_keeps_its_destination():
    code = thumb(0xBD00)                                     # pop {pc}
    chip = chip_at(code)
    chip.regs["sp"] -= 4
    chip.write32(chip.regs["sp"], (FLASH + 0x300) | 1)
    run(chip, code)
    assert chip.regs["pc"] == FLASH + 0x300


def test_exception_entered_by_the_instruction_gets_the_original_return_address():
    code = thumb(0xDF01)                                     # svc #1
    chip = chip_at(code)
    chip.handlers[11] = FLASH + 0x800
    run(chip, code)
    assert chip.regs["pc"] == FLASH + 0x800
    assert chip.read32(chip.regs["sp"] + 24) == AT + 2       # 積まれた戻り先は Flash の元の番地の次


def test_memory_access_that_overlaps_the_scratch_area_needs_a_rewrite():
    code = thumb(0x6008)                                     # str r0, [r1]
    chip = chip_at(code)
    chip.regs["r1"] = SCRATCH + 4
    before = dict(chip.regs)
    with pytest.raises(NeedsRewrite):
        run(chip, code)
    assert chip.regs == before and chip.steps == []


def test_enabled_mpu_needs_a_rewrite():
    code = thumb(0x2005)
    chip = chip_at(code)
    chip.periph[MPU_CTRL] = 1
    with pytest.raises(NeedsRewrite):
        run(chip, code)


def test_rewrite_plans_raise():
    code = thumb(0x4487)                                     # add pc, r0
    with pytest.raises(NeedsRewrite):
        run(chip_at(code), code)


def test_cfsr_is_untouched_by_emulation():
    code = thumb(0xE002)
    chip = chip_at(code)
    chip.periph[CFSR] = 0x100
    run(chip, code)
    assert chip.periph[CFSR] == 0x100 and chip.read32(DFSR) == 0


def test_instruction_cache_is_invalidated_on_the_selected_core():
    # pyOCD の SoC のターゲットは invalidate_instruction_cache をコアへ渡さない
    calls = []
    core = type("Core", (), {"invalidate_instruction_cache": lambda self: calls.append("core")})()
    soc = type("Soc", (), {"selected_core": core})()
    invalidate_icache(soc)
    invalidate_icache(object())                              # 無ければ何もしない
    assert calls == ["core"]
