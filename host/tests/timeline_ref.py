"""共通タイムラインのタイルの間引きの参照の実装（試験用。画面の TS 版 host/ui/src/lib/timeline/decimate.ts と同じ定義）。

タイルの生データから、`la.Waveform.view`（捕捉）・`Recording.waveform_view`（記録）・`Recording.density`（出来事）と
同じ結果を作れることを、Python の側で確かめるのに使う。速さは考えない。
"""

from bisect import bisect_left, bisect_right


def wave_states(tile: dict, data: dict, v0: int, v1: int, width: int) -> list[str]:
    """生の波形のタイルを [v0, v1) の width 画素に間引く: チャンネルごとの '0' / '1' / 'x' / '-'。"""
    wave = tile["wave"]
    span = v1 - v0
    rows = {ch: ["-"] * width for ch in wave["channels"]}
    for i, piece in enumerate(wave["pieces"]):
        t0, tps, length = piece["t0"], piece["tps"], piece["length"]
        t1 = piece.get("t1", t0 + round(length * tps))
        present = piece.get("channels", wave["channels"])
        for px in range(width):
            p0, p1 = v0 + px * span // width, v0 - (-(px + 1) * span // width)
            if t1 <= p0 or t0 >= p1:
                continue
            s0 = max(0, int((p0 - t0) / tps))
            s1 = min(length, max(s0 + 1, int(-(-(p1 - t0) // tps))))
            a, b = max(0, s0), min(length, s1)
            b = max(a + 1, b)
            for k, ch in enumerate(present):
                edges = data[f"wave.{i}.{ch}"]
                before = bisect_right(edges, a)
                level = "x" if bisect_left(edges, b) > before else str(piece["levels"][k] ^ (before & 1))
                have = rows[ch][px]
                rows[ch][px] = level if have in ("-", level) else "x"
    return ["".join(rows[ch]) for ch in wave["channels"]]


def lane_counts(lane: dict, data: dict, index: int, v0: int, v1: int, width: int) -> list[int]:
    """生の出来事の段を画素ごとの件数に（t が画素 [p0, p1) に入るもの。Recording.density と同じ境）。"""
    t = data[f"lane.{index}.t"]
    span = v1 - v0
    out = []
    for px in range(width):
        p0, p1 = v0 + px * span // width, v0 + (px + 1) * span // width
        out.append(bisect_left(t, p1) - bisect_left(t, p0))
    return out


def row_density(times: list[int], v0: int, v1: int, width: int) -> list[int]:
    """1 つの段の出来事の時刻（昇順）の、画素ごとの件数（Recording.density と同じ境）。"""
    span = v1 - v0
    return [bisect_left(times, v0 + (px + 1) * span // width) - bisect_left(times, v0 + px * span // width)
            for px in range(width)]


def value_envelope(samples: list[tuple[int, float]], v0: int, v1: int, width: int, t0: int, t1: int,
                   hold: bool = True) -> list[list[float] | None]:
    """値の段の画素ごとの [最小, 最大]: 画素 [p0, p1)（p1 は波形と同じ切り上げ）のサンプルと、hold なら p0 の前の最後の
    サンプル（保たれている値）。タイル [t0, t1) の外にはみ出す画素・値の無い画素は None（画面の TS 版と同じ定義）。"""
    times = [t for t, _ in samples]
    span, out = v1 - v0, []
    for px in range(width):
        p0, p1 = v0 + px * span // width, v0 - (-(px + 1) * span // width)
        if p0 < t0 or p1 > t1:
            out.append(None)
            continue
        a, b = bisect_left(times, p0), bisect_left(times, p1)
        values = [v for _, v in samples[(a - 1 if hold and a > 0 else a):b]]
        out.append([min(values), max(values)] if values else None)
    return out
