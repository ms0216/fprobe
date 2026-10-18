"""Gowin rPLL の分周比ソルバ。

    fout = fin * FBDIV / IDIV,  fvco = fout * ODIV,  fpfd = fin / IDIV

IDIV / FBDIV はここでは実際の分周比（1..64）で扱い、rPLL パラメータ
（IDIV_SEL / FBDIV_SEL は「分周比 - 1」）は *_sel プロパティで得る。
"""

from dataclasses import dataclass

__all__ = ["PLL_CONSTRAINTS", "PllConstraints", "PllSolution", "PllSolveError", "solve_pll"]


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
