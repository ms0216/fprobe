"""いまのデーモンの間引き（本体のコードをそのまま呼ぶ。変えない）を、共通データで測る。

- la.Waveform.view（la.view / recording.waveform の中身。純 Python の bisect、画素ごと・チャンネルごと）
- Recording.density（recording.density の中身。画素ごとに SQLite の COUNT）

本体の venv で動かす: .venv/Scripts/python prototypes/ui-bench/server/current_api.py [--tmp <作業ディレクトリ>]
（numpy を使わない。記録は作業ディレクトリに作って最後に消す）
"""

import argparse
import json
import shutil
import statistics
import tempfile
import time
from array import array
from pathlib import Path
from types import SimpleNamespace

from fprobe.la import Waveform
from fprobe.recording import Recorder, Recording

DATA = Path(__file__).parent.parent / "data"
SAMPLE_VIEWS = [60, 160, 260, 360, 460, 560, 660, 760, 860, 959]   # 拡大の途中・移動・縮小から 10 点


def f64(name: str) -> array:
    a = array("d")
    a.frombytes((DATA / name).read_bytes())
    return a


def stats(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": statistics.median(xs), "max": max(xs), "mean": statistics.fmean(xs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--width", type=int, default=1500)
    a = ap.parse_args()
    meta = json.loads((DATA / "meta.json").read_text())
    views = json.loads((DATA / "views.json").read_text())
    out: dict = {"impl": "daemon-current", "width": a.width}

    # --- 波形: Waveform を変化点の表から直接組む（10M 変化点の Transitions から作ると数十秒かかるため）
    edges, o = f64("edges.f64"), meta["edge_offsets"]
    w = object.__new__(Waveform)
    w.tr = SimpleNamespace(length=meta["span"], channels=16)
    w._initial = [0] * 16
    w._edges = [array("Q", (int(x) for x in edges[o[c]:o[c + 1]])) for c in range(16)]
    times = []
    for i in SAMPLE_VIEWS:
        v0, v1 = views[i]
        t = time.perf_counter()
        w.view(int(v0), int(v1), a.width)
        times.append((time.perf_counter() - t) * 1e3)
    out["waveform_view_ms"] = stats(times)
    print("waveform_view", out["waveform_view_ms"], flush=True)

    # --- 出来事: 100 万件を記録に入れて density を測る
    tmp = Path(tempfile.mkdtemp(prefix="uibench-rec-", dir=a.tmp))
    try:
        t0s, t1s, eo = f64("ev_t0.f64"), f64("ev_t1.f64"), meta["event_offsets"]
        rec = Recorder(tmp / "rec", 1e8)
        t = time.perf_counter()
        for lane, name in enumerate(meta["lanes"]):
            rec.add_many((name, "ev", int(t0s[k]), 0, 0, None, int(t1s[k])) for k in range(eo[lane], eo[lane + 1]))
            rec.flush()
        rec.close()
        out["insert_1m_s"] = time.perf_counter() - t
        r = Recording(tmp / "rec")
        one, every = [], []
        for i in SAMPLE_VIEWS:
            v0, v1 = int(views[i][0]), int(views[i][1])
            t = time.perf_counter()
            r.density(v0, v1, a.width, [meta["lanes"][0]])
            one.append((time.perf_counter() - t) * 1e3)
            t = time.perf_counter()
            for name in meta["lanes"]:
                r.density(v0, v1, a.width, [name])
            every.append((time.perf_counter() - t) * 1e3)
        r.close()
        out["density_one_lane_ms"] = stats(one)
        out["density_8_lanes_ms"] = stats(every)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(out, indent=1))
    (Path(__file__).parent.parent / "results" / "daemon-current.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
