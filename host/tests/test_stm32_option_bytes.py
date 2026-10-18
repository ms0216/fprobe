"""fprobe.stm32_option_bytes: 系列・解読・変更の計画（確認の約束）・書き込みの手順（FLASH の模型で）。"""

import pytest
from stm32_flash_fake import FakeStm32Flash

from fprobe import stm32_option_bytes as ob
from fprobe.coresight_id import Part

NO_SLEEP = lambda s: None                                      # noqa: E731


def test_families_by_part():
    f401 = ob.family_for(0x433)
    assert f401.name == "f4" and f401.option_address == 0x40023C14 and f401.field("nwrp").width == 8
    assert ob.family_for(0x423).field("nWRP").width == 6
    assert "nWRP" not in [f.name for f in ob.family_for(0x463).fields]          # セクタ数の分からない品種
    assert ob.family_for(0x435).name == "l4" and ob.family_for(0x435).option_address == 0x40022020
    assert ob.family_for(0x468).name == "g4" and ob.family_for(0x460).name == "g0"
    with pytest.raises(ob.OptionBytesError):
        ob.family_for(0x410)                                                    # F1 は扱わない
    with pytest.raises(ob.OptionBytesError):
        ob.family_for(Part("Nordic Semiconductor", "nRF52840", "", 0x52840))


def test_decode_the_f4_default():
    d = ob.decode(ob.family_for(0x433), 0x0FFFAAED)
    assert d["fields"]["RDP"] == 0xAA and d["rdp_level"] == 0
    assert d["fields"]["nWRP"] == 0xFF and d["fields"]["BOR_LEV"] == 3 and d["fields"]["WDG_SW"] == 1
    assert "RDP" in d["descriptions"]


def test_rdp_levels():
    assert [ob.rdp_level(v) for v in (0xAA, 0xBB, 0x00, 0xCC)] == [0, 1, 1, 2]


def test_plan_keeps_unnamed_bits_and_enforces_confirmations():
    f4 = ob.family_for(0x433)
    new, notes = ob.plan_write(f4, 0x8FFFAAED, {"nWRP": 0xFE})
    assert new == 0x8FFEAAED and not notes                                     # bit 31 等は保つ
    with pytest.raises(ValueError, match="Level 2"):
        ob.plan_write(f4, 0x0FFFAAED, {"RDP": 0xCC}, confirm_protect=True)
    with pytest.raises(ValueError, match="confirm_protect"):
        ob.plan_write(f4, 0x0FFFAAED, {"RDP": 0xBB})
    with pytest.raises(ValueError, match="confirm_mass_erase"):
        ob.plan_write(f4, 0x0FFFBBED, {"RDP": 0xAA})
    new, notes = ob.plan_write(f4, 0x0FFFBBED, {"RDP": 0xAA}, confirm_mass_erase=True)
    assert (new >> 8) & 0xFF == 0xAA and "全消去" in notes[0]
    with pytest.raises(ValueError):
        ob.plan_write(f4, 0, {"nWRP": 0x100})
    with pytest.raises(ValueError):
        ob.plan_write(f4, 0, {"FOO": 1})
    with pytest.raises(ValueError):
        ob.plan_write(f4, 0, {})


def test_f4_programming_unlocks_starts_waits_and_locks():
    flash = FakeStm32Flash("optcr", 0x0FFFBBED)
    family = ob.family_for(0x433)
    new, _ = ob.plan_write(family, flash.read32(family.option_address), {"RDP": 0xAA}, confirm_mass_erase=True)
    log = ob.program(flash, family, new, sleep=NO_SLEEP)
    assert flash.programmed == [0x0FFFAAEC] and flash.mass_erased
    assert flash.option & 1 and log[0] == "OPTCR を開いた"


def test_f4_flash_errors_are_raised():
    flash = FakeStm32Flash("optcr", 0x0FFFAAED)
    flash.sr = 1 << 4                                                          # 前回の WRPERR は消してから
    family = ob.family_for(0x433)
    ob.program(flash, family, 0x0FFEAAED, sleep=NO_SLEEP)
    assert flash.sr == 0

    class Failing(FakeStm32Flash):
        def _commit(self):
            super()._commit()
            self.sr |= 1 << 7                                                  # PGSERR
    bad = Failing("optcr", 0x0FFFAAED)
    with pytest.raises(ob.OptionBytesError, match="FLASH の誤り"):
        ob.program(bad, family, 0x0FFEAAED, sleep=NO_SLEEP)


def test_busy_timeout():
    flash = FakeStm32Flash("optcr", 0x0FFFAAED, busy_reads=10**9)
    with pytest.raises(ob.OptionBytesError, match="BSY"):
        ob.program(flash, ob.family_for(0x433), 0x0FFEAAED, timeout_s=0.0, sleep=NO_SLEEP)


def test_optr_programming_and_obl_launch():
    flash = FakeStm32Flash("optr", 0xFFEFF8BB)
    family = ob.family_for(0x435)
    new, _ = ob.plan_write(family, flash.read32(family.option_address), {"RDP": 0xAA}, confirm_mass_erase=True)
    log = ob.program(flash, family, new, sleep=NO_SLEEP)
    assert flash.programmed == [0xFFEFF8AA] and not flash.launched and flash.cr & (1 << 31)
    assert "OBL_LAUNCH" in log[-1]
    flash2 = FakeStm32Flash("optr", 0xFFEFF8BB)
    log = ob.program(flash2, family, new, launch=True, sleep=NO_SLEEP)
    assert flash2.launched and flash2.mass_erased and log[-1].startswith("OBL_LAUNCH")


def test_protection_status_for_the_diagnosis():
    part = Part("STMicroelectronics", "STM32F401xD/E", "", 0x433)
    flash = FakeStm32Flash("optcr", 0x0FFFBBED)
    status = ob.protection_status(part, flash.read32)
    assert status["status"] == "warn" and status["rdp_level"] == 1 and "target_unlock" in status["message"]
    assert ob.protection_status(part, FakeStm32Flash("optcr").read32)["status"] == "ok"
    assert ob.protection_status(Part("Raspberry Pi", "RP2040", "", 2), lambda a: 0) is None
