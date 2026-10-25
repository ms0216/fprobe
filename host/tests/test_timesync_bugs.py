"""不具合の再現試験（修正済み。台帳: docs/reports/bugs/correlate-recording.md）。領域: timesync / ClockMap。"""

import pytest

from fprobe.timesync import fit_parent, interpolator


def test_fit_parent_with_identical_local_times_is_none():
    assert fit_parent([(5, 10), (5, 20)]) is None


def test_interpolator_with_duplicate_pair_at_the_end_does_not_divide_by_zero():
    to = interpolator([(0, 0), (10, 10), (10, 10)])
    assert to(15) == pytest.approx(interpolator([(0, 0), (10, 10)])(15))    # 重複を除いた 2 点の直線と同じ
