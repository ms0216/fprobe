"""UI フレームワーク比較の共通データを作る（すべての試作が同じファイルを読む）。

- 波形: 16 ch、チャンネルごとの変化点の時刻（タイムベース = 10 ns、1 秒 = 1e8）。合計 約 1000 万の変化点。
  fprobe の `la.Waveform` と同じ「チャンネルごとの変化点の表」の形（初期値 0）。
- 出来事: 8 段 × 12.5 万件 = 100 万件の区間 [t0, t1)。段ごとに t0 の昇順。
- 視点: 拡大 300 フレーム → 移動 300 フレーム → 縮小 300 フレーム（+ 先頭に慣らしの 60 フレーム）。

ファイル（data/、リトルエンディアンの float64）: edges.f64, ev_t0.f64, ev_t1.f64, meta.json, views.json
使い方: python gen_data.py
"""

import json
from pathlib import Path

import numpy as np

SPAN = 100_000_000                       # 1 秒（10 ns のタイムベース）
EDGES_M = [3.1, 2.0, 1.2, 0.9, 0.7, 0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.12, 0.1, 0.05, 0.02, 0.01]   # 百万
LANES = [("DAP", 300), ("UART0", 8680), ("SWO", 500), ("RTT", 2000), ("task", -1), ("irq", 100),
         ("watch", 0), ("AI/human", 5000)]
EVENTS_PER_LANE = 125_000
WARMUP, PHASE = 60, 300
OUT = Path(__file__).parent / "data"


def channel_edges(rng: np.random.Generator, n: int) -> np.ndarray:
    """半分は一様、半分は 1000 か所の塊（バースト）に集まる変化点。"""
    uniform = rng.integers(0, SPAN, n // 2)
    centers = rng.integers(0, SPAN, 1000)
    burst = (rng.choice(centers, n - n // 2) + rng.normal(0, 20_000, n - n // 2)).astype(np.int64)
    edges = np.unique(np.clip(np.concatenate([uniform, burst]), 1, SPAN - 1))
    return edges.astype(np.float64)


def lane_events(rng: np.random.Generator, duration: int) -> tuple[np.ndarray, np.ndarray]:
    t0 = np.unique(rng.integers(0, SPAN, EVENTS_PER_LANE)).astype(np.float64)
    gap = np.diff(np.append(t0, SPAN))
    if duration < 0:                         # タスク: 次の切り替えまで続く
        t1 = t0 + gap
    else:
        t1 = t0 + np.minimum(gap * 0.8, duration)
    return t0, t1


def views() -> list[list[float]]:
    out = []

    def add(center: float, span: float) -> None:
        span = min(SPAN, span)
        start = min(max(0.0, center - span / 2), SPAN - span)
        out.append([start, start + span])

    zoom_in = [SPAN * (1e-5) ** (i / (PHASE - 1)) for i in range(PHASE)]        # 1 s → 10 µs
    for i in range(WARMUP):
        add(SPAN / 2, zoom_in[i * PHASE // WARMUP])
    for i, s in enumerate(zoom_in):
        add(SPAN / 2 + 1.2345e7 * i / (PHASE - 1), s)
    c = SPAN / 2 + 1.2345e7
    for i in range(PHASE):                                                     # 1 ms の窓で右へ移動
        add(c + i * 1e5 / 30, 1e5)
    c += PHASE * 1e5 / 30
    for i in range(PHASE):                                                     # 1 ms → 1 s
        add(c - 3e6 * i / (PHASE - 1), 1e5 * 1e3 ** (i / (PHASE - 1)))
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rng = np.random.default_rng(1)
    edges = [channel_edges(rng, int(m * 1e6)) for m in EDGES_M]
    ev = [lane_events(rng, d) for _, d in LANES]
    np.concatenate(edges).astype("<f8").tofile(OUT / "edges.f64")
    np.concatenate([e[0] for e in ev]).astype("<f8").tofile(OUT / "ev_t0.f64")
    np.concatenate([e[1] for e in ev]).astype("<f8").tofile(OUT / "ev_t1.f64")
    meta = {
        "span": SPAN, "tick_s": 1e-8, "width": 1500, "warmup": WARMUP,
        "channels": len(edges), "edge_offsets": np.cumsum([0] + [len(e) for e in edges]).tolist(),
        "lanes": [n for n, _ in LANES], "event_offsets": np.cumsum([0] + [len(e[0]) for e in ev]).tolist(),
        "lane_max_duration": [float((e[1] - e[0]).max()) for e in ev],
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1))
    (OUT / "views.json").write_text(json.dumps(views()))
    print(f"edges={meta['edge_offsets'][-1]:,} events={meta['event_offsets'][-1]:,} frames={len(views())}")


if __name__ == "__main__":
    main()
