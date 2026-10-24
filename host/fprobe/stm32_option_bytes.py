"""STM32 のオプションバイト（U30）: 読み出し保護（RDP）・書き込み保護・BOR・ウォッチドッグ・リセット・ブートの設定を
名前で読み書きし、RDP を Level 1 → 0 に戻す（全消去を伴う）。STM32CubeProgrammer の -ob に当たる。

系列（参照マニュアルの FLASH の章）:
- f4: FLASH_OPTCR（0x40023C14）。OPTKEYR で開き、OPTCR に書いて OPTSTRT。RDP を 1 → 0 にすると、その場で全消去。
- l4 / g4 / g0: FLASH_OPTR（0x40022020）。CR の LOCK と OPTLOCK を鍵で開き、OPTR に書いて OPTSTRT。反映は
  OBL_LAUNCH（ターゲットがリセットされ接続が切れる）か次の電源投入。RDP を 1 → 0 にすると反映のときに全消去。

安全のための約束（この単位の外からも変えない）:
- RDP Level 2（0xCC）は書かない（デバッグが永久に塞がり、戻せない）。
- RDP を下げる（1 → 0 = 全消去）には confirm_mass_erase、上げる（0 → 1 = Flash を読めなくする）には
  confirm_protect が要る。無ければ ValueError で断り、何も書かない。
- 名前の無いビット（予約・この表に無い機能）は今の値を保つ。

機器には Memory（read32 / write32）で触る。部品の判別は fprobe.coresight_id（DBGMCU の DEV_ID）。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .coresight_id import STM32_PARTS, Part

__all__ = ["FAMILIES", "Family", "OptionField", "Memory", "OptionBytesError", "decode", "family_for", "plan_write",
           "program", "protection_status", "rdp_level"]

KEY1, KEY2 = 0x45670123, 0xCDEF89AB                  # FLASH_KEYR
OPTKEY1, OPTKEY2 = 0x08192A3B, 0x4C5D6E7F            # FLASH_OPTKEYR
RDP_LEVEL0, RDP_LEVEL2, RDP_LEVEL1_DEFAULT = 0xAA, 0xCC, 0xBB
BSY = 1 << 16


class Memory(Protocol):
    def read32(self, address: int) -> int: ...
    def write32(self, address: int, value: int) -> None: ...


class OptionBytesError(RuntimeError):
    pass


@dataclass(frozen=True)
class OptionField:
    name: str
    lsb: int
    width: int
    summary: str

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lsb

    def get(self, raw: int) -> int:
        return (raw >> self.lsb) & ((1 << self.width) - 1)


@dataclass(frozen=True)
class Family:
    name: str
    style: str                                        # optcr（F4）/ optr（L4・G4・G0）
    flash_base: int
    fields: tuple[OptionField, ...]
    sr_errors: int                                    # SR の誤りのビット（書いて 1 で消す）

    @property
    def option_address(self) -> int:
        return self.flash_base + (0x14 if self.style == "optcr" else 0x20)

    @property
    def sr(self) -> int:
        return self.flash_base + (0x0C if self.style == "optcr" else 0x10)

    @property
    def cr(self) -> int:
        return self.flash_base + (0x10 if self.style == "optcr" else 0x14)

    @property
    def keyr(self) -> int:
        return self.flash_base + (0x04 if self.style == "optcr" else 0x08)

    @property
    def optkeyr(self) -> int:
        return self.flash_base + (0x08 if self.style == "optcr" else 0x0C)

    def field(self, name: str) -> OptionField:
        for f in self.fields:
            if f.name.upper() == name.upper():
                return f
        raise ValueError(f"{self.name} のオプションバイトに {name!r} は無い（{', '.join(f.name for f in self.fields)}）")


_F = OptionField
_RDP = _F("RDP", 0, 8, "読み出し保護: 0xAA = Level 0、0xCC = Level 2（永久。書かない）、ほか = Level 1")
_OPTR_COMMON = (_RDP, _F("nRST_STOP", 12, 1, "0 = STOP に入るとリセット"),
                _F("nRST_STDBY", 13, 1, "0 = STANDBY に入るとリセット"), _F("nRST_SHDW", 14, 1, "0 = SHUTDOWN に入るとリセット"),
                _F("IWDG_SW", 16, 1, "1 = IWDG はソフトウェアで開始"), _F("IWDG_STOP", 17, 1, "0 = STOP で IWDG を止める"),
                _F("IWDG_STDBY", 18, 1, "0 = STANDBY で IWDG を止める"), _F("WWDG_SW", 19, 1, "1 = WWDG はソフトウェアで開始"),
                _F("BOR_LEV", 8, 3, "BOR の閾値（0〜4）"))
_SR_OPTR = (1 << 1) | (0x7F << 3) | (1 << 14) | (1 << 15)   # OPERR・PROGERR〜FASTERR・RDERR・OPTVERR


def _f4(nwrp_bits: int | None) -> Family:
    fields = [_F("BOR_LEV", 2, 2, "BOR の閾値: 3 = 無効、2 = Level 1、1 = Level 2、0 = Level 3"),
              _F("WDG_SW", 5, 1, "1 = IWDG はソフトウェアで開始"), _F("nRST_STOP", 6, 1, "0 = STOP に入るとリセット"),
              _F("nRST_STDBY", 7, 1, "0 = STANDBY に入るとリセット"), _F("RDP", 8, 8, _RDP.summary)]
    if nwrp_bits:
        fields.append(_F("nWRP", 16, nwrp_bits, f"セクタ 0〜{nwrp_bits - 1} の書き込み保護（ビット = 0 で保護）"))
    return Family("f4", "optcr", 0x40023C00, tuple(fields), (1 << 1) | (0xF << 4))


FAMILIES: dict[str, Family] = {
    "l4": Family("l4", "optr", 0x40022000, _OPTR_COMMON + (
        _F("BFB2", 20, 1, "1 = Bank 2 から起動"), _F("DUALBANK", 21, 1, "1 = 2 バンク（1 MB 未満の品種）"),
        _F("nBOOT1", 23, 1, "ブートの選択"), _F("SRAM2_PE", 24, 1, "0 = SRAM2 のパリティ検査"),
        _F("SRAM2_RST", 25, 1, "0 = システムリセットで SRAM2 を消す"), _F("nSWBOOT0", 26, 1, "0 = BOOT0 を nBOOT0 で決める"),
        _F("nBOOT0", 27, 1, "nSWBOOT0 = 0 のときの BOOT0")), _SR_OPTR),
    "g4": Family("g4", "optr", 0x40022000, _OPTR_COMMON + (
        _F("BFB2", 20, 1, "1 = Bank 2 から起動"), _F("DBANK", 22, 1, "1 = 2 バンク"), _F("nBOOT1", 23, 1, "ブートの選択"),
        _F("SRAM_PE", 24, 1, "0 = SRAM のパリティ検査"), _F("CCMSRAM_RST", 25, 1, "0 = システムリセットで CCM SRAM を消す"),
        _F("nSWBOOT0", 26, 1, "0 = BOOT0 を nBOOT0 で決める"), _F("nBOOT0", 27, 1, "nSWBOOT0 = 0 のときの BOOT0"),
        _F("NRST_MODE", 28, 2, "PG10 の役割: 1 = リセット入力のみ、2 = GPIO、3 = 入出力"),
        _F("IRHEN", 30, 1, "1 = 内部のリセットを NRST に出す")), _SR_OPTR),
    "g0": Family("g0", "optr", 0x40022000, (
        _RDP, _F("BORR_EN", 8, 1, "1 = BOR を有効"), _F("BORF_LEV", 9, 2, "BOR の下がりの閾値"),
        _F("BORR_LEV", 11, 2, "BOR の上がりの閾値"), _F("nRST_STOP", 13, 1, "0 = STOP に入るとリセット"),
        _F("nRST_STDBY", 14, 1, "0 = STANDBY に入るとリセット"), _F("nRST_SHDW", 15, 1, "0 = SHUTDOWN に入るとリセット"),
        _F("IWDG_SW", 16, 1, "1 = IWDG はソフトウェアで開始"), _F("IWDG_STOP", 17, 1, "0 = STOP で IWDG を止める"),
        _F("IWDG_STDBY", 18, 1, "0 = STANDBY で IWDG を止める"), _F("WWDG_SW", 19, 1, "1 = WWDG はソフトウェアで開始"),
        _F("RAM_PARITY_CHECK", 22, 1, "0 = SRAM のパリティ検査"), _F("nBOOT_SEL", 24, 1, "0 = BOOT0 を PA14 で決める"),
        _F("nBOOT1", 25, 1, "ブートの選択"), _F("nBOOT0", 26, 1, "nBOOT_SEL = 1 のときの BOOT0"),
        _F("NRST_MODE", 27, 2, "PF2 の役割: 1 = リセット入力のみ、2 = GPIO、3 = 入出力"),
        _F("IRHEN", 29, 1, "1 = 内部のリセットを NRST に出す")), _SR_OPTR),
}
# F4 の nWRP のビット数（= セクタ数）。分からない品種は nWRP を扱わない（ビットは保つ）
_F4_SECTORS = {0x423: 6, 0x433: 8, 0x431: 8, 0x421: 8, 0x413: 12, 0x419: 12, 0x441: 12, 0x458: 5}


def family_for(part: Part | int) -> Family:
    """部品（coresight_id.Part か DEV_ID）のオプションバイトの系列。扱えない系列は OptionBytesError。"""
    dev_id = part.id if isinstance(part, Part) else part
    stm = STM32_PARTS.get(dev_id) if (not isinstance(part, Part) or part.vendor == "STMicroelectronics") else None
    if stm is None or stm.option_family is None:
        name = part.name if isinstance(part, Part) else f"DEV_ID 0x{dev_id:03X}"
        raise OptionBytesError(f"{name} のオプションバイトは扱えない（対応: STM32F4・L4・G4・G0）")
    if stm.option_family == "f4":
        return _f4(_F4_SECTORS.get(dev_id))
    return FAMILIES[stm.option_family]


def rdp_level(value: int) -> int:
    return 0 if value == RDP_LEVEL0 else 2 if value == RDP_LEVEL2 else 1


def decode(family: Family, raw: int) -> dict:
    fields = {f.name: f.get(raw) for f in family.fields}
    return {"family": family.name, "address": family.option_address, "raw": raw, "fields": fields,
            "rdp_level": rdp_level(fields["RDP"]),
            "descriptions": {f.name: f.summary for f in family.fields}}


def plan_write(family: Family, current: int, changes: dict[str, int], confirm_mass_erase: bool = False,
               confirm_protect: bool = False) -> tuple[int, list[str]]:
    """変更（名前 → 値）を今の値 current に重ねた新しい値と、注意の列。危ない変更は確認が無ければ ValueError。"""
    if not changes:
        raise ValueError("変えるフィールドが無い")
    new = current
    notes: list[str] = []
    for name, value in changes.items():
        f = family.field(name)
        if not isinstance(value, int) or not 0 <= value < 1 << f.width:
            raise ValueError(f"{f.name} は 0〜{(1 << f.width) - 1}")
        new = (new & ~f.mask) | (value << f.lsb)
    before, after = rdp_level(family.field("RDP").get(current)), rdp_level(family.field("RDP").get(new))
    if after == 2:
        raise ValueError("RDP Level 2（0xCC）は書かない: デバッグが永久に塞がり、戻せない")
    if before == 1 and after == 0:
        if not confirm_mass_erase:
            raise ValueError("RDP を Level 1 → 0 に戻すと Flash を全消去する（プログラムもデータも消える）: "
                             "よければ confirm_mass_erase=true で")
        notes.append("RDP を Level 0 に戻す: Flash を全消去する")
    if before == 0 and after == 1:
        if not confirm_protect:
            raise ValueError("RDP を Level 1 にするとデバッガから Flash を読めなくなる（戻すには全消去）: "
                             "よければ confirm_protect=true で")
        notes.append("RDP を Level 1 にする: 以後 Flash はデバッガから読めない（戻すと全消去）")
    return new, notes


def _wait(mem: Memory, family: Family, timeout_s: float, sleep: Callable[[float], None]) -> None:
    deadline = time.monotonic() + timeout_s
    while mem.read32(family.sr) & BSY:
        if time.monotonic() >= deadline:
            raise OptionBytesError(f"FLASH が {timeout_s} 秒たっても BSY のまま")
        sleep(0.01)


def _check_errors(mem: Memory, family: Family) -> None:
    sr = mem.read32(family.sr)
    if sr & family.sr_errors:
        mem.write32(family.sr, sr & family.sr_errors)
        raise OptionBytesError(f"FLASH の誤り（SR = 0x{sr:08X}）: 書き込み保護・順序の誤り・値の検査で拒まれた")


def program(mem: Memory, family: Family, new: int, timeout_s: float = 60.0, launch: bool = False,
            sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """オプションバイトを new にする。全消去を伴うと時間がかかる（timeout_s）。launch（optr の系列）は書いた後で
    OBL_LAUNCH（ターゲットがリセットされる。その書き込みの応答は返らないことがある）。行ったことの列を返す。"""
    log: list[str] = []
    _wait(mem, family, 1.0, sleep)
    mem.write32(family.sr, family.sr_errors)                           # 前の誤りの印を消す
    opt = family.option_address
    if family.style == "optcr":
        if mem.read32(opt) & 1:                                         # OPTLOCK
            mem.write32(family.optkeyr, OPTKEY1)
            mem.write32(family.optkeyr, OPTKEY2)
            if mem.read32(opt) & 1:
                raise OptionBytesError("OPTCR の鍵が開かない（鍵の順序の誤りで次のリセットまで塞がっている）")
            log.append("OPTCR を開いた")
        mem.write32(opt, new & ~0x3)
        mem.write32(opt, (new & ~0x3) | 0x2)                            # OPTSTRT
        log.append(f"OPTCR に 0x{new & ~0x3:08X} を書いて OPTSTRT")
        _wait(mem, family, timeout_s, sleep)
        _check_errors(mem, family)
        mem.write32(opt, mem.read32(opt) | 0x1)
        log.append("OPTCR を閉じた")
        return log
    cr = family.cr
    if mem.read32(cr) & (1 << 31):                                      # LOCK
        mem.write32(family.keyr, KEY1)
        mem.write32(family.keyr, KEY2)
    if mem.read32(cr) & (1 << 30):                                      # OPTLOCK
        mem.write32(family.optkeyr, OPTKEY1)
        mem.write32(family.optkeyr, OPTKEY2)
    if mem.read32(cr) & (3 << 30):
        raise OptionBytesError("FLASH_CR の鍵が開かない（鍵の順序の誤りで次のリセットまで塞がっている）")
    log.append("FLASH_CR と OPTLOCK を開いた")
    mem.write32(opt, new)
    mem.write32(cr, mem.read32(cr) | (1 << 17))                         # OPTSTRT
    log.append(f"OPTR に 0x{new:08X} を書いて OPTSTRT")
    _wait(mem, family, timeout_s, sleep)
    _check_errors(mem, family)
    if launch:
        log.append("OBL_LAUNCH（ターゲットはリセットされる）")
        try:
            mem.write32(cr, mem.read32(cr) | (1 << 27))
        except Exception:
            pass                                                        # リセットで応答が返らないことがある
    else:
        mem.write32(cr, mem.read32(cr) | (1 << 31))
        log.append("FLASH_CR を閉じた（反映は OBL_LAUNCH か次の電源投入）")
    return log


def protection_status(part: Part, read: Callable[[int], int]) -> dict | None:
    """接続の診断（fprobe.diagnose）の保護の段: STM32 の RDP（読めた事実の message と rdp_level・option_bytes）。
    扱えない部品は None。良し悪しの判定はしない。"""
    try:
        family = family_for(part)
    except OptionBytesError:
        return None
    raw = read(family.option_address)
    level = rdp_level(family.field("RDP").get(raw))
    out = {"rdp_level": level, "option_bytes": raw}
    if level == 0:
        return {"message": "STM32 の読み出し保護は Level 0（保護なし）", **out}
    if level == 1:
        return {"message": "STM32 の読み出し保護が Level 1: デバッガから Flash を読めない"
                           "（target_unlock で全消去して解除）", **out}
    return {"message": "STM32 の読み出し保護が Level 2: デバッグは永久に塞がっている", **out}
