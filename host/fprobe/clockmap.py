"""2 つの時計の対応付け: 「すべての窓を通る直線の範囲」（docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.6・§13）。

入力は組の列。1 つの組は、同じ瞬間を 2 つの時計で見た値の窓: 対象の時計 x ∈ [x_lo, x_hi]、プローブの時計 y ∈ [y_lo, y_hi]
（どちらも両端を含む閉じた区間。半開きの範囲 [lo, hi) は閉じた区間に広げて渡してよい: 範囲が広がるだけで誤りにはならない）。
仮定は 1 つだけ: 区間の中で y = a + b·x（2 つの時計の比 b が一定）で、b > 0（どちらの時計も進む）。この仮定を結果に書く。
合う直線 (a, b) の集まりは凸の多角形で、任意の x に対する y の範囲（と逆向き）はその頂点の上の最小・最大で決まる。
最小二乗（対称な雑音の仮定が入る）は使わない（§6 の判断 4）。どの直線も全部の窓を通らなければ、それを事実として返す
（仮定が破れた。first_conflict = 時刻の順に足して初めて合わなくなった組の番号）。

計算は有理数（fractions.Fraction）で行う（窓の端の比べに丸めの誤差を入れない）。区間の切れ目は使う側が明示して分ける
（timesync.segments の gap_ticks と同じ。暗黙の既定は置かない）。比の範囲を使う側が知っていれば slope = (lo, hi) で渡す
（例: 申告の公称の比を 1 点で使う。その仮定は使う側が明示する）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction

__all__ = ["ASSUMPTION", "ClockMap", "Pair", "pair", "segments_by_gap"]

ASSUMPTION = "constant_ratio_within_segment"     # 結果に書く仮定の名前

Number = int | float | Fraction


@dataclass(frozen=True)
class Pair:
    """同じ瞬間の 2 つの時計の値の窓（両端を含む）。x = 対象の時計、y = プローブの時計（刻み）。"""
    x_lo: Fraction
    x_hi: Fraction
    y_lo: Fraction
    y_hi: Fraction


def _f(v: Number) -> Fraction:
    return v if isinstance(v, Fraction) else Fraction(v)


def pair(x: Number | tuple[Number, Number], y: Number | tuple[Number, Number]) -> Pair:
    """組を作る。x・y は 1 点か (lo, hi)。"""
    x_lo, x_hi = (x, x) if not isinstance(x, tuple) else x
    y_lo, y_hi = (y, y) if not isinstance(y, tuple) else y
    p = Pair(_f(x_lo), _f(x_hi), _f(y_lo), _f(y_hi))
    if p.x_lo > p.x_hi or p.y_lo > p.y_hi:
        raise ValueError(f"窓の下限が上限より大きい: {p}")
    return p


# 制約は c_a·a + c_b·b <= c（a = 切片、b = 傾き。直線 y = a + b·(x − x0)、x0 は最初の組の x_lo: 値を小さく保つ）

def _constraints(p: Pair, x0: Fraction) -> list[tuple[Fraction, Fraction, Fraction]]:
    lo, hi = p.x_lo - x0, p.x_hi - x0
    # ある x ∈ [lo, hi] で a + b·x ∈ [y_lo, y_hi]（b > 0）⇔ a + b·lo <= y_hi かつ a + b·hi >= y_lo
    return [(Fraction(1), lo, p.y_hi), (Fraction(-1), -hi, -p.y_lo)]


def _clip(poly: list[tuple[Fraction, Fraction]], ca: Fraction, cb: Fraction, c: Fraction) -> list[tuple[Fraction, Fraction]]:
    """凸の多角形（頂点の列。線分・点に縮んでいてもよい）を半平面 ca·a + cb·b <= c で切る（Sutherland–Hodgman）。"""
    if not poly:
        return []
    out: list[tuple[Fraction, Fraction]] = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        fp, fq = ca * p[0] + cb * p[1] - c, ca * q[0] + cb * q[1] - c
        if fp <= 0:
            out.append(p)
        if (fp < 0 < fq) or (fq < 0 < fp):
            t = fp / (fp - fq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    dedup: list[tuple[Fraction, Fraction]] = []
    for v in out:
        if not dedup or dedup[-1] != v:
            dedup.append(v)
    if len(dedup) > 1 and dedup[0] == dedup[-1]:
        dedup.pop()
    return dedup


class ClockMap:
    """組の列（1 つの区間）から、合う直線の範囲を持つ。

    - feasible: 全部の窓を通る直線があるか（False なら仮定が破れた。first_conflict に組の番号。傾きが決まらなければ None）。
    - slope_range(): 傾き b（プローブの刻み / 対象の 1 単位）の範囲。決まらなければ None（組の x が 1 種類で、slope も
      渡されていない等）。
    - to_probe(x): 対象の時計 x（1 点か窓）に対するプローブの時刻の範囲 {"t_lo", "t_hi", ...}。
    - to_target(y): 逆向き。
    """

    def __init__(self, pairs: Iterable[Pair], slope: tuple[Number, Number] | None = None):
        self.pairs: list[Pair] = list(pairs)
        if slope is not None and not (0 <= _f(slope[0]) <= _f(slope[1]) and _f(slope[1]) > 0):
            raise ValueError("slope は 0 <= lo <= hi、0 < hi（lo = 0 は前の区間の範囲の下限が 0 のとき等）")
        self.slope_given = None if slope is None else (_f(slope[0]), _f(slope[1]))
        self.first_conflict: int | None = None
        self.x0 = self.pairs[0].x_lo if self.pairs else Fraction(0)
        self._poly: list[tuple[Fraction, Fraction]] | None = None     # 合う直線 (a, b) の多角形（頂点の列）
        self.determined = False                     # 傾きが上下から押さえられた（範囲を出せる）
        self._build()

    # --- 作る -----------------------------------------------------------------------------------------------------
    def _build(self) -> None:
        """組を与えた順に足していく（first_conflict を「その順で初めて合わなくなった組」にするため、外側の箱も、それまでの
        組だけから作る）。"""
        if not self.pairs:
            return
        poly: list[tuple[Fraction, Fraction]] | None = None
        first_hi = last_lo = None                  # それまでの組で x_hi の最も小さいもの・x_lo の最も大きいもの
        for k, p in enumerate(self.pairs):
            if poly is None:
                first_hi = p if first_hi is None or p.x_hi < first_hi.x_hi else first_hi
                last_lo = p if last_lo is None or p.x_lo > last_lo.x_lo else last_lo
                b_hi = self._slope_bound(first_hi, last_lo) if self.slope_given is None else self.slope_given[1]
                if b_hi is None:
                    continue                       # まだ傾きが上から押さえられない
                poly = self._box(b_hi)
                todo = self.pairs[:k + 1]
                start = 0
            else:
                todo, start = [p], k
            for i, q in enumerate(todo, start):
                for ca, cb, c in _constraints(q, self.x0):
                    poly = _clip(poly, ca, cb, c)
                if not poly:
                    self.first_conflict = i
                    break
            if not poly:
                break
        self.determined = poly is not None or self.first_conflict is not None
        self._poly = poly

    def _box(self, b_hi: Fraction) -> list[tuple[Fraction, Fraction]]:
        """合う直線をすべて含む外側の箱（最初の組から導く）: b ∈ [b_lo, b_hi]、a ∈ [y_lo0 − b_hi·w0, y_hi0]
        （w0 = 最初の組の x の窓の幅。a は x0 = x_lo0 での y）。"""
        b_lo = Fraction(0) if self.slope_given is None else self.slope_given[0]
        p0 = self.pairs[0]
        w0 = p0.x_hi - self.x0
        a_lo, a_hi = p0.y_lo - b_hi * w0, p0.y_hi
        return [(a_lo, b_lo), (a_hi, b_lo), (a_hi, b_hi), (a_lo, b_hi)]

    @staticmethod
    def _slope_bound(i: Pair, j: Pair) -> Fraction | None:
        """合う直線の傾きの確かな上限: 組 i・j で x_hi_i < x_lo_j なら、直線は x_hi_i で y_lo_i 以上、x_lo_j で y_hi_j 以下
        なので b <= (y_hi_j − y_lo_i) / (x_lo_j − x_hi_i)（箱が合う直線を含めばよく、最も狭い上限である必要はない）。
        x の窓が重なれば None。"""
        if not i.x_hi < j.x_lo:
            return None
        return max((j.y_hi - i.y_lo) / (j.x_lo - i.x_hi), Fraction(0))

    # --- 問い合わせ -------------------------------------------------------------------------------------------------
    @property
    def feasible(self) -> bool | None:
        """全部の窓を通る直線があるか。傾きが決まらない（determined = False）ときは None（調べていない）。"""
        if not self.pairs or not self.determined:
            return None
        return self.first_conflict is None

    def _vertices(self) -> list[tuple[Fraction, Fraction]] | None:
        if not self.feasible or not self._poly:
            return None
        return self._poly

    def slope_range(self) -> tuple[Fraction, Fraction] | None:
        v = self._vertices()
        if v is None:
            return None
        bs = [b for _, b in v]
        return min(bs), max(bs)

    def to_probe(self, x: Number | tuple[Number, Number]) -> dict:
        """対象の時計の値 x（1 点か (lo, hi)）→ プローブの時刻の範囲 [t_lo, t_hi]（両端を含む）。決まらなければ None。"""
        x_lo, x_hi = (x, x) if not isinstance(x, tuple) else x
        x_lo, x_hi = _f(x_lo) - self.x0, _f(x_hi) - self.x0
        v = self._vertices()
        if v is None:
            return self._unknown()
        lo = min(a + b * x_lo for a, b in v)                 # b >= 0 なので x の窓の下端で最小
        hi = max(a + b * x_hi for a, b in v)
        return {"t_lo": lo, "t_hi": hi, "assumption": ASSUMPTION, "inside": self._inside(x_lo + self.x0, x_hi + self.x0),
                "unknown": []}

    def to_target(self, y: Number | tuple[Number, Number]) -> dict:
        """プローブの時刻 y（1 点か (lo, hi)）→ 対象の時計の範囲（両端を含む）。傾きが 0 を含めば上下が決まらない（None）。"""
        y_lo, y_hi = (y, y) if not isinstance(y, tuple) else y
        y_lo, y_hi = _f(y_lo), _f(y_hi)
        v = self._vertices()
        if v is None:
            return {"x_lo": None, "x_hi": None, "assumption": ASSUMPTION, "unknown": self._unknown()["unknown"]}
        if any(b <= 0 for _, b in v):
            return {"x_lo": None, "x_hi": None, "assumption": ASSUMPTION, "unknown": ["slope_lower_bound"]}
        # x = (y − a) / b は多角形の上で準線形: 最小・最大は頂点にある
        lo = min((y_lo - a) / b for a, b in v) + self.x0
        hi = max((y_hi - a) / b for a, b in v) + self.x0
        return {"x_lo": lo, "x_hi": hi, "assumption": ASSUMPTION, "unknown": []}

    def _inside(self, x_lo: Fraction, x_hi: Fraction) -> bool:
        """x が組の範囲（最初の組の x_lo〜最後の組の x_hi）の中か（外は延ばした直線の範囲: 仮定がその外でも成り立つとき）。"""
        first = min(p.x_lo for p in self.pairs)
        last = max(p.x_hi for p in self.pairs)
        return first <= x_lo and x_hi <= last

    def _unknown(self) -> dict:
        reason = ("no_pairs" if not self.pairs else "slope_undetermined" if not self.determined
                  else "assumption_broken")
        return {"t_lo": None, "t_hi": None, "assumption": ASSUMPTION, "inside": None, "unknown": [reason]}

    def summary(self) -> dict:
        """傾き・切片（最初の組の x_lo での y）の範囲と、仮定・破れた所（JSON に出せる float で）。"""
        sr = self.slope_range()
        v = self._vertices()
        return {"pairs": len(self.pairs), "feasible": self.feasible, "assumption": ASSUMPTION,
                "first_conflict": self.first_conflict,
                "slope": None if sr is None else [float(sr[0]), float(sr[1])],
                "x0": float(self.x0),
                "intercept": None if v is None else [float(min(a for a, _ in v)), float(max(a for a, _ in v))],
                "slope_given": None if self.slope_given is None else [float(s) for s in self.slope_given]}


def segments_by_gap(pairs: Sequence[Pair], gap: Number | None) -> list[list[Pair]]:
    """x の順の組を、間が gap（使う側が明示。None なら分けない）を超える所で区間に分ける。"""
    out: list[list[Pair]] = []
    for p in sorted(pairs, key=lambda p: p.x_lo):
        if out and (gap is None or p.x_lo - out[-1][-1].x_hi <= _f(gap)):
            out[-1].append(p)
        else:
            out.append([p])
    return out
