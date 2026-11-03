"""ロジアナの流れの「拍 → 変化点 → VCD」を、ネットワークを使わずメモリ上の合成の流れで段ごとに測る（試験の計測用）。

使い方（リポジトリの根から）:
    .venv/Scripts/python.exe tools/bench/la_vcd_bench.py [--mb 64] [--chunk-mb 4] [--only 名前,...] [--python]

段:
    transitions  StreamChunk の変化点（la.StreamChunk.changes があればそれ、無ければ transitions）
    vcd          VcdWriter.write（変化点はあらかじめ作っておき、書き出しだけを測る。ファイルは一時ディレクトリの実ファイル）
    total        両方を続けて（取り込みと書き出しを分ける前の stream_to_vcd の受け取りループの中身）
    capture      取り込みの道（RawCaptureWriter.write のみ。分けた後の受け取りループの中身）。変化点の取り出しも VCD の整形もしない
    export       書き出しの道（生の保存ファイル → RawCapture → 変化点 → VCD。分けた後の raw_to_vcd）
出す数: 変化点/秒・入力（線の拍）の MB/s・VCD の出力の MB/s・実時間比（その時間で処理できた記録の長さ ÷ 経過時間。
800 MS/s として。1 以上なら流しに追いつく）。

--python: numpy を使わない道（fprobe.la_wave._np = None）で測る。
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# 比べる実装の host ディレクトリ（直す前の版を git archive で出したもの等）。既定はこのリポジトリの host
sys.path.insert(0, os.environ.get("FPROBE_HOST") or str(Path(__file__).resolve().parents[2] / "host"))

from fprobe import la, la_vcd, la_wave  # noqa: E402

try:                                                           # 取り込みと書き出しを分ける前の版には無い（git archive の版と比べるとき）
    from fprobe import la_raw as raw_module  # noqa: E402
except ImportError:
    raw_module = None

PERIOD_S = 1.25e-9                     # 800 MS/s（PYNQ の fprobe_pynq_deep_fast の細かいサンプル）
LINE_MB_S = 117.7                      # 線の理論値（D）
WRITER: dict = {}                      # VcdWriter の引数（--workers）


# --- 合成の流れ（拍の列）----------------------------------------------------------------------------------

def rle_square(total: int, period: int, mask: int) -> bytes:
    """RLE: 周期 period の方形波（mask の ch が同時に反転）。1 レコード = 半周期。"""
    n = total // 4
    half = period // 2
    values = np.where(np.arange(n) & 1, mask, 0).astype("<u4")
    return (values | np.uint32(half - 1) << 16).astype("<u4").tobytes()


def rle_dense(total: int, seed: int = 1) -> bytes:
    """RLE: 毎レコードが変化（繰り返し 1 = 毎サンプル変化。16 ch のランダム）。"""
    rng = np.random.default_rng(seed)
    n = total // 4
    v = rng.integers(0, 1 << 16, n, dtype=np.uint32)
    v[1:][v[1:] == v[:-1]] ^= 1                                 # 隣と同じ値を避ける（毎レコード変化）
    return v.astype("<u4").tobytes()


def raw_square(total: int, period: int, bits: int, mask: int) -> bytes:
    """RAW: bits ビットのサンプルを詰めた語（bits = 16 は詰めない）。周期 period の方形波。"""
    samples = total * 8 // bits
    s = np.where((np.arange(samples) % period) >= period // 2, mask, 0).astype(np.uint64)
    return _pack(s, bits)


def raw_random(total: int, bits: int, seed: int = 2) -> bytes:
    """RAW: 毎サンプルが変化するランダムな値（bits ビット）。"""
    rng = np.random.default_rng(seed)
    samples = total * 8 // bits
    s = rng.integers(0, 1 << bits, samples, dtype=np.uint64)
    s[1:][s[1:] == s[:-1]] ^= 1
    return _pack(s, bits)


def _pack(s, bits: int) -> bytes:
    per = 64 // bits
    s = s.reshape(-1, per) << (np.arange(per, dtype=np.uint64) * bits)
    return np.bitwise_or.reduce(s, axis=1).astype("<u8").tobytes()


PACK_OF_BITS = {16: 0, 8: 1, 4: 2, 2: 3, 1: 4}


def scenarios(total: int):
    """(名前, データ, rle, pack, VCD の ch 数)。"""
    yield "rle16_sq300_1ch", rle_square(total, 300, 0x0001), True, 0, 16
    yield "rle16_sq300_16ch", rle_square(total, 300, 0xFFFF), True, 0, 16
    yield "rle16_every", rle_dense(total), True, 0, 16
    yield "raw16_sq300_1ch", raw_square(total, 300, 16, 1), False, 0, 16
    yield "raw16_every", raw_random(total, 16), False, 0, 16
    for bits in (8, 4, 2, 1):
        mask = (1 << bits) - 1
        yield f"pack{bits}ch_sq300", raw_square(total, 300, bits, mask), False, PACK_OF_BITS[bits], bits
        yield f"pack{bits}ch_every", raw_random(total, bits), False, PACK_OF_BITS[bits], bits


def chunks_of(data: bytes, rle: bool, pack: int, size: int) -> list:
    out, first, previous = [], 0, None
    for i in range(0, len(data), size):
        part = data[i:i + size]
        out.append(la.StreamChunk(part, rle, first, previous, pack=pack))
        samples, last = la._samples_of(part, rle, pack)
        first, previous = first + samples, last
    return out


def changes_of(chunk):
    fast = getattr(chunk, "changes", None)
    return fast() if fast is not None else chunk.transitions()


# --- 測る ------------------------------------------------------------------------------------------------

def measure(name, data, rle, pack, channels, chunk_bytes, directory, repeat):
    """repeat 回測り、段ごとに最も速かった回（ほかの処理に邪魔されなかった回）を出す。"""
    runs = [_measure_once(name, data, rle, pack, channels, chunk_bytes, directory) for _ in range(repeat)]
    return [min(rows, key=lambda row: row[-1]) for rows in zip(*runs)]


def _measure_once(name, data, rle, pack, channels, chunk_bytes, directory):
    chunks = chunks_of(data, rle, pack, chunk_bytes)
    samples = sum(c.length for c in chunks)
    t0 = time.perf_counter()
    trs = [changes_of(c) for c in chunks]
    t1 = time.perf_counter()
    count = sum(len(v) for v, _ in trs)
    path = Path(directory) / f"{name}.vcd"
    t2 = time.perf_counter()
    with la_vcd.VcdWriter(path, channels, PERIOD_S, **WRITER) as vcd:
        for v, s in trs:
            vcd.write(v, s)
    t3 = time.perf_counter()
    vcd_bytes = path.stat().st_size
    path.unlink()
    del trs
    t4 = time.perf_counter()
    with la_vcd.VcdWriter(path, channels, PERIOD_S, **WRITER) as vcd:
        for c in chunks:
            vcd.write(*changes_of(c))
    t5 = time.perf_counter()
    path.unlink()
    real = samples * PERIOD_S
    mb = len(data) / 1e6
    rows = []
    stages = [("transitions", t1 - t0), ("vcd", t3 - t2), ("total", t5 - t4)]
    if raw_module is not None:                                    # 分けた後の実装: 取り込みの道と書き出しの道
        meta = raw_module.RawMeta(rle=rle, pack=pack, channels=channels, first_channel=0, sample_period_s=PERIOD_S,
                                  names={}, skew=(), timebase_hz=None)
        rawfile = Path(directory) / f"{name}.fplaraw"
        t6 = time.perf_counter()
        with raw_module.RawCaptureWriter(rawfile, meta) as writer:
            for c in chunks:
                writer.write(c)
        t7 = time.perf_counter()
        raw_module.raw_to_vcd(raw_module.RawCapture(rawfile), path, workers=WRITER.get("workers"))
        t8 = time.perf_counter()
        path.unlink()
        rawfile.unlink()
        stages += [("capture", t7 - t6), ("export", t8 - t7)]
    for stage, dt in stages:
        rows.append((name, stage, count / dt / 1e6, mb / dt, vcd_bytes / 1e6 / dt if stage != "transitions" else 0.0,
                     real / dt, dt))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mb", type=float, default=64, help="各場面の入力の量（MB）")
    ap.add_argument("--chunk-mb", type=float, default=4, help="1 チャンクの大きさ（MB。LaStream.READ_MAX = 4）")
    ap.add_argument("--only", default="", help="測る場面の名前（コンマ区切り）")
    ap.add_argument("--python", action="store_true", help="numpy を使わない道で測る")
    ap.add_argument("--repeat", type=int, default=3, help="測る回数（段ごとに最も速い回を出す）")
    ap.add_argument("--workers", type=int, default=None, help="VcdWriter の workers（既定 = その実装の既定）")
    args = ap.parse_args()
    if args.python:
        la_wave._np = None
        la._np = None
        if hasattr(la_vcd, "_np"):
            la_vcd._np = None
    if args.workers is not None:
        WRITER["workers"] = args.workers
    total = int(args.mb * 1e6) // 16 * 16
    chunk = int(args.chunk_mb * 1e6) // 16 * 16
    wanted = set(filter(None, args.only.split(",")))
    print(f"{'場面':<18} {'段':<11} {'変化点 M/s':>10} {'入力 MB/s':>10} {'VCD MB/s':>9} {'実時間比':>8} {'秒':>7}")
    with tempfile.TemporaryDirectory() as directory:
        for name, data, rle, pack, channels in scenarios(total):
            if wanted and name not in wanted:
                continue
            for row in measure(name, data, rle, pack, channels, chunk, directory, args.repeat):
                n, stage, mt, mb, vmb, rt, dt = row
                print(f"{n:<18} {stage:<11} {mt:>10.2f} {mb:>10.1f} {vmb:>9.1f} {rt:>8.2f} {dt:>7.3f}")
            sys.stdout.flush()
    print(f"（線の理論値 D = {LINE_MB_S} MB/s。CPU: {os.cpu_count()} 論理コア）")


if __name__ == "__main__":
    main()
