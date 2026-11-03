"""共通の時間軸の記録（fprobe.recording・recording_long・recording_stream）の段ごとの速さを、合成の記録で測る（試験の計測用）。

使い方（リポジトリの根から）:
    .venv/Scripts/python.exe tools/bench/recording_bench.py [--events 1000000] [--only 段,...] [--repeat 3]
    FPROBE_HOST=<直す前の host> ...   比べる実装（git archive で出した前の版等）

段（出すのは測った事実: 件数・秒・件/秒。秒は repeat 回の最も速い回の経過時間と、その回の CPU 時間）:
  floor_*      SQLite の 1 件の挿入の下限 D（Python の executemany。索引 0〜4 個・列の型ごと。記録の作りの外の生の表）
  add_many     Recorder.add_many（PC の側の行の組み立てだけ。SQLite に書かない）
  flush        Recorder.flush（SQLite への書き込み）
  write        add_many + flush（flush_rows 件ずつ。収集の道と同じ繰り返し）
  waveform     Recorder.add_waveform（1 秒ぶんのかたまりの変化点 --wave-transitions 個 × --wave-chunks）
  stream       LaRecordingStream の受け取りの道（RLE の流れの StreamChunk → 変化点 → かたまり → add_waveform）。
               LaStream の代わりに合成の拍の列を渡す（機器・リンクを使わない）
  iter_all / iter_range / iter_stream / iter_kind / iter_overlap   Recording.iter_events（全部・10 % の範囲・stream・
               stream と kind・区間を含める）
  count / density / density_stream / event_bins / kind_counts / span / distinct / nearest
  export_vcd / export_csv / export_json
  long_write   SegmentedRecorder（分割 4 つ。閉じるときの縮図の事前計算を含む）
理論の目安: 記録に入る量の上限 = リンクの 117.7 MB/s（1 GbE の中身）を全部イベント（16 バイト）にしたとき 7.36 M 件/秒、
全部 RLE の変化点（4 バイト）にしたとき 29.4 M 変化点/秒。書き込みの段の「上限比」= 件/秒 ÷ その上限（1 以上なら追いつく）。
"""

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from array import array
from pathlib import Path

sys.path.insert(0, os.environ.get("FPROBE_HOST") or str(Path(__file__).resolve().parents[2] / "host"))

from fprobe import recording, recording_long  # noqa: E402
from fprobe.la import Transitions  # noqa: E402

HZ = 100_000_000                      # タイムベース 100 MHz（10 ns）
LINE_MB_S = 117.7                     # リンクの理論値（la_vcd_bench と同じ）
EVENT_BYTES = 16
EVENTS_MAX = LINE_MB_S * 1e6 / EVENT_BYTES          # 件/秒
RLE_MAX = LINE_MB_S * 1e6 / 4                       # 変化点/秒
STREAMS = (("dap", ("ap_read", "ap_write", "dp_read")), ("sample", ("word",)), ("uart", ("rx",)),
           ("fpga", ("value", "group", "gap")))


# --- 合成の出来事 --------------------------------------------------------------------------------------------

def event_rows(first: int, n: int, seed: int = 1) -> list[tuple]:
    """(stream, kind, t, a, b, body, t_end) の列。dap 4 割（本体あり）・sample 3 割・uart 1 割・fpga 2 割（値がほとんど、
    組の切り替え・欠けがまれ）、50 件に 1 件は区間（t_end）。時刻は 10 刻みでほぼ昇順（±3 前後する）。"""
    rnd = random.Random(seed + first)
    out = []
    for i in range(first, first + n):
        r = i % 10
        t = i * 10 + rnd.randint(-3, 3)
        if r < 4:
            row = ("dap", STREAMS[0][1][i % 3], t, 0x2000_0000 + (i & 0xFFF) * 4, i & 0xFFFF,
                   {"ack": 1, "requested": t - 50, "t_lo": t - 40, "t_hi": t - 2})
        elif r < 7:
            row = ("sample", "word", t, 0x2000_0100, i & 0xFFFFFFFF, None)
        elif r < 8:
            row = ("uart", "rx", t, i & 0xFF, 0, None)
        else:
            kind = "group" if i % 997 == 0 else "gap" if i % 1499 == 0 else "value"
            row = ("fpga", kind, t, i % 3, i & 0xFFFF, None)
        if i % 50 == 0:
            row = (*row[:6], t + 400)
        out.append(row)
    return out


def write_events(directory: Path, n: int, flush_rows: int) -> tuple[float, float, float, float]:
    """(add_many の秒, flush の秒, add_many の CPU 秒, flush の CPU 秒)。行の生成は測らない。"""
    rec = recording.Recorder(directory, HZ)
    add = flush = add_cpu = flush_cpu = 0.0
    for first in range(0, n, flush_rows):
        rows = event_rows(first, min(flush_rows, n - first))
        w0, c0 = time.perf_counter(), time.process_time()
        rec.add_many(rows)
        w1, c1 = time.perf_counter(), time.process_time()
        rec.flush()
        w2, c2 = time.perf_counter(), time.process_time()
        add, flush, add_cpu, flush_cpu = add + w1 - w0, flush + w2 - w1, add_cpu + c1 - c0, flush_cpu + c2 - c1
    rec.close()
    return add, flush, add_cpu, flush_cpu


# --- 合成の波形 ----------------------------------------------------------------------------------------------

def square(transitions: int, period: int, channels: int = 16) -> Transitions:
    values = array("H", [(0xFFFF if i & 1 else 0) & ((1 << channels) - 1) for i in range(transitions)])
    starts = array("Q", range(0, transitions * period, period))
    return Transitions(values, starts, transitions * period, 0, 0, 1e-8, channels)


def rle_chunks(transitions: int, period: int, chunk_records: int):
    """RLE の流れ（1 レコード = 4 バイト: 値 16 ビット + 繰り返し − 1）の StreamChunk の列。方形波（全 ch が反転）。"""
    import numpy as np

    from fprobe import la
    values = np.where(np.arange(transitions) & 1, 0xFFFF, 0).astype("<u4")
    data = (values | np.uint32(period - 1) << 16).astype("<u4").tobytes()
    out, first, previous = [], 0, None
    for i in range(0, len(data), chunk_records * 4):
        part = data[i:i + chunk_records * 4]
        out.append(la.StreamChunk(part, True, first, previous))
        samples, last = la._samples_of(part, True, 0)
        first, previous = first + samples, last
    return out


class FakeStream:
    """LaRecordingStream._run が読む LaStream の口だけ（合成のかたまりを返す）。"""

    def __init__(self, chunks, period_s: float, channels: int):
        self._chunks, self.sample_period_s, self.channels = chunks, period_s, channels
        self.carries_events = False

    def chunks(self, _duration):
        yield from self._chunks

    def stats(self):
        from types import SimpleNamespace
        n = sum(c.length for c in self._chunks)
        return SimpleNamespace(samples=n, overrun=False, gaps=(), gap_samples=0, unplaced_beats=0,
                               link_buffer_dict=None)


def write_stream(directory: Path, transitions: int, period: int) -> int:
    from fprobe.recording_stream import LaRecordingStream, StreamAnchor
    rec = recording.Recorder(directory, HZ)
    chunks = rle_chunks(transitions, period, 1 << 20)
    s = LaRecordingStream(object(), rec, chunk_s=1.0)
    s.stream = FakeStream(chunks, 1e-8, 16)
    s.anchor = StreamAnchor(0, 0.0, "circuit", 0, 0, None)
    s._run()
    rec.close()
    return s.blocks


# --- 測る ----------------------------------------------------------------------------------------------------

RESULTS: list[tuple] = []


def report(stage: str, count: float, wall: float, cpu: float, limit: float | None = None) -> None:
    rate = count / wall if wall > 0 else float("inf")
    ratio = f"{rate / limit:8.3f}" if limit else " " * 8
    RESULTS.append((stage, count, wall, cpu, rate))
    print(f"{stage:<16} {count:>12,.0f} {wall:>9.3f} {cpu:>9.3f} {rate:>14,.0f} {ratio}", flush=True)


def best(fn, repeat: int):
    """repeat 回のうち経過時間が最も短い回の (経過, CPU, 戻り値)。"""
    out = None
    for _ in range(repeat):
        w0, c0 = time.perf_counter(), time.process_time()
        value = fn()
        w, c = time.perf_counter() - w0, time.process_time() - c0
        if out is None or w < out[0]:
            out = (w, c, value)
    return out


def floor(directory: Path, n: int, repeat: int, only) -> None:
    rows_int = [(i * 10, None, i % 4, i % 7, i, 0, None, None) for i in range(n)]
    rows_text = [(i * 10, None, ("dap", "sample", "uart", "fpga")[i % 4], "k%d" % (i % 7), i, 0, None, None)
                 for i in range(n)]
    idx = ["CREATE INDEX i1 ON events(t);", "CREATE INDEX i2 ON events(stream,t);",
           "CREATE INDEX i3 ON events(stream,kind,t);", "CREATE INDEX i4 ON events(t_end) WHERE t_end IS NOT NULL;"]
    for typ, rows in (("int", rows_int), ("text", rows_text)):
        for k in range(len(idx) + 1):
            stage = f"floor_{typ}_idx{k}"
            if only and stage not in only and "floor" not in only:
                continue

            def run():
                path = directory / f"{stage}.sqlite"
                for p in directory.glob(f"{stage}.sqlite*"):
                    p.unlink()
                db = sqlite3.connect(path, isolation_level=None)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.executescript(f"CREATE TABLE events (t INTEGER NOT NULL, t_end INTEGER, stream {typ.upper()} NOT NULL, "
                                 f"kind {typ.upper()} NOT NULL, a INTEGER, b INTEGER, body TEXT, unc REAL);" + "".join(idx[:k]))
                for j in range(0, n, 100_000):
                    db.execute("BEGIN")
                    db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", rows[j:j + 100_000])
                    db.execute("COMMIT")
                db.close()
            wall, cpu, _ = best(run, repeat)
            report(stage, n, wall, cpu, EVENTS_MAX)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", type=int, default=1_000_000)
    ap.add_argument("--flush-rows", type=int, default=100_000, help="1 回の flush の件数")
    ap.add_argument("--floor-events", type=int, default=300_000)
    ap.add_argument("--wave-chunks", type=int, default=8)
    ap.add_argument("--wave-transitions", type=int, default=1_000_000, help="1 つのかたまりの変化点")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--only", default="", help="段の名前（コンマ区切り。floor は floor_* の全部）")
    ap.add_argument("--dir", default=None, help="記録を作る場所（既定 = 一時ディレクトリ）")
    ap.add_argument("--json", default=None, help="結果を JSON で書く")
    args = ap.parse_args()
    only = set(filter(None, args.only.split(",")))
    want = (lambda s: not only or s in only)
    root = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="recbench-"))
    root.mkdir(parents=True, exist_ok=True)
    n = args.events
    print(f"host: {recording.__file__}")
    print(f"{'段':<16} {'件数':>12} {'経過 秒':>9} {'CPU 秒':>9} {'件/秒':>14} {'上限比':>8}")
    try:
        if not only or any(s.startswith("floor") for s in only):
            floor(root, args.floor_events, args.repeat, only)

        main_rec = root / "main"
        if want("write") or want("add_many") or want("flush") or not main_rec.exists():
            def run():
                shutil.rmtree(main_rec, ignore_errors=True)
                return write_events(main_rec, n, args.flush_rows)
            wall, cpu, (add, flush, add_cpu, flush_cpu) = best(run, args.repeat)
            report("add_many", n, add, add_cpu, EVENTS_MAX)
            report("flush", n, flush, flush_cpu, EVENTS_MAX)
            report("write", n, add + flush, add_cpu + flush_cpu, EVENTS_MAX)

        wave_rec = root / "wave"
        if want("waveform") or want("export_vcd"):
            trs = [square(args.wave_transitions, 3) for _ in range(args.wave_chunks)]

            def run():                                         # かたまりのファイルを書くところだけを測る
                shutil.rmtree(wave_rec, ignore_errors=True)
                rec = recording.Recorder(wave_rec, HZ)
                w0, c0 = time.perf_counter(), time.process_time()
                for k, tr in enumerate(trs):
                    rec.add_waveform(tr, k * tr.length)
                w, c = time.perf_counter() - w0, time.process_time() - c0
                rec.add_many(event_rows(0, 200_000))           # FPGA の観測の値も書き出しに載せる（測らない）
                rec.close()
                return w, c
            _, _, (wall, cpu) = best(run, args.repeat)
            report("waveform", args.wave_transitions * args.wave_chunks, wall, cpu, RLE_MAX)

        if want("stream"):
            def run():
                d = root / "stream"
                shutil.rmtree(d, ignore_errors=True)
                return write_stream(d, args.wave_transitions * args.wave_chunks, 3)
            wall, cpu, _ = best(run, args.repeat)
            report("stream", args.wave_transitions * args.wave_chunks, wall, cpu, RLE_MAX)

        r = recording.Recording.open(main_rec)
        span = n * 10
        lo, hi = span * 45 // 100, span * 55 // 100
        reads = {
            "iter_all": lambda: sum(1 for _ in r.iter_events()),
            "iter_range": lambda: sum(1 for _ in r.iter_events(lo, hi)),
            "iter_stream": lambda: sum(1 for _ in r.iter_events(streams=["fpga"])),
            "iter_kind": lambda: sum(1 for _ in r.iter_events(streams=["dap"], kinds=["ap_read"])),
            "iter_overlap": lambda: sum(1 for _ in r.iter_events(lo, hi, streams=["dap"], overlapping=True)),
            "count": lambda: r.count_events(lo, hi, streams=["dap"]),
            "density": lambda: sum(r.density(0, span, 2000)),
            "density_stream": lambda: sum(r.density(0, span, 2000, streams=["dap"])),
            "event_bins": lambda: sum(sum(v) for v in r.event_bins(0, span // 2000 + 1, 2000).values()),
            "kind_counts": lambda: sum(x[2] for x in r.kind_counts()),
            "span": lambda: [r.span() for _ in range(1000)] and 1000,
            "distinct": lambda: (r._distinct.clear(), len(r.distinct("a", ["fpga"])))[1],
            "nearest": lambda: sum(1 for k in range(1000)
                                   if r.nearest(span * k // 1000, k & 1 == 0, "fpga", "group") is not None),
        }
        for stage, fn in reads.items():
            if want(stage):
                wall, cpu, count = best(fn, args.repeat)
                report(stage, count, wall, cpu)
        r.close()

        if want("export_vcd"):
            w = recording.Recording.open(wave_rec)
            w.update_meta(**{"fpga.signals": {}})
            out = root / "out.vcd"
            wall, cpu, _ = best(lambda: w.export_vcd(out), args.repeat)
            report("export_vcd", out.stat().st_size, wall, cpu)
            print(f"{'':16} （件数の列は VCD のバイト数）")
            w.close()
        r = recording.Recording.open(main_rec)
        for stage, fn in (("export_csv", r.export_csv), ("export_json", r.export_json)):
            if want(stage):
                out = root / f"out.{stage[7:]}"
                wall, cpu, _ = best(lambda: fn(out, t0=0, t1=span // 10), args.repeat)
                report(stage, n // 10, wall, cpu)
        r.close()

        if want("long_write"):
            def run():
                d = root / "long"
                shutil.rmtree(d, ignore_errors=True)
                now = [0.0]
                rec = recording_long.SegmentedRecorder(d, HZ, policy=recording_long.Policy(segment_s=1.0),
                                                       clock=lambda: now[0])
                per = n // 4
                for seg in range(4):
                    for first in range(seg * per, (seg + 1) * per, args.flush_rows):
                        rec.add_many(event_rows(first, min(args.flush_rows, (seg + 1) * per - first)))
                        rec.flush()
                    now[0] += 2.0
                rec.close()
            wall, cpu, _ = best(run, args.repeat)
            report("long_write", n // 4 * 4, wall, cpu, EVENTS_MAX)
    finally:
        if args.json:
            Path(args.json).write_text(json.dumps([dict(zip(("stage", "count", "wall_s", "cpu_s", "per_s"), r))
                                                   for r in RESULTS], ensure_ascii=False, indent=1), encoding="utf-8")
        if not args.dir:
            shutil.rmtree(root, ignore_errors=True)
    print(f"（上限比 = 件/秒 ÷ リンクの上限: イベント {EVENTS_MAX / 1e6:.2f} M 件/秒・RLE の変化点 {RLE_MAX / 1e6:.1f} M/秒。"
          f"CPU: {os.cpu_count()} 論理コア）")


if __name__ == "__main__":
    main()
