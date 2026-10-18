"""性能の測定のタイル（段の多い版。G01 の段の型をすべて）: 試作と同じ波形 16 ch（約 990 万変化点）に、
RTOS のタスク 8 段・割り込み 4 段（入れ子あり）・値 4 段・解読の箱 2 段を足し、timeline.tiles（format 2）と同じ形の
バイナリのフレームにする。視点の列は試作と同じ（views.json）。

    python prototypes/ui-bench/gen_data.py          # 先に試作のデータ（numpy が要る）
    python host/ui/bench/make_rows_tile.py          # host/ui/bench/data/rows-tile.bin（views.json は make_tile.py と同じ）
"""

import json
import sys
from array import array
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "host"))
from fprobe.api.binary import encode_frame  # noqa: E402

SRC = ROOT / "prototypes" / "ui-bench" / "data"
OUT = Path(__file__).resolve().parent / "data"
TASKS, ISRS, VALUES, DECODES = 8, 4, 4, 2


def f64(a: np.ndarray) -> array:
    out = array("d")
    out.frombytes(np.ascontiguousarray(a, dtype="<f8").tobytes())
    return out


def typed(code: str, a: np.ndarray) -> array:
    out = array(code)
    out.frombytes(np.ascontiguousarray(a, dtype={"H": "<u2", "I": "<u4"}[code]).tobytes())
    return out


def lane(i: int, stream: str, rows: list[dict], t: np.ndarray, t_end: np.ndarray, row: np.ndarray, sections: dict,
         labels: list[str], label: np.ndarray, value: np.ndarray | None = None, depth: np.ndarray | None = None) -> dict:
    order = np.argsort(t, kind="stable")
    t, t_end, row, label = t[order], t_end[order], row[order], label[order]
    n = len(t)
    sections[f"lane.{i}.t"] = f64(t)
    sections[f"lane.{i}.t_end"] = f64(t_end)
    sections[f"lane.{i}.kind"] = typed("H", np.zeros(n))
    sections[f"lane.{i}.row"] = typed("H", row)
    sections[f"lane.{i}.label"] = typed("I", label)
    sections[f"lane.{i}.value"] = f64(value[order] if value is not None else np.full(n, np.nan))
    sections[f"lane.{i}.depth"] = (depth[order] if depth is not None else np.zeros(n)).astype(np.uint8).tobytes()
    sections[f"lane.{i}.tone"] = bytes(n)
    sections[f"lane.{i}.address"] = f64(np.full(n, np.nan))
    return {"stream": stream, "mode": "raw", "count": n, "items": n, "kinds": [stream], "rows": rows, "labels": labels}


def main() -> None:
    meta = json.loads((SRC / "meta.json").read_text())
    edges = np.fromfile(SRC / "edges.f64", dtype="<f8")
    span, channels, o = int(meta["span"]), meta["channels"], meta["edge_offsets"]
    rng = np.random.default_rng(2)
    sections = {f"wave.0.{ch}": f64(edges[o[ch]:o[ch + 1]]) for ch in range(channels)}
    lanes = []
    # RTOS: タスクの実行（次の切り替えまで、12.5 万）と割り込み（段ごとに 2.5 万、1〜10 µs、1 割は入れ子）
    t = np.unique(rng.integers(0, span, 125_000)).astype(np.float64)
    task = rng.integers(0, TASKS, len(t))
    t_end = np.append(t[1:], span)
    isr_t = np.sort(rng.integers(0, span, ISRS * 25_000)).astype(np.float64)
    isr_row = rng.integers(0, ISRS, len(isr_t))
    isr_end = isr_t + rng.integers(100, 1000, len(isr_t))
    nested = rng.random(len(isr_t)) < 0.1
    depth = np.concatenate([np.zeros(len(t)), nested.astype(np.float64)])        # 1 割に入れ子の深さ 1（描画の負荷）
    rows = ([{"id": f"rtos.task:{k}", "rule": "task", "type": "intervals", "title": f"task{k}"} for k in range(TASKS)]
            + [{"id": f"rtos.isr:{k}", "rule": "isr", "type": "intervals", "title": f"IRQ{k}"} for k in range(ISRS)])
    labels = [f"task{k}" for k in range(TASKS)] + [f"IRQ{k}" for k in range(ISRS)]
    lanes.append(lane(0, "rtos", rows, np.concatenate([t, isr_t]), np.concatenate([t_end, isr_end]),
                      np.concatenate([task, TASKS + isr_row]), sections, labels,
                      np.concatenate([task, TASKS + isr_row]), depth=depth))
    # 変数のサンプル: 段ごとに 5 万
    st = np.sort(rng.integers(0, span, VALUES * 50_000)).astype(np.float64)
    srow = rng.integers(0, VALUES, len(st))
    value = np.sin(st / span * 40 * (srow + 1)) * 1000 + rng.normal(0, 50, len(st))
    rows = [{"id": f"sample.word:{k}", "rule": "word", "type": "values", "title": f"var{k}"} for k in range(VALUES)]
    lanes.append(lane(1, "sample", rows, st, st, srow, sections, [], np.full(len(st), 0xFFFFFFFF), value=value))
    # 解読の箱: 段ごとに 6 万（UART の 1 バイト = 8.68 µs）
    dt = np.sort(rng.integers(0, span, DECODES * 60_000)).astype(np.float64)
    drow = rng.integers(0, DECODES, len(dt))
    rows = [{"id": f"annotation.decode:{k}", "rule": "decode", "type": "bus", "title": f"UART{k}"} for k in range(DECODES)]
    labels = [f"0x{b:02X}" for b in range(256)]
    lanes.append(lane(2, "annotation", rows, dt, dt + 868, drow, sections, labels, rng.integers(0, 256, len(dt))))
    header = {"format": 2, "source": "recording", "t0": 0, "t1": span, "tick_s": meta["tick_s"], "extent": [0, span],
              "wave": {"mode": "raw", "channels": list(range(channels)), "edges": int(o[-1]),
                       "pieces": [{"t0": 0, "t1": span, "tps": 1, "length": span, "s0": 0, "s1": span,
                                   "channels": list(range(channels)), "levels": [0] * channels}]},
              "lanes": lanes}
    OUT.mkdir(exist_ok=True)
    frame = encode_frame({"jsonrpc": "2.0", "id": 1, "result": header}, sections)
    (OUT / "rows-tile.bin").write_bytes(frame)
    print(f"rows-tile.bin {len(frame) / 2**20:.1f} MB, 段 = 波形 {channels} + タスク {TASKS} + 割り込み {ISRS} + 値 {VALUES}"
          f" + 解読 {DECODES}（+ まとまりの見出し 4）")


if __name__ == "__main__":
    main()
