"""変数のライブ観測の統計: XY の組・カーソル A/B の測定・サンプラの損失率。純粋な計算（機器にも API にも依らない）。

fprobe.probe_watch が再公開する。
"""

from .dwarf.values import scalar


def pair_latest(xs: list, ys: list, max_dt: float | None = None) -> tuple[list[tuple], int, int]:
    """XY の組: xs の各 (時刻, 値) に、その時刻以前で最新の ys の (時刻, 値) を組む（2 つは同時に測っていないので、
    両方の時刻を残す）。返り値 = ([(x の時刻, x, y の時刻, y)], それ以前に y が無い x の数, 時刻の差が max_dt を
    超えて外した数)。max_dt は使う側が渡すときだけ（無ければ外さない）。どちらも古い順。"""
    pairs, unpaired, excluded = [], 0, 0
    j = -1
    for tx, vx in xs:
        while j + 1 < len(ys) and ys[j + 1][0] <= tx:
            j += 1
        if j < 0:
            unpaired += 1
        elif max_dt is not None and tx - ys[j][0] > max_dt:
            excluded += 1
        else:
            pairs.append((tx, vx, ys[j][0], ys[j][1]))
    return pairs, unpaired, excluded


def xy_result(x: str, y: str, pairs: list[tuple], unpaired: int, excluded: int, max_dt_us: float | None,
              us: float, from_us: dict) -> dict:
    """watch_xy の応答（時刻は µs。us = 1 カウントの µs）。"""
    return {"x": x, "y": y,
            "points": [{"x_time_us": tx * us, "x": vx, "y_time_us": ty * us, "y": vy} for tx, vx, ty, vy in pairs],
            "dt_max_us": max(((tx - ty) * us for tx, _, ty, _ in pairs), default=None),
            "unpaired": unpaired, "excluded": excluded, "max_dt_us": max_dt_us, "from_us": from_us}


def _number(value: dict) -> int | float | None:
    """値の数（SCALAR_KINDS の value。真偽は 0・1）。数でない種類・浮動小数の NaN・無限大は None。"""
    v = scalar(value)
    return v if isinstance(v, (int, float)) else None


class ValueStats:
    """1 つの変数の、watch_set から読んだ値の統計（数・最小・最大・平均）。数でない値（構造体・配列等）は数えず
    not_numeric に。int・float・bool・enum（SCALAR_KINDS）の数を数える（真偽は 0・1、NaN・無限大は数えない）。"""

    def __init__(self):
        self.count = self.not_numeric = 0
        self.min = self.max = None
        self._sum = 0.0

    def add(self, value: object) -> None:
        value = _number(value)
        if value is None:
            self.not_numeric += 1
            return
        self.count += 1
        self._sum += value
        self.min = value if self.min is None or value < self.min else self.min
        self.max = value if self.max is None or value > self.max else self.max

    def as_dict(self) -> dict:
        return {"count": self.count, "min": self.min, "max": self.max,
                "mean": self._sum / self.count if self.count else None, "not_numeric": self.not_numeric}


def measure(series: dict[str, list[tuple[float, object]]], a_us: float, b_us: float) -> dict:
    """カーソル A・B の測定（series = 変数ごとの (時刻 µs, 値) の古い順）。a・b = その時刻以前で最後の値と、その値の
    時刻（測った時刻とは別。無ければ None）、delta = b − a（どちらも数のとき）、stats = A〜B（両端を含む）の値の
    ValueStats、first_us = 持っている最初の値の時刻（それより前は持っていない）、covers_range_start = 持っている最初の値が範囲の始まり
    （A・B の早い方）以前か（偽なら、範囲の始まりからしばらくのデータが無い）。"""
    lo, hi = min(a_us, b_us), max(a_us, b_us)

    def at(points: list, t: float) -> dict | None:
        before = [p for p in points if p[0] <= t]
        return {"time_us": before[-1][0], "value": before[-1][1]} if before else None

    rows = []
    for name, points in series.items():
        a, b = at(points, a_us), at(points, b_us)
        st = ValueStats()
        for t, v in points:
            if lo <= t <= hi:
                st.add(v)
        na, nb = (None if x is None else _number(x["value"]) for x in (a, b))
        rows.append({"name": name, "a": a, "b": b, "delta": nb - na if na is not None and nb is not None else None,
                     "stats": st.as_dict(), "first_us": points[0][0] if points else None,
                     "covers_range_start": bool(points) and points[0][0] <= lo})
    return {"a_us": a_us, "b_us": b_us, "dt_us": b_us - a_us, "rows": rows}


def expression_facts(expressions: list[str] | None, watches: list[dict], rejected: list[dict]) -> dict | None:
    """渡された式（expressions）と観測の組（watches の name）の関係: observed = 観測している式、missing = 観測しておらず
    rejected（watch_set が誤りとして外した式）でもない式、others = 観測しているが渡した式に無い名前、all_observed = 式が
    1 つ以上あり全部観測している。式を渡さなければ None。画面が集合の差を持たずに済むよう、API で返す。"""
    if expressions is None:
        return None
    running = [w["name"] for w in watches]
    bad = {r["expression"] for r in rejected}
    observed = [e for e in expressions if e in running]
    return {"observed": observed,
            "missing": [e for e in expressions if e not in running and e not in bad],
            "others": [n for n in running if n not in expressions],
            "all_observed": bool(expressions) and len(observed) == len(expressions)}


def rates(status: dict) -> dict:
    """watch_status の数から: loss_ratio = 飛ばした周期 ÷ (巡回 + 飛ばした周期)、measured_hz = 巡回 ÷ 始めてからの秒。
    数が無い・0 なら None。"""
    rounds, overruns, since = status.get("rounds"), status.get("overruns"), status.get("since_start_s")
    total = (rounds or 0) + (overruns or 0)
    return {"loss_ratio": (overruns or 0) / total if rounds is not None and total else None,
            "measured_hz": rounds / since if rounds is not None and since else None}
