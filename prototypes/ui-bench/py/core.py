"""共通の間引き（numpy 版。web/core.js・egui/src/core.rs と同じ定義）と、numpy による画素の塗り（ラスタ）。"""

import json
import time
from pathlib import Path

import numpy as np

LABEL_W, W, AXIS_H, ROW_H, LANE_H, CH, LANES = 100, 1500, 30, 26, 40, 16, 8
HEIGHT = AXIS_H + CH * ROW_H + LANES * LANE_H + 4
FULL_W = LABEL_W + W
LABEL_MIN_PX = 60
ST_X = 2
DATA = Path(__file__).parent.parent / "data"
RESULTS = Path(__file__).parent.parent / "results"


class Data:
    def __init__(self, directory: Path = DATA):
        t = time.perf_counter()
        self.meta = json.loads((directory / "meta.json").read_text())
        self.views = json.loads((directory / "views.json").read_text())
        edges = np.fromfile(directory / "edges.f64", "<f8")
        t0s, t1s = np.fromfile(directory / "ev_t0.f64", "<f8"), np.fromfile(directory / "ev_t1.f64", "<f8")
        o, e = self.meta["edge_offsets"], self.meta["event_offsets"]
        self.channels = [edges[o[c]:o[c + 1]] for c in range(CH)]
        self.lanes = [(name, t0s[e[i]:e[i + 1]], t1s[e[i]:e[i + 1]], self.meta["lane_max_duration"][i])
                      for i, name in enumerate(self.meta["lanes"])]
        self.load_ms = (time.perf_counter() - t) * 1e3


def bounds(v0: float, v1: float) -> np.ndarray:
    return v0 + (np.arange(W + 1, dtype=np.float64) * (v1 - v0)) / W


def decimate_wave(channels, v0: float, v1: float, out: np.ndarray) -> None:
    """out: (CH, W) uint8 に 0 / 1 / ST_X。"""
    b = bounds(v0, v1)
    for c, e in enumerate(channels):
        before = np.searchsorted(e, b[:-1], "right")
        hi = np.searchsorted(e, b[1:], "left")
        out[c] = np.where(before < hi, ST_X, before & 1)


def decimate_lane(lane, v0: float, v1: float):
    """("sparse", x0, x1, 番号) か ("dense", 画素ごとの件数)。"""
    _, t0, t1, max_dur = lane
    i0 = np.searchsorted(t0, v0 - max_dur, "left")
    i1 = np.searchsorted(t0, v1, "left")
    k = W / (v1 - v0)
    if i1 - i0 <= 2 * W:
        x1 = (t1[i0:i1] - v0) * k
        keep = x1 >= 0
        x0 = np.maximum(0, (t0[i0:i1][keep] - v0) * k)
        x1 = np.maximum(x0 + 1, np.minimum(W, x1[keep]))
        return "sparse", x0, x1, np.arange(i0, i1)[keep]
    return "dense", np.diff(np.searchsorted(t0, bounds(v0, v1), "left"))


def axis_ticks(v0: float, v1: float, tick_s: float) -> list[float]:
    raw = (v1 - v0) * tick_s / 10
    p = 10 ** np.floor(np.log10(raw))
    step = next(m * p for m in (1, 2, 5, 10) if m * p >= raw) / tick_s
    t, out = np.ceil(v0 / step) * step, []
    while t < v1:
        out.append(t)
        t += step
    return out


def fmt_time(s: float) -> str:
    a = abs(s)
    k, u = (1e9, "ns") if a < 1e-6 else (1e6, "us") if a < 1e-3 else (1e3, "ms") if a < 1 else (1, "s")
    return f"{float(f'{s * k:.4g}'):g} {u}"


def _argb(r: int, g: int, b: int) -> np.uint32:
    return np.uint32(0xFF000000 | r << 16 | g << 8 | b)


BG, LINE, XFILL, EV, GRID = (_argb(17, 17, 17), _argb(68, 204, 68), _argb(42, 106, 42), _argb(85, 136, 170),
                             _argb(68, 68, 68))


def raster(img: np.ndarray, wave: np.ndarray, lanes_out, ticks_x) -> None:
    """img: (HEIGHT, FULL_W) uint32（ARGB32）に図形を塗る（文字は別）。列ごとの塗りを numpy でまとめて行う。"""
    img[:] = BG
    plot = img[:, LABEL_W:]
    for x in ticks_x:
        img[AXIS_H - 6:, LABEL_W + int(x)] = GRID
    cols = np.arange(W)
    for c in range(CH):
        s = wave[c]
        y_hi, y_lo = AXIS_H + c * ROW_H + 4, AXIS_H + (c + 1) * ROW_H - 5
        plot[y_hi:y_lo, s == ST_X] = XFILL
        plot[y_hi, cols[s == 1]] = LINE
        plot[y_lo, cols[s == 0]] = LINE
        edge = np.nonzero((s[1:] != s[:-1]) & (s[1:] != ST_X) & (s[:-1] != ST_X))[0] + 1
        plot[y_hi:y_lo, edge] = LINE
    ys = np.arange(LANE_H)[:, None]
    for i, out in enumerate(lanes_out):
        top = AXIS_H + CH * ROW_H + i * LANE_H
        if out[0] == "sparse":
            cov = np.zeros(W + 1, np.int32)
            np.add.at(cov, out[1].astype(np.int64), 1)
            np.add.at(cov, np.minimum(W, np.ceil(out[2])).astype(np.int64), -1)
            plot[top + 6:top + LANE_H - 6, np.cumsum(cov[:-1]) > 0] = EV
        else:
            counts = out[1]
            h = (LANE_H - 6) * np.log2(1 + counts) / np.log2(1 + max(1, counts.max()))
            mask = (ys >= LANE_H - 3 - h[None, :]) & (ys < LANE_H - 3) & (counts[None, :] > 0)
            plot[top:top + LANE_H][mask] = EV


def labels(data: Data, v0: float, v1: float, lanes_out, ticks):
    """(x, y, 文字) の一覧: 名前・軸・出来事のラベル。"""
    out = [(8, AXIS_H + c * ROW_H + ROW_H // 2, f"ch{c}") for c in range(CH)]
    out += [(8, AXIS_H + CH * ROW_H + i * LANE_H + LANE_H // 2, lane[0]) for i, lane in enumerate(data.lanes)]
    tick_s = data.meta["tick_s"]
    out += [(LABEL_W + (t - v0) * W / (v1 - v0) + 3, AXIS_H // 2, fmt_time(t * tick_s)) for t in ticks]
    for i, o in enumerate(lanes_out):
        if o[0] != "sparse":
            continue
        y = AXIS_H + CH * ROW_H + i * LANE_H + LANE_H // 2
        wide = (o[2] - o[1]) >= LABEL_MIN_PX
        out += [(LABEL_W + x + 3, y, f"#{n}") for x, n in zip(o[1][wide], o[3][wide])]
    return out


def stats(xs) -> dict:
    a = np.sort(np.asarray(xs, dtype=np.float64))
    q = lambda f: float(a[min(len(a) - 1, int(f * len(a)))])  # noqa: E731
    return {"n": len(a), "mean": float(a.mean()), "p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "max": float(a[-1])}


def save(impl: str, load_ms: float, work: list, intervals: list, warmup: int, **extra) -> None:
    RESULTS.mkdir(exist_ok=True)
    result = {"impl": impl, "load_ms": load_ms, "work_ms": stats(work[warmup:]), "interval_ms": stats(intervals[warmup:]),
              "raw_work": [round(x, 2) for x in work[warmup:]], "raw_interval": [round(x, 2) for x in intervals[warmup:]],
              **extra}
    (RESULTS / f"{impl}.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":                               # 間引きだけの時間と突き合わせ用の和
    d = Data()
    wave = np.zeros((CH, W), np.uint8)
    tw, tl = [], []
    weights = (np.arange(CH * W) % 251 + 1).reshape(CH, W)
    for i, (v0, v1) in enumerate(d.views):
        t = time.perf_counter()
        decimate_wave(d.channels, v0, v1, wave)
        a = time.perf_counter()
        for lane in d.lanes:
            decimate_lane(lane, v0, v1)
        b = time.perf_counter()
        if i >= d.meta["warmup"]:
            tw.append((a - t) * 1e3)
            tl.append((b - a) * 1e3)
        if i in (60, 360, 660, 959):
            print(f"check {i}: {int((wave.astype(np.int64) * weights).sum())}")
    print(json.dumps({"impl": "numpy-decimate", "wave_ms": stats(tw), "lanes_ms": stats(tl)}))
