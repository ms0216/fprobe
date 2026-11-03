"""不具合の再現試験（修正済み。台帳: docs/reports/bugs/correlate-recording.md）。領域: timesync / ClockMap。"""

from fprobe.timesync import fit_parent, neighbour_converter


def test_fit_parent_with_identical_local_times_is_none():
    assert fit_parent([(5, 10), (5, 20)]) is None


def test_neighbour_converter_with_duplicate_pair_at_the_end_does_not_divide_by_zero():
    # 段階 S3: 内挿（interpolator）は両側の 2 組の窓を通る直線の範囲（neighbour_converter）に置き換えた
    to = neighbour_converter([(0, 0), (10, 10), (10, 10)])
    want = neighbour_converter([(0, 0), (10, 10)])(15)                     # 重複を除いた 2 組と同じ
    got = to(15)
    assert (got["t_lo"], got["t_hi"]) == (want["t_lo"], want["t_hi"]) and got["t_lo"] is not None
