"""ターゲットの時計（CLOCK_MONOTONIC_RAW の ns）→ PC の単調時計（秒）。

組（ターゲットの時刻、PC の往復の中点、往復の半分）の、隣り合う 2 つの間を直線で内挿する（時計の比のゆれに追う。
timesync.interpolator と同じ考え）。範囲の外は端の 2 つの直線で延ばし、そのことを outside で示す。"""
from __future__ import annotations

import bisect


class TargetClock:
    def __init__(self) -> None:
        self.pairs: list[tuple[int, float, float]] = []

    def add(self, target_ns: int, host_s: float, half_rtt_s: float) -> None:
        bisect.insort(self.pairs, (target_ns, host_s, half_rtt_s))

    def to_host(self, target_ns: int) -> dict | None:
        """{host_s, half_rtt_s（使った 2 つの組の大きい方）, nearest_pair_ns, outside}。組が 2 つ未満なら None。"""
        p = self.pairs
        if len(p) < 2:
            return None
        i = bisect.bisect_right(p, (target_ns, float("inf"), 0.0))
        outside = i == 0 or i == len(p)
        k = min(max(i, 1), len(p) - 1)                    # 挟む（外なら端の）2 つ: p[k-1], p[k]
        (n0, h0, r0), (n1, h1, r1) = p[k - 1], p[k]
        host = h0 + (target_ns - n0) * (h1 - h0) / (n1 - n0) if n1 != n0 else h0
        near = min(abs(target_ns - n) for n, _, _ in (p[k - 1], p[k]))
        return {"host_s": host, "half_rtt_s": max(r0, r1), "nearest_pair_ns": near, "outside": outside}
