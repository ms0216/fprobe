"""PLL / MMCM の分周比（ボードの入力クロック → sys のクロック）。ベンダーの道具を呼ばない純粋な計算。

Gowin rPLL の分周比ソルバ:

    fout = fin * FBDIV / IDIV,  fvco = fout * ODIV,  fpfd = fin / IDIV

IDIV / FBDIV はここでは実際の分周比（1..64）で扱い、rPLL パラメータ
（IDIV_SEL / FBDIV_SEL は「分周比 - 1」）は *_sel プロパティで得る。
Vivado 7 系の MMCM は固定の分周（MmcmSolution）で持つ。
"""

from dataclasses import dataclass

__all__ = ["PLL_CONSTRAINTS", "MmcmSolution", "PllConstraints", "PllSolution", "PllSolveError", "solve_pll"]


@dataclass(frozen=True)
class PllConstraints:
    pfd_mhz: tuple[float, float]
    vco_mhz: tuple[float, float]
    out_mhz: tuple[float, float]
    idiv_max: int = 64
    fbdiv_max: int = 64
    odiv_values: tuple[int, ...] = (2, 4, 8, 16, 32, 48, 64, 80, 96, 112, 128)


# 出典: Gowin UG286（Clock User Guide）/ DS102（GW2A データシート）
PLL_CONSTRAINTS: dict[str, PllConstraints] = {
    "GW2A-18C": PllConstraints(pfd_mhz=(3.0, 400.0), vco_mhz=(500.0, 1250.0),
                               out_mhz=(3.90625, 625.0)),
    # GW1NR-9（C6/I5）: Gowin DS117。apicula（apycula/gowin_pll.py、Tang Nano 9K で試験済み）と LiteX も同じ値
    "GW1NR-9C": PllConstraints(pfd_mhz=(3.0, 400.0), vco_mhz=(400.0, 1200.0), out_mhz=(3.125, 600.0)),
}


class PllSolveError(ValueError):
    """制約内で要求周波数を実現できない。"""


@dataclass(frozen=True)
class PllSolution:
    fin_mhz: float
    idiv: int
    fbdiv: int
    odiv: int

    @property
    def pfd_mhz(self) -> float:
        return self.fin_mhz / self.idiv

    @property
    def fout_mhz(self) -> float:
        return self.fin_mhz * self.fbdiv / self.idiv

    @property
    def vco_mhz(self) -> float:
        return self.fout_mhz * self.odiv

    @property
    def fout_hz(self) -> int:
        return round(self.fout_mhz * 1e6)

    # rPLL のパラメータ値
    @property
    def idiv_sel(self) -> int:
        return self.idiv - 1

    @property
    def fbdiv_sel(self) -> int:
        return self.fbdiv - 1

    @property
    def odiv_sel(self) -> int:
        return self.odiv


def solve_pll(fin_mhz: float, fout_mhz: float, constraints: PllConstraints, *,
              tolerance: float = 0.005) -> PllSolution:
    """誤差最小の解を返す。誤差が同じなら PFD が高い（ジッタの少ない）解を選ぶ。"""
    def within(value, bounds):
        return bounds[0] <= value <= bounds[1]

    best: tuple[tuple[float, float], PllSolution] | None = None
    for idiv in range(1, constraints.idiv_max + 1):
        if not within(fin_mhz / idiv, constraints.pfd_mhz):
            continue
        for fbdiv in range(1, constraints.fbdiv_max + 1):
            for odiv in constraints.odiv_values:
                s = PllSolution(fin_mhz, idiv, fbdiv, odiv)
                if not (within(s.fout_mhz, constraints.out_mhz)
                        and within(s.vco_mhz, constraints.vco_mhz)):
                    continue
                key = (abs(s.fout_mhz - fout_mhz), -s.pfd_mhz)
                if best is None or key < best[0]:
                    best = (key, s)
    if best is None or best[0][0] / fout_mhz > tolerance:
        found = f"（最良でも {best[1].fout_mhz:.4f} MHz）" if best else ""
        raise PllSolveError(
            f"{fin_mhz} MHz から {fout_mhz} MHz を誤差 {tolerance:.2%} 以内で作れない{found}")
    return best[1]


@dataclass(frozen=True)
class MmcmSolution:
    """7 系の MMCME2 の分周: fvco = fin * mult / divclk、fout = fvco / out_div（UG472。VCO は 600〜1200 MHz、-1 品）。

    2 本目の出力（CLKOUT1。取り込みのクロック cap_clk）は構成ごとに sys の整数倍 ratio を選ぶ: 分周 = out_div / ratio
    （同じ VCO から割るので sys と立ち上がりがそろう。cap_div）。BUFG の上限（DS187 -1: 464 MHz）を超える比は作れない。"""
    fin_mhz: float
    mult: float
    divclk: int
    out_div: float

    BUFG_MAX_MHZ = 464.0            # DS187 v1.21（-1）の BUFG の上限（docs/superpowers/specs/2026-10-29-performance-limits-study.md §0.1）

    def __post_init__(self):
        if not 600.0 <= self.vco_mhz <= 1200.0:
            raise ValueError(f"MMCM の VCO {self.vco_mhz} MHz が範囲外（600〜1200）")

    @property
    def vco_mhz(self) -> float:
        return self.fin_mhz * self.mult / self.divclk

    @property
    def fout_hz(self) -> int:
        return round(self.vco_mhz / self.out_div * 1e6)

    def cap_div(self, ratio: int) -> int:
        """cap_clk = sys × ratio を作る CLKOUT1 の分周（整数）。作れない比は ValueError。"""
        if ratio < 2 or self.out_div % ratio:
            raise ValueError(f"cap_clk は sys の 2 以上の整数倍（sys の分周 {self.out_div} が比 {ratio} で割り切れない）")
        div = round(self.out_div) // ratio
        if self.vco_mhz / div > self.BUFG_MAX_MHZ:
            raise ValueError(f"cap_clk {self.vco_mhz / div} MHz が BUFG の上限 {self.BUFG_MAX_MHZ} MHz を超える")
        return div

    def cap_hz(self, ratio: int) -> int:
        return round(self.vco_mhz / self.cap_div(ratio) * 1e6)
