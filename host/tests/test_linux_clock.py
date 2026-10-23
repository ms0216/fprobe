import pytest

from fprobe.linux.clock import TargetClock


def _clock():
    c = TargetClock()
    c.add(2_000_000_000, 12.5, 1e-4)
    c.add(0, 10.0, 1e-4)
    c.add(1_000_000_000, 11.0, 2e-4)
    return c


def test_pairs_kept_sorted():
    assert [p[0] for p in _clock().pairs] == [0, 1_000_000_000, 2_000_000_000]


def test_interpolates_between_neighbours():
    r = _clock().to_host(1_500_000_000)
    assert r["host_s"] == pytest.approx(11.75)
    assert r["half_rtt_s"] == 2e-4 and r["nearest_pair_ns"] == 500_000_000 and r["outside"] is False


def test_to_host_outside_and_too_few():
    one = TargetClock()
    one.add(0, 1.0, 1e-4)
    assert one.to_host(0) is None
    r = _clock().to_host(3_000_000_000)
    assert r["host_s"] == pytest.approx(14.0) and r["outside"] is True and r["nearest_pair_ns"] == 1_000_000_000
