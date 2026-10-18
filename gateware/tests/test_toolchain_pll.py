import pytest

from fprobe_gw.toolchain.pll import PLL_CONSTRAINTS, PllSolveError, solve_pll

GW2A = PLL_CONSTRAINTS["GW2A-18C"]


def test_reproduces_known_good_sipeed_setting():
    # Sipeed 公式例: 27 MHz → 120 MHz は IDIV_SEL=8, FBDIV_SEL=39, ODIV_SEL=8
    s = solve_pll(27.0, 120.0, GW2A)
    assert s.fout_mhz == pytest.approx(120.0)
    assert (s.idiv_sel, s.fbdiv_sel, s.odiv_sel) == (8, 39, 8)


def test_100mhz_within_tolerance_and_constraints():
    s = solve_pll(27.0, 100.0, GW2A)
    assert abs(s.fout_mhz - 100.0) / 100.0 <= 0.005
    assert GW2A.pfd_mhz[0] <= s.pfd_mhz <= GW2A.pfd_mhz[1]
    assert GW2A.vco_mhz[0] <= s.vco_mhz <= GW2A.vco_mhz[1]


def test_infeasible_request_names_the_reason():
    with pytest.raises(PllSolveError, match="27.0 MHz"):
        solve_pll(27.0, 2000.0, GW2A)
