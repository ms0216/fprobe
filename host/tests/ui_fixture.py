"""画面の間引き（host/ui/src/lib/timeline/decimate.ts）の試験の固定データを作る: タイルのバイナリのフレームと、
同じ視点の Python の結果（la.Waveform.view・Recording.waveform_view・Recording.density）。

    python host/tests/ui_fixture.py        # host/ui/tests/fixtures/ に書く（test_timeline_tiles が最新かを確かめる）
"""

import json
import random
import sys
import tempfile
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fprobe.api.binary import encode_frame  # noqa: E402
from fprobe.la import Transitions, Waveform  # noqa: E402
from fprobe.recording import Recorder, Recording  # noqa: E402
from fprobe.timeline import capture_tile, recording_tile  # noqa: E402
from fprobe.timeline_rows import classify  # noqa: E402
from timeline_ref import row_density, value_envelope  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "ui" / "tests" / "fixtures"
HZ = 1_000_000


def _transitions(rng: random.Random, length: int, channels: int, gap: int, period_s: float) -> Transitions:
    values, starts, v, i = array("H"), array("Q"), rng.randrange(1 << channels), 0
    while i < length:
        values.append(v)
        starts.append(i)
        i += rng.randrange(1, gap)
        v ^= 1 << rng.randrange(channels)
    return Transitions(values, starts, length, length // 4, 0, period_s, channels)


def _frame(tile) -> bytes:
    return encode_frame({"jsonrpc": "2.0", "id": 1, "result": tile.result}, tile.sections)


def _random_views(rng: random.Random, lo: int, hi: int, n: int) -> list[tuple[int, int, int]]:
    out = [(lo, hi, 300), (lo, lo + 7, 50), (hi - 1, hi, 3), (lo, hi, 1)]
    for _ in range(n):
        a = rng.randrange(lo, hi - 1)
        b = rng.randrange(a + 1, min(hi, a + rng.choice([10, 1000, hi])) + 1)
        out.append((a, b, rng.choice([1, 17, 300, 600])))
    return out


def _aligned_views(rng: random.Random, start: int, end: int, bin_: int, n: int) -> list[tuple[int, int, int]]:
    """画素の境が要約の区間の境にそろう視点（要約からの間引きが正確になる）。"""
    out = []
    while len(out) < n:
        width = rng.choice([10, 50, 120])
        per = rng.choice([1, 2, 3])
        v0 = start + bin_ * rng.randrange(0, (end - start) // bin_)
        v1 = v0 + width * per * bin_
        if v1 <= end:
            out.append((v0, v1, width))
    return out


def _capture_cases(rng: random.Random) -> tuple[dict, dict]:
    tr = _transitions(rng, 20_000, 6, 40, 40e-9)
    wave = Waveform(tr)
    info = {"id": 1, "length": tr.length, "sample_period_ns": 40.0, "trigger_index": tr.trigger, "channels": 6,
            "source": "la.capture"}
    files, cases = {}, []
    tile = capture_tile(wave, info, 0)
    files["capture-raw.bin"] = _frame(tile)
    cases.append({"name": "捕捉・全体の生データ", "frame": "capture-raw.bin",
                  "views": [{"v0": a, "v1": b, "width": w, "wave": wave.view(a, b, w)}
                            for a, b, w in _random_views(rng, 0, 20_000, 20)]})
    tile = capture_tile(wave, info, 5000, 15_000, channels=[0, 2, 5])
    files["capture-part.bin"] = _frame(tile)
    cases.append({"name": "捕捉・一部の範囲と一部のチャンネル", "frame": "capture-part.bin",
                  "views": [{"v0": a, "v1": b, "width": w, "wave": [wave.view(a, b, w)[ch] for ch in (0, 2, 5)]}
                            for a, b, w in _random_views(rng, 5000, 15_000, 20)]})
    tile = capture_tile(wave, info, 0, bin=16, max_edges=10)
    w = tile.result["wave"]
    files["capture-summary.bin"] = _frame(tile)
    cases.append({"name": "捕捉・要約（そろった視点）", "frame": "capture-summary.bin",
                  "views": [{"v0": a, "v1": b, "width": n, "wave": wave.view(a, b, n)}
                            for a, b, n in _aligned_views(rng, w["start"], 20_000, w["bin"], 20)]})
    return files, cases


def _recording_cases(rng: random.Random, directory: Path) -> tuple[dict, dict]:
    rec = Recorder(directory, HZ)
    rec.add_waveform(_transitions(rng, 5000, 3, 60, 1 / HZ), 1000)
    rec.add_waveform(_transitions(rng, 4000, 3, 60, 2 / HZ), 9000)
    for _ in range(3000):
        t = rng.randrange(0, 20_000)
        rec.add(rng.choice(["dap", "uart", "rtt"]), rng.choice(["rx", "tx"]), t, t_end=t + rng.randrange(0, 50))
    rec.close()
    view = Recording(directory)
    try:
        files, cases = {}, []
        tile = recording_tile(view, 0, 20_000)
        streams = [lane["stream"] for lane in tile.result["lanes"]]
        files["recording-raw.bin"] = _frame(tile)
        cases.append({"name": "記録・生データ（2 つのかたまりと隙間、3 段の出来事）", "frame": "recording-raw.bin",
                      "views": [{"v0": a, "v1": b, "width": w, "wave": view.waveform_view(a, b, w),
                                 "lanes": {s: view.density(a, b, w, [s]) for s in streams}}
                                for a, b, w in _random_views(rng, 0, 20_000, 20)]})
        tile = recording_tile(view, 0, 20_000, bin=64, max_edges=10, max_events=10)
        files["recording-summary.bin"] = _frame(tile)
        start = tile.result["wave"]["start"]
        cases.append({"name": "記録・要約（そろった視点）", "frame": "recording-summary.bin",
                      "views": [{"v0": a, "v1": b, "width": w, "wave": view.waveform_view(a, b, w),
                                 "lanes": {s: view.density(a, b, w, [s]) for s in streams}}
                                for a, b, w in _aligned_views(rng, start, 20_000, 64, 20)]})
        return files, cases
    finally:
        view.close()


def _rows_recording(rng: random.Random, directory: Path) -> None:
    """段の規則のすべての型: タスク 3 つ・入れ子の割り込み・UART の箱・変数 2 つと電圧の値・状態・マーカー。"""
    rec = Recorder(directory, HZ)
    t, k = 0, 0
    while t < 20_000:
        d = rng.randrange(20, 400)
        rec.add("rtos", "run", t, a=0x100 + k % 3, body={"task": ["main", "comm", "IDLE"][k % 3]}, t_end=t + d)
        t, k = t + d, k + 1
    for t in range(150, 20_000, 700):
        rec.add("rtos", "isr", t, a=54, body={"name": "USART2"}, t_end=t + 60)
        if rng.random() < 0.5:
            rec.add("rtos", "isr", t + 10, a=15, body={"name": "SysTick"}, t_end=t + 30)
    for t in range(500, 20_000, 1500):
        for i, ch in enumerate(b"ok\r\n"):
            rec.add("uart", "tx", t + 90 * i, b=ch, t_end=t + 90 * i + 87)
    for t in range(0, 20_000, rng.randrange(40, 60)):
        rec.add("sample", "word", t, a=0x20000010, b=t // 7 + rng.randrange(0, 50))
        rec.add("sample", "word", t + 3, a=0x20000014, b=rng.randrange(0, 4096))
    for t in range(100, 20_000, 300):
        rec.add("aux", "mv", t, b=3300 - (t // 1000) * 10)
    for t, state in ((0, "RUNNING"), (4000, "HALTED"), (4600, "RUNNING"), (15_000, "HALTED")):
        rec.add("target", "state", t, body={"state": state})
    for t in (900, 7777, 18_000):
        rec.add("annotation", "mark", t, body={"text": f"mark {t}"})
    rec.close()


def _row_expectations(view: Recording, tile, v0: int, v1: int, width: int, summary: bool) -> dict:
    """タイルの段ごとの Python の結果: 件数（Recording.density と同じ境）と、値の段の画素ごとの [最小, 最大]。"""
    t0, t1 = tile.result["t0"], tile.result["t1"]
    out = {}
    for lane in tile.result["lanes"]:
        events = view.events(None, t1, [lane["stream"]])
        c = classify(lane["stream"], events)
        for row in c.table.rows:
            r = [x.id for x in c.table.rows].index(row.id)
            mine = [(e.t, v) for e, rr, v in zip(events, c.row, c.value) if rr == r]
            if row.rule.type == "values":
                out[row.id] = value_envelope(mine, v0, v1, width, t0, t1, hold=not summary)
            else:
                out[row.id] = row_density([t for t, _ in mine], v0, v1, width)
    return out


def _rows_cases(rng: random.Random, directory: Path) -> tuple[dict, list]:
    _rows_recording(rng, directory)
    view = Recording(directory)
    try:
        files, cases = {}, []
        for name, title, (t0, t1), kw, aligned in (
                ("rows-raw.bin", "段・生データ（全体）", (0, 20_000), {}, False),
                ("rows-part.bin", "段・生データ（一部。状態と値は範囲の前の最後の出来事を持つ）", (6000, 14_000), {}, False),
                ("rows-summary.bin", "段・要約（そろった視点）", (0, 20_000), {"bin": 64, "max_events": 10}, True)):
            tile = recording_tile(view, t0, t1, **kw)
            files[name] = _frame(tile)
            views = (_aligned_views(rng, next(x["start"] for x in tile.result["lanes"] if x["mode"] == "summary"), t1, 64, 6) if aligned
                     else _random_views(rng, t0, t1, 6))
            cases.append({"name": title, "frame": name,
                          "views": [{"v0": a, "v1": b, "width": w, "rows": _row_expectations(view, tile, a, b, w, aligned)}
                                    for a, b, w in views]})
        return files, cases
    finally:
        view.close()


def build() -> dict[str, bytes]:
    """固定データのファイル名 → 中身。"""
    rng = random.Random(20261018)
    files, cases = _capture_cases(rng)
    with tempfile.TemporaryDirectory() as tmp:
        more, more_cases = _recording_cases(rng, Path(tmp) / "r")
        rows, row_cases = _rows_cases(random.Random(20261019), Path(tmp) / "rows")
    files.update(more)
    files.update(rows)
    files["decimate.json"] = (json.dumps({"cases": cases + more_cases}, ensure_ascii=False, indent=0) + "\n").encode()
    files["rows.json"] = (json.dumps({"cases": row_cases}, ensure_ascii=False, indent=0) + "\n").encode()
    return files


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (OUT / name).write_bytes(data)
        print(f"{OUT / name}: {len(data):,} バイト")
