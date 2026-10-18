"""性能の測定のタイルを作る: 試作（prototypes/ui-bench）と同じデータ（16 ch・約 990 万変化点、8 段 × 12.5 万件）を、
timeline.tiles と同じ形のバイナリのフレーム（生データ、1 つのかたまり）にする。

    python prototypes/ui-bench/gen_data.py          # 先に試作のデータを作る（numpy が要る）
    python host/ui/bench/make_tile.py               # host/ui/bench/data/tile.bin・views.json
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


def f64(a: np.ndarray) -> array:
    out = array("d")
    out.frombytes(np.ascontiguousarray(a, dtype="<f8").tobytes())
    return out


def main() -> None:
    meta = json.loads((SRC / "meta.json").read_text())
    edges = np.fromfile(SRC / "edges.f64", dtype="<f8")
    t0s, t1s = np.fromfile(SRC / "ev_t0.f64", dtype="<f8"), np.fromfile(SRC / "ev_t1.f64", dtype="<f8")
    span, channels = int(meta["span"]), meta["channels"]
    o, e = meta["edge_offsets"], meta["event_offsets"]
    sections = {f"wave.0.{ch}": f64(edges[o[ch]:o[ch + 1]]) for ch in range(channels)}
    lanes = []
    for i, name in enumerate(meta["lanes"]):
        sections[f"lane.{i}.t"] = f64(t0s[e[i]:e[i + 1]])
        sections[f"lane.{i}.t_end"] = f64(t1s[e[i]:e[i + 1]])
        sections[f"lane.{i}.kind"] = array("H", [0]) * (e[i + 1] - e[i])
        lanes.append({"stream": name, "mode": "raw", "count": e[i + 1] - e[i], "items": e[i + 1] - e[i], "kinds": [name]})
    header = {"format": 1, "source": "recording", "t0": 0, "t1": span, "tick_s": meta["tick_s"], "extent": [0, span],
              "wave": {"mode": "raw", "channels": list(range(channels)), "edges": int(o[-1]),
                       "pieces": [{"t0": 0, "t1": span, "tps": 1, "length": span, "s0": 0, "s1": span,
                                   "channels": list(range(channels)), "levels": [0] * channels}]},
              "lanes": lanes}
    OUT.mkdir(exist_ok=True)
    frame = encode_frame({"jsonrpc": "2.0", "id": 1, "result": header}, sections)
    (OUT / "tile.bin").write_bytes(frame)
    views = [[round(a), max(round(a) + 1, round(b))] for a, b in json.loads((SRC / "views.json").read_text())]
    (OUT / "views.json").write_text(json.dumps({"warmup": meta["warmup"], "width": meta["width"], "views": views}))
    print(f"tile.bin {len(frame) / 2**20:.1f} MB, {len(views)} 視点")


if __name__ == "__main__":
    main()
