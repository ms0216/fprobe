"""縮図の事前計算（U23）: 出来事の件数を、時刻の段（ピラミッド）ごとの区間に数えておき、任意の範囲の件数を
区間の和で正確に出す（巨大な記録でも画素ごとの要約が速い。Tracealyzer の Trace Preview の相当）。

- 段 0 の区間の幅 w0 はタイムベースの 2 の冪（およそ 1 ms）。段が 1 つ上がるごとに FAN（16）倍。区間の番号は
  絶対時刻 // 幅（記録の分割をまたいでも区間がそろう）。
- 範囲 [t0, t1) は `decompose` で「丸ごとの区間（段, 番号の範囲）」と「最も細かい区間より短い端（生で数える）」に
  分ける。各段の端は FAN 個未満なので、問い合わせは段の数に比例する（件数には比例しない）。

    grid = SummaryGrid.for_timebase(100e6)
    table = bin_counts(db, grid)                 # {段: [(stream, 番号, 件数)]}（記録の分割を閉じるときに 1 回）
    grid.count(t0, t1, bins_count, raw_count, streams)   # {stream: 件数}
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["FAN", "LEVELS", "SummaryGrid", "bin_counts", "decompose"]

FAN_BITS = 4
FAN = 1 << FAN_BITS                # 段が 1 つ上がるときの幅の倍率
LEVELS = 10                        # 段 0（約 1 ms）〜段 9（約 2 年）
TARGET_W0_S = 1e-3                 # 段 0 の幅の目安


def decompose(t0: int, t1: int, w0: int, levels: int = LEVELS) -> tuple[list[tuple[int, int]], list[tuple[int, int, int]]]:
    """[t0, t1) を (生で数える端 [(a, b)], 丸ごとの区間 [(段, 番号の始め, 番号の終わり)]) に分ける（重なり・隙間なし）。"""
    if t1 <= t0:
        return [], []
    a, b = -(-t0 // w0), t1 // w0
    if a >= b:
        return [(t0, t1)], []
    raw = []
    if t0 < a * w0:
        raw.append((t0, a * w0))
    if b * w0 < t1:
        raw.append((b * w0, t1))
    bins = []
    for level in range(levels):
        up_a, up_b = -(-a // FAN), b // FAN
        if level == levels - 1 or up_a >= up_b:
            bins.append((level, a, b))
            break
        if a < up_a * FAN:
            bins.append((level, a, up_a * FAN))
        if up_b * FAN < b:
            bins.append((level, up_b * FAN, b))
        a, b = up_a, up_b
    return raw, bins


Counts = dict[str, int]


@dataclass(frozen=True)
class SummaryGrid:
    """段の幅の決め（記録の meta.json の summary に保存し、読み手も同じものを使う）。"""
    w0: int
    levels: int = LEVELS

    @classmethod
    def for_timebase(cls, timebase_hz: float) -> SummaryGrid:
        return cls(1 << max(0, round(math.log2(max(1.0, timebase_hz * TARGET_W0_S)))))

    @classmethod
    def from_dict(cls, d: dict) -> SummaryGrid:
        return cls(int(d["w0"]), int(d.get("levels", LEVELS)))

    def to_dict(self) -> dict:
        return {"w0": self.w0, "fan": FAN, "levels": self.levels}

    def width(self, level: int) -> int:
        return self.w0 << (FAN_BITS * level)

    def level_for(self, ticks: int) -> int:
        """幅が ticks 以下の最も粗い段（ticks が段 0 より短ければ 0）。"""
        level = 0
        while level + 1 < self.levels and self.width(level + 1) <= ticks:
            level += 1
        return level

    def count(self, t0: int, t1: int, bins_count: Callable[[int, int, int, list[str] | None], Counts],
              raw_count: Callable[[int, int, list[str] | None], Counts], streams: list[str] | None) -> Counts:
        """[t0, t1) の stream ごとの件数。bins_count(段, 番号の始め, 終わり, streams)・raw_count(a, b, streams) が数える。"""
        raw, bins = decompose(t0, t1, self.w0, self.levels)
        out: Counts = {}
        for part in [raw_count(a, b, streams) for a, b in raw] + [bins_count(lv, lo, hi, streams) for lv, lo, hi in bins]:
            for stream, n in part.items():
                out[stream] = out.get(stream, 0) + n
        return out


def bin_counts(db: sqlite3.Connection, grid: SummaryGrid, levels: range | None = None) -> dict[int, list[tuple[str, int, int]]]:
    """表 events の stream ごと・区間ごとの件数を、段 0 から上の段へ足し上げて作る。{段: [(stream, 番号, 件数)]}。"""
    base: dict[tuple[str, int], int] = {}
    for stream, b, n in db.execute("SELECT stream, t / ?, COUNT(*) FROM events GROUP BY stream, t / ?",
                                   (grid.w0, grid.w0)):
        base[(stream, b)] = n
    out: dict[int, list[tuple[str, int, int]]] = {}
    wanted = range(grid.levels) if levels is None else levels
    current = base
    for level in range(grid.levels):
        if level in wanted:
            out[level] = [(s, b, n) for (s, b), n in sorted(current.items())]
        upper: dict[tuple[str, int], int] = {}
        for (s, b), n in current.items():
            key = (s, b >> FAN_BITS)
            upper[key] = upper.get(key, 0) + n
        current = upper
    return out
