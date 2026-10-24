"""CoreSight の命令トレースの部品の設定: ETMv4・ATB ファンネル（CSTF）・TPIU（標準のプログラマーズモデル）。

チップに依らない手順だけを置く（番地は呼ぶ側が渡す。Cortex-M の既定は ETM 0xE0041000・TPIU 0xE0040000）。
RP2350 のトレース FIFO・DMA のようなチップ固有の手順はプラグイン（fprobe.plugins.rp2350_trace）。

ETM の設定は M-profile の命令トレースの最小: 分岐の全出力（BB）・リターンスタック（RS）は使わない（再構成が
間接分岐の行き先を Address で受け取る前提）。ViewInst は「常に真・開始済み」で全命令を出す。
出典: Arm IHI 0064（ETMv4 の TRCPRGCTLR・TRCSTATR・TRCCONFIGR・TRCVICTLR・TRCSYNCPR・TRCIDRn）、
CoreSight Architecture Specification（LAR・LSR、ファンネルの Ctrl、TPIU の CSPSR・FFCR・FFSR・FSCR）、
Armv8-M（DEMCR.TRCENA）。
"""

import time
from dataclasses import asdict, dataclass

from ..memview import RegisterAccess
from .etmv4 import Etmv4Config

__all__ = ["DEMCR", "ETM_BASE", "TPIU_BASE", "Etmv4", "Etmv4Info", "Etmv4Setup", "Funnel", "Tpiu",
           "enable_trace"]

ETM_BASE = 0xE004_1000
TPIU_BASE = 0xE004_0000
DEMCR = 0xE000_EDFC
TRCENA = 1 << 24
LAR, LSR = 0xFB0, 0xFB4
UNLOCK = 0xC5AC_CE55

# ETMv4 のレジスタ（ETM の先頭からのオフセット）
TRCPRGCTLR, TRCSTATR, TRCCONFIGR = 0x004, 0x00C, 0x010
TRCEVENTCTL0R, TRCEVENTCTL1R, TRCSTALLCTLR, TRCTSCTLR = 0x020, 0x024, 0x02C, 0x030
TRCSYNCPR, TRCCCCTLR, TRCBBCTLR, TRCTRACEIDR = 0x034, 0x038, 0x03C, 0x040
TRCVICTLR, TRCVIIECTLR, TRCVISSCTLR = 0x080, 0x084, 0x088
TRCIDR0, TRCIDR1, TRCIDR2 = 0x1E0, 0x1E4, 0x1E8
_CONFIG_CCI, _CONFIG_TS = 1 << 4, 1 << 11
_ETM_OFFSETS = {"TRCCONFIGR": TRCCONFIGR, "TRCEVENTCTL0R": TRCEVENTCTL0R, "TRCEVENTCTL1R": TRCEVENTCTL1R,
                "TRCSTALLCTLR": TRCSTALLCTLR, "TRCTSCTLR": TRCTSCTLR, "TRCSYNCPR": TRCSYNCPR, "TRCCCCTLR": TRCCCCTLR,
                "TRCBBCTLR": TRCBBCTLR, "TRCTRACEIDR": TRCTRACEIDR, "TRCVICTLR": TRCVICTLR,
                "TRCVIIECTLR": TRCVIIECTLR, "TRCVISSCTLR": TRCVISSCTLR}
_VICTLR_ALWAYS_STARTED = 0x01 | 1 << 9          # EVENT = 資源 1（常に真）、SSSTATUS = 1（開始済み）

# TPIU のレジスタ
CSPSR, SPPR, FFSR, FFCR, FSCR = 0x004, 0x0F0, 0x300, 0x304, 0x308
_TPIU_OFFSETS = {"CSPSR": CSPSR, "SPPR": SPPR, "FFCR": FFCR, "FSCR": FSCR}
_FFCR_ENFCONT, _FFCR_FONMAN, _FFCR_TRIGIN = 1 << 1, 1 << 6, 1 << 8

_POLL_S = 0.2


def _wait(predicate, timeout_s: float = _POLL_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.001)


def unlock(regs: RegisterAccess, base: int) -> None:
    """ソフトのロック（LSR.SLI = 実装あり、SLK = 掛かっている）があれば LAR で外す。無い部品（v8-M の ETM 等）は触らない。"""
    lsr = regs.read(base + LSR, 32)
    if lsr & 0b01 and lsr & 0b10:
        regs.write(base + LAR, 32, UNLOCK)


def enable_trace(regs: RegisterAccess) -> None:
    """DEMCR.TRCENA（DWT・ITM・ETM・TPIU を使えるようにする）。"""
    demcr = regs.read(DEMCR, 32)
    if not demcr & TRCENA:
        regs.write(DEMCR, 32, demcr | TRCENA)


@dataclass(frozen=True)
class Etmv4Info:
    """TRCIDR0〜2 から分かる ETM の構成。"""
    arch_major: int
    arch_minor: int
    commit_opt1: bool
    cycle_counting: bool
    branch_broadcast: bool
    return_stack: bool
    timestamp_bits: int
    vmid_bytes: int
    cid_bytes: int

    @property
    def version(self) -> str:
        return f"{self.arch_major}.{self.arch_minor}"

    def decoder_config(self) -> Etmv4Config:
        # ID レジスタの言う大きさのまま（0 = 無い。別の値で置き換えない）
        return Etmv4Config(commit_opt1=self.commit_opt1, vmid_bytes=self.vmid_bytes, cid_bytes=self.cid_bytes)

    def as_dict(self) -> dict:
        return {"version": self.version, **asdict(self)}


@dataclass(frozen=True)
class Etmv4Setup:
    trace_id: int = 0x10
    cycle_counting: bool = False
    cycle_threshold: int = 256
    timestamps: bool = False
    sync_period: int = 1024                     # A-sync・Trace Info を出す間隔（バイト、2 のべき 256〜1 MiB）

    def __post_init__(self):
        if not 0x01 <= self.trace_id <= 0x6F:
            raise ValueError(f"トレース ID は 0x01〜0x6F: 0x{self.trace_id:X}")
        if self.sync_period & (self.sync_period - 1) or not 256 <= self.sync_period <= 1 << 20:
            raise ValueError(f"sync_period は 256〜1048576 の 2 のべき: {self.sync_period}")
        if not 1 <= self.cycle_threshold <= 0xFFF:
            raise ValueError(f"cycle_threshold は 1〜4095: {self.cycle_threshold}")


class Etmv4:
    def __init__(self, regs: RegisterAccess, base: int = ETM_BASE):
        self.regs, self.base = regs, base

    def _r(self, offset: int) -> int:
        return self.regs.read(self.base + offset, 32)

    def _w(self, offset: int, value: int) -> None:
        self.regs.write(self.base + offset, 32, value)

    def info(self) -> Etmv4Info:
        idr0, idr1, idr2 = self._r(TRCIDR0), self._r(TRCIDR1), self._r(TRCIDR2)
        vmid = {0: 0, 1: 1, 2: 2, 4: 4}.get(idr2 >> 10 & 0x1F, 0)
        cid = 4 if idr2 >> 5 & 0x1F == 4 else 0
        ts = {0: 0, 6: 48, 8: 64}.get(idr0 >> 24 & 0x1F, 0)
        return Etmv4Info(arch_major=idr1 >> 8 & 0xF, arch_minor=idr1 >> 4 & 0xF, commit_opt1=bool(idr0 >> 29 & 1),
                         cycle_counting=bool(idr0 >> 7 & 1), branch_broadcast=bool(idr0 >> 5 & 1),
                         return_stack=bool(idr0 >> 9 & 1), timestamp_bits=ts, vmid_bytes=vmid, cid_bytes=cid)

    def idle(self) -> bool:
        return bool(self._r(TRCSTATR) & 1)

    def disable(self) -> bool:
        """止めて、IDLE（トレースを出し切った）を待つ。待ち切れなければ False。"""
        self._w(TRCPRGCTLR, 0)
        return _wait(self.idle)

    def enable(self, setup: Etmv4Setup) -> dict:
        """止めてから設定を書き、有効にする。書いた値を返す。"""
        unlock(self.regs, self.base)
        if not self.disable():
            raise RuntimeError(f"ETM（0x{self.base:08X}）が止まらない（TRCSTATR.IDLE が立たない）")
        config = (_CONFIG_CCI if setup.cycle_counting else 0) | (_CONFIG_TS if setup.timestamps else 0)
        written = {
            "TRCCONFIGR": config, "TRCEVENTCTL0R": 0, "TRCEVENTCTL1R": 0, "TRCSTALLCTLR": 0, "TRCTSCTLR": 0,
            "TRCSYNCPR": setup.sync_period.bit_length() - 1,
            "TRCCCCTLR": setup.cycle_threshold if setup.cycle_counting else 0, "TRCBBCTLR": 0,
            "TRCTRACEIDR": setup.trace_id, "TRCVICTLR": _VICTLR_ALWAYS_STARTED, "TRCVIIECTLR": 0, "TRCVISSCTLR": 0,
        }
        for name, value in written.items():
            self._w(_ETM_OFFSETS[name], value)
        self._w(TRCPRGCTLR, 1)
        if not _wait(lambda: not self.idle()):
            raise RuntimeError(f"ETM（0x{self.base:08X}）が動き出さない（TRCSTATR.IDLE が落ちない）")
        return written


class Funnel:
    """ATB ファンネル（CSTF）: Ctrl の下位 8 ビットが入口ごとの有効、ビット 11:8 が最小の保持時間。"""

    def __init__(self, regs: RegisterAccess, base: int):
        self.regs, self.base = regs, base

    def enable(self, ports: int) -> int:
        if not 0 <= ports <= 0xFF:
            raise ValueError(f"ファンネルの入口は 8 ビットのマスク: 0x{ports:X}")
        unlock(self.regs, self.base)
        hold = self.regs.read(self.base, 32) & 0xF00          # 最小の保持時間はいまの値のまま（0 も有効な値）
        value = hold | ports
        self.regs.write(self.base, 32, value)
        return value


class Tpiu:
    def __init__(self, regs: RegisterAccess, base: int = TPIU_BASE):
        self.regs, self.base = regs, base

    def configure(self, port_size: int, formatter: bool, fsync_frames: int) -> dict:
        """パラレル（同期）の端子、port_size ビット、フォーマッタ（継続モード）か素通し。"""
        if not 1 <= port_size <= 32:
            raise ValueError(f"port_size は 1〜32 ビット: {port_size}")
        unlock(self.regs, self.base)
        written = {"CSPSR": 1 << (port_size - 1), "SPPR": 0,
                   "FFCR": _FFCR_TRIGIN | (_FFCR_ENFCONT if formatter else 0), "FSCR": fsync_frames}
        for name, value in written.items():
            self.regs.write(self.base + _TPIU_OFFSETS[name], 32, value)
        return written

    def flush(self) -> bool:
        """フォーマッタの中身を出し切る（FFCR.FOnMan）。済めば True。"""
        ffcr = self.regs.read(self.base + FFCR, 32)
        self.regs.write(self.base + FFCR, 32, ffcr | _FFCR_FONMAN)
        return _wait(lambda: not self.regs.read(self.base + FFCR, 32) & _FFCR_FONMAN)
