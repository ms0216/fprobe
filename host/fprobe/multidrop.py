"""複数ドロップの SWD（DPv2 の TARGETSEL）でコアごとに DP を持つ部品のマルチコア（U31）。

RP2040 は 1 本の SWD の線に DP が 3 つ並ぶ（core0・core1・救済の DP）。コアごとに DP と AHB-AP が別で、pyOCD は
1 つの接続で 1 つのコアの DP しか使わない（ターゲット名 rp2040_core0 / rp2040_core1）。ここでは:

- 部品の表（PARTS）: コアの番号 → TARGETSEL の値・pyOCD のターゲット名。target_connect の core の解決に使う。
- もう一方のコアを覗く・止める・再開する（CoreProbe）: pyOCD が接続中の接続口の上で、線のリセット → TARGETSEL で
  別の DP を選び、その AHB-AP で DHCSR 等を読み書きし、最後に元の DP を選び直す（DPIDR を読む）。
  DP のレジスタ（SELECT・CTRL/STAT）は DP ごとに保たれるので、pyOCD が覚えている元の DP の SELECT は食い違わない。
  救済の DP（RP2040 は 0xF1002927）は選ばない（選ぶとチップがリセットされる）。

機器固有の値は表に置き、手順は DPv2 の複数ドロップと ARMv6-M / v7-M の DHCSR の汎用の手順だけを使う。
"""

import time
from dataclasses import dataclass

from .raw_dap import RawDap

__all__ = ["CoreProbe", "DropCore", "MultidropPart", "PARTS", "core_state", "part_of", "resolve_target"]

DHCSR, DCRSR, DCRDR = 0xE000EDF0, 0xE000EDF4, 0xE000EDF8
DBGKEY = 0xA05F << 16
C_DEBUGEN, C_HALT, C_MASKINTS = 1 << 0, 1 << 1, 1 << 3
S_REGRDY, S_HALT, S_SLEEP, S_LOCKUP, S_RESET_ST = 1 << 16, 1 << 17, 1 << 18, 1 << 19, 1 << 25
REG_PC = 15                                             # DCRSR の REGSEL（DebugReturnAddress = PC）
AHB_AP = 0                                              # 各コアの DP の AP 0（RP2040）


@dataclass(frozen=True)
class DropCore:
    index: int
    targetsel: int
    pyocd_target: str                                   # このコアの DP で接続する pyOCD のターゲット名


@dataclass(frozen=True)
class MultidropPart:
    name: str
    cores: tuple[DropCore, ...]
    default_core: int = 0

    def core(self, index: int) -> DropCore:
        for core in self.cores:
            if core.index == index:
                return core
        raise ValueError(f"{self.name} のコアは {', '.join(str(c.index) for c in self.cores)}（{index} は無い）")


PARTS = (
    MultidropPart("rp2040", (DropCore(0, 0x01002927, "rp2040_core0"), DropCore(1, 0x11002927, "rp2040_core1"))),
)


def part_of(target: str | None) -> tuple[MultidropPart, int] | None:
    """pyOCD のターゲット名から (部品, コアの番号)。部品の名前そのもの（"rp2040"）は既定のコア。該当しなければ None。"""
    name = (target or "").strip().lower()
    for part in PARTS:
        if name == part.name:
            return part, part.default_core
        for core in part.cores:
            if name == core.pyocd_target:
                return part, core.index
    return None


def resolve_target(target: str, core: int | None) -> tuple[str, MultidropPart | None, int | None]:
    """target_connect の target と core から (pyOCD のターゲット名, 部品, コア)。core はコアごとに DP を持つ部品だけ。
    core が無ければ target の示すコア（"rp2040" は core0）。"""
    found = part_of(target)
    if found is None:
        if core is not None:
            names = ", ".join(p.name for p in PARTS)
            raise ValueError(f"core を選べるのはコアごとに DP を持つ部品（{names}）と RISC-V のハート"
                             f"（rp2350_riscv）、Cortex-A のコア（bcm2836 / cortex_a）だけ: target={target!r}")
        return target, None, None
    part, index = found
    if core is not None:
        if target.strip().lower() != part.name and core != index:
            raise ValueError(f"target={target!r} はコア {index} を指す: core={core} と食い違う")
        index = core
    return part.core(index).pyocd_target, part, index


def core_state(dhcsr: int) -> str:
    """DHCSR の状態のビットを pyOCD の Target.State の名前に（RESET / LOCKUP / HALTED / SLEEPING / RUNNING）。"""
    if dhcsr & S_RESET_ST:
        return "RESET"
    if dhcsr & S_LOCKUP:
        return "LOCKUP"
    if dhcsr & S_HALT:
        return "HALTED"
    if dhcsr & S_SLEEP:
        return "SLEEPING"
    return "RUNNING"


class CoreProbe:
    """pyOCD が接続中の接続口（DAPAccessCMSISDAP）の上で、接続していないコアの DP を一時的に選んで使う。

    使い方は visit(index, fn): 別の DP を選んで電源を確かめ、fn(self) を呼び、元の DP（home）を選び直す。fn の中では
    read32 / write32（その DP の AHB-AP）と state / halt / resume が使える。"""

    def __init__(self, link, part: MultidropPart, home: int, frequency_hz: int | None = None):
        self.part = part
        self.home = home
        self.dap = RawDap.attached(link, "swd", frequency_hz)

    def visit(self, index: int, fn):
        if index == self.home:
            raise ValueError("接続中のコアは pyOCD の経路で使う（visit は別のコアだけ）")
        core = self.part.core(index)
        try:
            self.dap.select_drop(core.targetsel)
            ctrl = self.dap.read_dp("CTRL/STAT")
            if not ctrl & (1 << 29):                       # CDBGPWRUPACK が無ければデバッグ電源を求める
                self.dap.clear_errors()
                if not self.dap.power_up()["debug_power"]:
                    raise RuntimeError(f"コア {index} の DP のデバッグ電源が上がらない")
            return fn(self)
        finally:
            self.dap.select_drop(self.part.core(self.home).targetsel)

    # --- 選んだ DP の上で（visit の fn の中） -------------------------------------------------------

    def read32(self, address: int) -> int:
        return self.dap.mem_read32(AHB_AP, address)

    def write32(self, address: int, value: int) -> None:
        self.dap.mem_write32(AHB_AP, address, value)

    def state(self) -> dict:
        """状態（DHCSR）と、止まっていれば PC（DCRSR / DCRDR。止まっているコアにだけ書く）。"""
        dhcsr = self.read32(DHCSR)
        out = {"state": core_state(dhcsr), "dhcsr": dhcsr}
        if out["state"] == "HALTED":
            out["pc"] = self._read_register(REG_PC)
        return out

    def halt(self) -> dict:
        dhcsr = self.read32(DHCSR)
        self.write32(DHCSR, DBGKEY | (dhcsr & C_MASKINTS) | C_DEBUGEN | C_HALT)
        self._wait(lambda v: v & S_HALT, "止まらない")
        return self.state()

    def resume(self) -> dict:
        dhcsr = self.read32(DHCSR)
        if dhcsr & S_HALT:
            self.write32(DHCSR, DBGKEY | C_DEBUGEN)          # C_HALT と C_MASKINTS を外す（デバッグは有効のまま）
            self._wait(lambda v: not v & S_HALT, "再開しない")
        return self.state()

    def _read_register(self, regsel: int) -> int:
        self.write32(DCRSR, regsel)
        self._wait(lambda v: v & S_REGRDY, "レジスタが読めない（S_REGRDY が立たない）")
        return self.read32(DCRDR)

    def _wait(self, ready, what: str, timeout_s: float = 0.1) -> int:
        deadline = time.monotonic() + timeout_s
        while True:
            value = self.read32(DHCSR)
            if ready(value):
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError(f"コアが{what}")
