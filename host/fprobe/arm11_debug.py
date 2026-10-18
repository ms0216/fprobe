"""ARM11（ARMv6 の ARM1176JZF-S 等）の外部デバッグへの道（U32）: CoreSight ではなく、JTAG の TAP の命令と
スキャンチェーンで直接。fprobe.arm_debug.CoreDebugPort の実装。

TAP の命令（IR 5 ビット）: EXTEST 0x00、SCAN_N 0x02、RESTART 0x04、HALT 0x08、INTEST 0x0C、ITRSEL 0x1D、
IDCODE 0x1E、BYPASS 0x1F。SCAN_N の DR（5 ビット）で INTEST / EXTEST がつなぐチェーンを選ぶ:
  0 = DBGDIDR（32）、1 = DSCR（32。INTEST で読み、EXTEST で書く）、
  4 = ITR（ITRSEL でつながる。33 ビット: [31:0] 命令、[32] InstCompl）、
  5 = DTR（34 ビット: [31:0] 値、[32] Ready（INTEST では wDTR に値があった）、[33] nRetry。INTEST で wDTR を読み、
      EXTEST で rDTR に書く）、
  7 = BVR / BCR / WVR / WCR（40 ビット: [0] nR/W（取り出しは Ready）、[32:1] 値、[39:33] 番号。
      BVR 64 + n、BCR 80 + n、WVR 96 + n、WCR 112 + n）。

ITR の命令は、Update-DR の直後に Run-Test/Idle に入ったとき（IR が ITRSEL、または INTEST / EXTEST でチェーン 4 / 5）
に実行される。命令を実行させたくない DR のシフトは Pause-DR で止め、次の操作の頭で Exit2-DR → Update-DR →
Select-DR-Scan と抜ける（Run-Test/Idle を通らない）。IR のシフトは Run-Test/Idle で終える。OpenOCD の arm11 の
手順（arm11_dbgtap.c）と同じ前提で、実機では未確認。
"""

import time

from .arm_debug import ArmDebugError
from .jtag_chain import JtagWire, clock

__all__ = ["Arm11Port", "EXTEST", "HALT", "INTEST", "ITRSEL", "RESTART", "SCAN_N"]

EXTEST, SCAN_N, RESTART, HALT, INTEST, ITRSEL = 0x00, 0x02, 0x04, 0x08, 0x0C, 0x1D
IR_LENGTH = 5
SC7_BVR, SC7_BCR, SC7_WVR, SC7_WCR = 64, 80, 96, 112
_DSCR_STICKY = 0x1C0
_POLLS = 200


class Arm11Port:
    """チェーン（IR 長の列、TDO 側から）の device 番目の ARM11 の TAP。呼ぶ前は TAP が Run-Test/Idle にいること。"""

    architecture = 6

    def __init__(self, wire: JtagWire, chain: list[int], device: int):
        if chain[device] != IR_LENGTH:
            raise ValueError(f"ARM11 の IR は {IR_LENGTH} ビット（チェーンの {device} 番は {chain[device]}）")
        self.wire, self.chain, self.device = wire, list(chain), device
        self._state = "idle"                                     # idle（Run-Test/Idle）/ pause（Pause-DR）
        self._ir: int | None = None
        self._scan_chain: int | None = None

    # --- TAP -------------------------------------------------------------------------------------------

    def _head(self, ir: bool) -> list[int]:
        if self._state == "idle":
            return [1, 1, 0, 0] if ir else [1, 0, 0]
        return [1, 1, 1, 1, 0, 0] if ir else [1, 1, 1, 0, 0]     # Exit2-DR → Update-DR → Select-DR-Scan → …

    def _shift(self, ir: bool, value: int, length: int, end_idle: bool) -> int:
        head = self._head(ir)
        tail = [1, 0] if end_idle else [0]                      # Exit1 → Update → Run-Test/Idle / Exit1 → Pause
        tms = head + [0] * (length - 1) + [1] + tail
        tdi = [0] * len(head) + [(value >> k) & 1 for k in range(length)] + [0] * len(tail)
        tdo = clock(self.wire, tms, tdi)[len(head):len(head) + length]
        self._state = "idle" if end_idle else "pause"
        return sum(b << k for k, b in enumerate(tdo))

    def _set_ir(self, instruction: int, force: bool = False) -> None:
        if self._ir == instruction and not force:
            return
        offset, total = sum(self.chain[:self.device]), sum(self.chain)
        value = ((1 << total) - 1) & ~(((1 << IR_LENGTH) - 1) << offset) | instruction << offset   # ほかは BYPASS
        self._shift(True, value, total, end_idle=True)
        self._ir = instruction

    def _dr(self, value: int, length: int, end_idle: bool) -> int:
        """この TAP の DR（ほかの装置は BYPASS の 1 ビットずつ）。"""
        before, after = self.device, len(self.chain) - 1 - self.device
        out = self._shift(False, value << before | ((1 << after) - 1) << (before + length), before + length + after,
                          end_idle)
        return (out >> before) & ((1 << length) - 1)

    def _select(self, scan_chain: int) -> None:
        if self._scan_chain != scan_chain:
            self._set_ir(SCAN_N)
            self._dr(scan_chain, 5, end_idle=True)
            self._scan_chain = scan_chain

    def _read_chain(self, scan_chain: int) -> int:
        self._select(scan_chain)
        self._set_ir(INTEST)
        return self._dr(0, 32, end_idle=True)

    # --- CoreDebugPort ---------------------------------------------------------------------------------

    def didr(self) -> int:
        return self._read_chain(0)

    def dscr(self) -> int:
        return self._read_chain(1)

    def write_dscr(self, value: int) -> None:
        self._select(1)
        self._set_ir(EXTEST)
        self._dr(value & 0xFFFFFFFF, 32, end_idle=True)

    def clear_sticky(self) -> None:
        """sticky のアボート・未定義の印に 0 を書いて消す（ARMv6 の DSCR。実機で未確認）。"""
        self.write_dscr(self.dscr() & ~_DSCR_STICKY)

    def halt_request(self) -> None:
        self._set_ir(HALT, force=True)                          # Update-IR → Run-Test/Idle で効く

    def restart_request(self) -> None:
        self.clear_sticky()
        self._set_ir(RESTART, force=True)

    def execute(self, instruction: int) -> None:
        """ITR に入れて実行し（Run-Test/Idle を通す）、同じ命令を Pause-DR で止めるシフトで InstCompl を待つ。"""
        self._set_ir(ITRSEL)
        self._dr(instruction, 33, end_idle=True)
        for _ in range(_POLLS):
            if self._dr(instruction, 33, end_idle=False) >> 32 & 1:
                return None
        raise ArmDebugError(f"命令 0x{instruction:08X} が終わらない（ITR の InstCompl）")

    def to_core(self, value: int) -> None:
        """rDTR に置く（Pause-DR で止め、次の操作の頭の Update-DR で入る。命令は実行させない）。"""
        self._select(5)
        self._set_ir(EXTEST)
        self._dr(value & 0xFFFFFFFF, 34, end_idle=False)

    def from_core(self) -> int:
        self._select(5)
        self._set_ir(INTEST)
        for _ in range(_POLLS):
            out = self._dr(0, 34, end_idle=False)
            if out >> 32 & 1:
                return out & 0xFFFFFFFF
            time.sleep(0.0005)
        raise ArmDebugError("wDTR に値が来ない（チェーン 5 の Ready）")

    def _sc7_write(self, address: int, value: int) -> None:
        self._select(7)
        self._set_ir(EXTEST)
        for _ in range(_POLLS):
            if self._dr(1 | (value & 0xFFFFFFFF) << 1 | address << 33, 40, end_idle=True) & 1:
                return
        raise ArmDebugError(f"スキャンチェーン 7 の {address} に書けない（Ready にならない）")

    def write_breakpoint(self, index: int, value: int, control: int) -> None:
        self._sc7_write(SC7_BCR + index, 0)
        self._sc7_write(SC7_BVR + index, value)
        self._sc7_write(SC7_BCR + index, control)

    def write_watchpoint(self, index: int, value: int, control: int) -> None:
        self._sc7_write(SC7_WCR + index, 0)
        self._sc7_write(SC7_WVR + index, value)
        self._sc7_write(SC7_WCR + index, control)
