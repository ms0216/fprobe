"""サイクルの計算（fprobe.profile.cycles）: CYCCNT の折り返し・CPU クロックの推定・イベントカウンタの差・命令数・区間の統計。"""

import pytest

from fprobe.profile import cycles


def test_unwrap_counter_follows_32_bit_wraps():
    raw = [0xFFFF_FF00, 0x0000_0100, 0x0000_0200, 0x8000_0000, 0x0000_0010]
    assert cycles.unwrap(raw) == [0xFFFF_FF00, 0x1_0000_0100, 0x1_0000_0200, 0x1_8000_0000, 0x2_0000_0010]
    assert cycles.unwrap([250, 3], bits=8) == [250, 259]


def test_estimate_clock_from_sampled_cyccnt():
    hz = 100_000_000                                     # タイムベース
    clock = 84_000_000
    samples = [(t, (t * clock // hz + 12345) & 0xFFFFFFFF) for t in range(0, hz * 2, hz // 1000)]
    est = cycles.estimate_clock(samples, hz)
    assert est.clock_hz == pytest.approx(clock, rel=1e-7)
    assert est.samples == len(samples) and est.span_s == pytest.approx(2, rel=1e-3)
    assert est.max_residual_cycles < 2 and est.residual_rms_cycles < 2 and not est.warnings
    d = est.as_dict()
    assert d["clock_mhz"] == pytest.approx(84, rel=1e-6)
    # 標準誤差は残差が独立・等分散という仮定の上の推定: 本体は出さない（plugins.heuristics.clock_stderr）
    assert "stderr_hz" not in d and "ppm" not in d and d["clock_min_hz"] is None and d["clock_max_hz"] is None


def test_estimate_clock_warns_when_the_counter_stops():
    hz = 100_000_000
    samples = [(t * 100_000, 1000 + (t if t < 5 else 5) * 16_000) for t in range(10)]
    est = cycles.estimate_clock(samples, hz)
    assert any("止まった" in w for w in est.warnings)
    with pytest.raises(ValueError, match="3 点"):
        cycles.estimate_clock(samples[:2], hz)


def test_two_point_clock_with_bounds():
    hz = 100_000_000
    est = cycles.clock_from_two_reads((1_000_000, 1_000_200, 5_000), (101_000_000, 101_000_200, 16_005_000), hz)
    assert est.clock_hz == pytest.approx(16e6)
    # 読みの前後の時刻で挟んだ範囲（事実）: 進み 16e6 を、最も長い間（b1 − a0）と最も短い間（b0 − a1）で割る
    assert est.clock_min_hz == pytest.approx(16e6 / 1.000002) and est.clock_max_hz == pytest.approx(16e6 / 0.999998)
    assert est.max_residual_cycles is None and est.residual_rms_cycles is None


def test_counter_delta_and_instructions():
    assert cycles.counter_delta(250, 4, overflows=1) == (10, True)
    assert cycles.counter_delta(250, 4, overflows=2) == (10 + 256, True)    # 溢れは 250→4 の 1 回を含む
    # 溢れを数えていない・数えた溢れと値が合わない（パケットが欠けた）: 1 周未満とみなした値と、数えていない印
    assert cycles.counter_delta(250, 4) == (10, False)
    assert cycles.counter_delta(250, 4, overflows=0) == (10, False)
    counts = {"cycles": 1000, "cpi": 100, "exc": 20, "sleep": 30, "lsu": 50, "fold": 10}
    assert cycles.instructions(counts) == 1000 - 100 - 20 - 30 - 50 + 10


def test_interval_summary():
    s = cycles.interval_summary([100, 120, 110], clock_hz=1e6)
    assert s["count"] == 3 and s["min"] == 100 and s["max"] == 120 and s["mean"] == 110
    assert s["time_us"]["mean"] == pytest.approx(110)
    assert cycles.interval_summary([5])["stdev"] == 0 and "time_us" not in cycles.interval_summary([5])
