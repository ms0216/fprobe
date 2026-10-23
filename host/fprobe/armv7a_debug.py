"""ARMv7-A（Cortex-A7 等）の外部デバッグへの道（U32）: JTAG-DP → APB-AP の向こうの CoreSight のデバッグレジスタ
（コアごとに 4 KB の窓。番地は ROM テーブルから見つける）。fprobe.arm_debug.CoreDebugPort の実装。

レジスタ（ARMv7 の外部デバッグ。窓の中の番地）:
  DBGDIDR 0x000、DBGDTRRX 0x080、DBGITR 0x084、DBGDSCR 0x088、DBGDTRTX 0x08C、DBGDRCR 0x090、
  DBGBVR / BCR 0x100 / 0x140 + 4n、DBGWVR / WCR 0x180 / 0x1C0 + 4n、DBGOSLAR 0x300、DBGOSLSR 0x304、
  DBGPRSR 0x314、DBGLAR 0xFB0、DEVTYPE 0xFCC（0x15 = 処理系のデバッグ）。

DCC は非ブロックのモード（DSCR.ExtDCCmode = 0）で使い、ITR に書く前と後に DSCR.InstrCompl_l を、DTRTX を読む前に
TXfull_l を見る（OpenOCD の cortex_a と同じ）。
"""

from .arm_debug import DSCR_INSTRCOMPL, DSCR_TXFULL, ArmDebugError
from .coresight_id import CLASS_CORESIGHT, decode_ap_idr, walk_rom_table
from .raw_dap import AP_BASE, AP_IDR, DapError, RawDap

__all__ = ["V7DebugPort", "find_apb_ap", "find_debug_bases"]

DIDR, DTRRX, ITR, DSCR, DTRTX, DRCR = 0x000, 0x080, 0x084, 0x088, 0x08C, 0x090
BVR, BCR, WVR, WCR = 0x100, 0x140, 0x180, 0x1C0
OSLAR, OSLSR, PRSR, LAR, DEVTYPE = 0x300, 0x304, 0x314, 0xFB0, 0xFCC
DRCR_HRQ, DRCR_RRQ, DRCR_CSE = 1 << 0, 1 << 1, 1 << 2
OSLSR_LOCKED, PRSR_POWERED = 1 << 1, 1 << 0
UNLOCK_KEY = 0xC5ACCE55
DEVTYPE_CORE_DEBUG = 0x15
_POLLS = 200


def find_apb_ap(dap: RawDap, max_aps: int = 16) -> int | None:
    """ADIv5 の AP を順に見て、最初の APB の MEM-AP（APB2/3・APB4）の番号。"""
    for ap in range(max_aps):
        try:
            idr = decode_ap_idr(dap.read_ap(ap, AP_IDR))
        except DapError:
            dap.clear_errors()
            continue
        if idr["kind"] == "mem-ap" and idr["type"] in (2, 6):
            return ap
    return None


def find_debug_bases(dap: RawDap, ap: int) -> list[int]:
    """APB-AP の ROM テーブルをたどり、コアのデバッグの窓（CoreSight の class 9、DEVTYPE 0x15）の番地を番地順に。
    ROM テーブルが無い・読めなければ空。"""
    base = dap.read_ap(ap, AP_BASE)
    if base in (0xFFFFFFFF, 0) or not base & 1:                    # 「ROM テーブルが無い」の印
        return []

    def read(address: int) -> int:
        try:
            return dap.mem_read32(ap, address)
        except DapError:
            dap.clear_errors()
            raise
    found = []
    for comp in walk_rom_table(read, base & 0xFFFFF000).flatten():
        if comp.error or comp.cls != CLASS_CORESIGHT:
            continue
        try:
            if read(comp.address + DEVTYPE) & 0xFF == DEVTYPE_CORE_DEBUG:
                found.append(comp.address)
        except DapError:
            continue
    return sorted(found)


class V7DebugPort:
    """1 つのコアのデバッグの窓（APB-AP の番地 base）。"""

    architecture = 7

    def __init__(self, dap: RawDap, ap: int, base: int):
        self.dap, self.ap, self.base = dap, ap, base

    def _read(self, offset: int) -> int:
        return self.dap.mem_read32(self.ap, self.base + offset)

    def _write(self, offset: int, value: int) -> None:
        self.dap.mem_write32(self.ap, self.base + offset, value & 0xFFFFFFFF)

    def unlock(self) -> dict:
        """ソフトウェアのロック（LAR）と OS ロック（OSLAR）を解き、電源の様子（PRSR。読むと sticky の印が消える）。"""
        self._write(LAR, UNLOCK_KEY)
        self._write(OSLAR, 0)
        if self._read(OSLSR) & OSLSR_LOCKED:
            raise ArmDebugError(f"0x{self.base:08X}: OS ロックが解けない（DBGOSLSR）")
        prsr = self._read(PRSR)
        if not prsr & PRSR_POWERED:
            raise ArmDebugError(f"0x{self.base:08X}: コアのデバッグの電源が落ちている（DBGPRSR 0x{prsr:08X}）")
        return {"prsr": prsr}

    # --- CoreDebugPort ---------------------------------------------------------------------------------

    def didr(self) -> int:
        return self._read(DIDR)

    def dscr(self) -> int:
        return self._read(DSCR)

    def write_dscr(self, value: int) -> None:
        self._write(DSCR, value)

    def halt_request(self) -> None:
        self._write(DRCR, DRCR_HRQ)

    def restart_request(self) -> None:
        self._write(DRCR, DRCR_RRQ | DRCR_CSE)

    def clear_sticky(self) -> None:
        self._write(DRCR, DRCR_CSE)

    def _wait(self, mask: int, what: str) -> int:
        for _ in range(_POLLS):
            dscr = self._read(DSCR)
            if dscr & mask:
                return dscr
        raise ArmDebugError(f"{what}（DSCR 0x{dscr:08X}）")

    def execute(self, instruction: int) -> int:
        self._wait(DSCR_INSTRCOMPL, "前の命令が終わらない")
        self._write(ITR, instruction)
        return self._wait(DSCR_INSTRCOMPL, f"命令 0x{instruction:08X} が終わらない")

    def read_dtrtx_fast(self, instruction: int, count: int) -> list[int]:
        """速いモード（DSCR.ExtDCCmode = 2）で: ITR に instruction を書き（この時点では実行されない。実機の Cortex-A7）、
        DTRTX を count 回まとめて読む（CMSIS-DAP のブロック転送。読むたびに今の値を返してから instruction を実行する。
        最初の値は前の値）。"""
        self._wait(DSCR_INSTRCOMPL, "前の命令が終わらない")
        self._write(ITR, instruction)
        return self.dap.mem_read32_repeat(self.ap, self.base + DTRTX, count)

    def to_core(self, value: int) -> None:
        self._write(DTRRX, value)

    def from_core(self) -> int:
        self._wait(DSCR_TXFULL | (1 << 26), "DTRTX に値が来ない")
        return self._read(DTRTX)

    def write_breakpoint(self, index: int, value: int, control: int) -> None:
        self._write(BCR + 4 * index, 0)                            # 無効にしてから番地を変える
        self._write(BVR + 4 * index, value)
        self._write(BCR + 4 * index, control)

    def write_watchpoint(self, index: int, value: int, control: int) -> None:
        self._write(WCR + 4 * index, 0)
        self._write(WVR + 4 * index, value)
        self._write(WCR + 4 * index, control)
