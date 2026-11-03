from fprobe import generated as g
from fprobe.timesync import DEFAULT_LINK, ParentTime, SyncLink, fit_parent, link_from_timing, segments, to_parent

# 子の回路: 線が周期 [T, T + 1) の中で 1 になる → 子の時刻 T + RX（sync_link_tb。時刻の約束は gateware/fprobe_gw/timing.py の
# 冒頭）。試験は辺を子の刻みの頭の直後に置く（2026-11-02 の S1b までは RX + 位相の幅で、ホストの換算が 1 刻み遅かった）
RX, TX = g.SYNC_RX_LATENCY_TICKS, g.SYNC_TX_LATENCY_TICKS


def pairs_for(ratio: float, offset: int, n: int = 10, step: int = 100_000) -> list[tuple[int, int]]:
    """親の時刻 p に送った組を、子が時刻 local = (p + TX) * ratio + offset で受けた（+ RX の遅れで記録）とする。"""
    out = []
    for k in range(n):
        parent = 1_000_000 + k * step
        out.append((round((parent + TX) * ratio + offset) + RX, parent))
    return out


def test_fit_recovers_offset_and_ratio():
    pairs = pairs_for(1.00002, 12345)
    fit = fit_parent(pairs)
    assert isinstance(fit, ParentTime) and fit.pairs == 10 and fit.feasible
    lo, hi = fit.slope
    assert lo <= 1 / 1.00002 <= hi and hi - lo < 3e-6          # 窓の幅 1 刻み × 2 / 組の範囲 90 万刻み
    for parent in (1_000_000, 1_450_000, 1_900_000):
        local = round(parent * 1.00002 + 12345)                    # その親の時刻に子で起きた出来事
        got = to_parent(fit, local, [p[0] for p in pairs])
        truth = (local - 12345) / 1.00002
        assert got["t_lo"] - 1e-6 <= truth <= got["t_hi"] + 1e-6 and got["t_hi"] - got["t_lo"] <= 2


def test_single_pair_has_no_slope():
    assert fit_parent(pairs_for(1.0, 0, n=1)) is None
    assert fit_parent([]) is None


def test_uncertainty_is_facts():
    pairs = pairs_for(1.0, 500)
    fit = fit_parent(pairs)
    got = to_parent(fit, pairs[3][0] + 7, [p[0] for p in pairs])
    assert set(got) == {"t", "t_lo", "t_hi", "phase_span_ticks", "link", "nearest_pair_ticks", "inside", "assumption",
                        "unknown", "cable_delay"}
    assert got["phase_span_ticks"] == 1 and got["cable_delay"] is None and got["link"] == "generated"
    assert got["nearest_pair_ticks"] == 7 and got["inside"]
    assert got["assumption"] == "constant_ratio_within_segment"
    assert got["t_lo"] <= got["t"] <= got["t_hi"]


def test_a_broken_ratio_is_reported_not_fitted():
    # 比が区間の途中で変わる: どの直線も全部の窓を通らない（最小二乗のように黙って合わせない）
    pairs = pairs_for(1.0, 0, n=5) + [(p[0] + 10_000_000 + 50 * k, p[1] + 10_000_000) for k, p in
                                      enumerate(pairs_for(1.0, 0, n=5))]
    fit = fit_parent(pairs)
    assert fit.feasible is False
    got = to_parent(fit, pairs[2][0], [p[0] for p in pairs])
    assert got["t_lo"] is None and got["unknown"] == ["assumption_broken"]


def test_the_link_comes_from_the_recorded_declaration():
    child = {"sync_rx": {"delay": 2, "span": 1}}
    parent = {"sync_tx": {"delay": -2}}                           # PYNQ-Z2: 線の 1 段 + 端子の IOB の 1 段（R9）
    assert link_from_timing(child, parent) == SyncLink(2, 1, 2, "TIMING")
    assert link_from_timing(None, parent) == DEFAULT_LINK


def test_gap_in_pairs_is_reported():
    pairs = pairs_for(1.0, 0, n=4) + [(p[0] + 10_000_000, p[1] + 10_000_000) for p in pairs_for(1.0, 0, n=4)]
    assert len(segments(pairs, gap_ticks=None)) == 1
    two = segments(pairs, gap_ticks=1_000_000)
    assert [len(s) for s in two] == [4, 4]


def test_errors_are_counted_not_used():
    # 換算は組だけから決まる（CRC の合わない組は FPGA が捨て、数だけ CSR に残る。ここには入らない）
    import inspect
    assert list(inspect.signature(fit_parent).parameters) == ["pairs", "link", "cable_delay_ticks"]


def test_neighbouring_pairs_follow_a_drifting_rate():
    # 2 枚の時計の速さの比が記録の間に変わる（温度等）と、全体に 1 本の直線は全部の窓を通らない（仮定が破れたと返す）。
    # 両側の 2 組の窓を通る直線の範囲なら、仮定は隣り合う組の間だけで、本当の時刻を範囲に含む
    from fprobe.timesync import neighbour_converter
    pairs, local, parent = [], 0.0, 0
    for k in range(200):
        rate = 1.0 + 3e-7 * k                                    # 比がゆっくり変わる
        pairs.append((round(local) + RX, parent - TX))
        local += 100_000 * rate
        parent += 100_000
    to = neighbour_converter(pairs)
    fit = fit_parent(pairs)
    for i in (10, 100, 190):                                     # 組と組の真ん中の出来事
        (l0, p0), (l1, p1) = pairs[i], pairs[i + 1]
        mid_local = (l0 + l1) // 2 - RX
        truth = ((p0 + TX) + (p1 + TX)) / 2
        got = to(mid_local)
        assert got["t_lo"] - 1 <= truth <= got["t_hi"] + 1 and got["t_hi"] - got["t_lo"] <= 2
        assert got["assumption"] == "constant_ratio_between_neighbouring_pairs"
    assert fit.feasible is False                                 # 1 本の直線では合わない（この試験の前提）
