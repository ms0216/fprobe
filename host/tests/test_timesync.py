from fprobe import generated as g
from fprobe.timesync import ParentTime, fit_parent, segments, to_parent

RX, TX = g.SYNC_RX_LATENCY_TICKS + g.SYNC_RX_PHASE_TICKS, g.SYNC_TX_LATENCY_TICKS


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
    assert isinstance(fit, ParentTime) and fit.pairs == 10
    assert abs(fit.ratio - 1 / 1.00002) < 1e-9
    for parent in (1_000_000, 1_450_000, 1_900_000):
        local = round(parent * 1.00002 + 12345)                    # その親の時刻に子で起きた出来事
        assert abs(to_parent(fit, local, [p[0] for p in pairs])["t"] - parent) <= 1


def test_single_pair_has_no_slope():
    assert fit_parent(pairs_for(1.0, 0, n=1)) is None
    assert fit_parent([]) is None


def test_uncertainty_is_facts():
    pairs = pairs_for(1.0, 500)
    fit = fit_parent(pairs)
    got = to_parent(fit, pairs[3][0] + 7, [p[0] for p in pairs])
    assert set(got) == {"t", "quantization_ticks", "residual_max_ticks", "nearest_pair_ticks", "cable_delay"}
    assert got["quantization_ticks"] == 1 and got["cable_delay"] is None
    assert got["nearest_pair_ticks"] == 7
    assert got["residual_max_ticks"] == fit.residual_max < 1


def test_gap_in_pairs_is_reported():
    pairs = pairs_for(1.0, 0, n=4) + [(p[0] + 10_000_000, p[1] + 10_000_000) for p in pairs_for(1.0, 0, n=4)]
    assert len(segments(pairs, gap_ticks=None)) == 1
    two = segments(pairs, gap_ticks=1_000_000)
    assert [len(s) for s in two] == [4, 4]


def test_errors_are_counted_not_used():
    # 換算は組だけから決まる（CRC の合わない組は FPGA が捨て、数だけ CSR に残る。ここには入らない）
    import inspect
    assert list(inspect.signature(fit_parent).parameters) == ["pairs"]


def test_interpolation_between_neighbouring_pairs_follows_a_drifting_rate():
    # 2 枚の時計の速さの比が記録の間に変わる（温度等）と、全体に 1 本の直線では場所によって数刻みずれる。
    # 両側の組の間の直線（内挿）なら、組の分解能（1 刻み）で合う
    from fprobe.timesync import interpolator
    pairs, local, parent = [], 0.0, 0
    for k in range(200):
        rate = 1.0 + 3e-7 * k                                    # 比がゆっくり変わる
        pairs.append((round(local) + RX, parent - TX))
        local += 100_000 * rate
        parent += 100_000
    to = interpolator(pairs)
    fit = fit_parent(pairs)
    for i in (10, 100, 190):                                     # 組と組の真ん中の出来事
        (l0, p0), (l1, p1) = pairs[i], pairs[i + 1]
        mid_local = (l0 + l1) // 2 - RX
        truth = ((p0 + TX) + (p1 + TX)) / 2
        assert abs(to(mid_local) - truth) <= 1
    assert fit.residual_max > 3                                  # 1 本の直線では合わない（この試験の前提）
