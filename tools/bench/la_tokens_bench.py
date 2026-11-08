"""トークン形式 RLE のホストの復号の速さ（M トークン/s）を、メモリ上の合成の塊で測る（ネットワーク・実機は使わない）。

使い方（リポジトリの根から）:
    .venv/Scripts/python.exe tools/bench/la_tokens_bench.py [--mb 3] [--repeats 7] [--threads 4]

列（各 --mb MB の塊。測るのは塊 1 つの復号の最短時間）:
    one_byte   1 バイトのトークンだけ（TOGGLE。間隔 1〜6）
    mixed      尾の付くトークン（TOGGLE_LONG・VALUE・WAIT）が混ざる（符号器で作る）
    repeat     REPEAT を含む（半周期が揃ったクロック + 乱数の信号）
測る関数:
    decode_*      decode_tokens（変化点を作る）
    extent_1t/_mt  token_extent（時刻の進みだけ。受け取りのループ用）
    decode_1t / _mt  decode_tokens（threads=1 / 拍の境で切って複数スレッド。_mt はスレッドを持つ版のみ）
出す数: M トークン/s（NOP・REPEAT を除くトークン。REPEAT で繰り返した分も数える）、M 変化点/s、MB/s。
"""

import argparse
import inspect
import os
import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, os.environ.get("FPROBE_HOST") or str(Path(__file__).resolve().parents[2] / "host"))

from fprobe import la_tokens as T  # noqa: E402


def _tile(piece: bytes, nbytes: int) -> bytes:
    """先頭が VALUE の piece の、最初の 8 拍（VALUE を含む）を除いた部分を繰り返して nbytes にする（流れの続きの塊。束は拍をまたがない）。"""
    body = piece[T.BEAT_BYTES * 8:]
    reps = max(1, (nbytes - len(piece)) // len(body))
    return body * (reps + 1)


def make_one_byte(nbytes: int) -> bytes:
    rng = np.random.default_rng(1)
    b = (rng.integers(0, 16, nbytes, dtype=np.uint8) << 3) | rng.integers(1, 7, nbytes, dtype=np.uint8)
    return b.tobytes()                                       # 流れの続きの塊（VALUE も NOP も尾も無い）


def make_mixed(nbytes: int) -> bytes:
    rng = random.Random(2)
    v, samples = 0, []
    for _ in range(200_000):
        r = rng.random()
        gap = 1 + (rng.randrange(6) if r < 0.7 else rng.randrange(70) if r < 0.9 else rng.randrange(8000) if r < 0.98
                   else rng.randrange(1 << 16))
        samples.extend([v] * min(gap, 5000))
        v ^= (1 << rng.randrange(16)) if rng.random() < 0.97 else rng.randrange(1 << 16)
        samples.append(v)
    return _tile(T.encode_tokens(samples), nbytes)


def make_repeat(nbytes: int) -> bytes:
    rng = random.Random(3)
    v, samples = 0, []
    for _ in range(4_000):
        if rng.random() < 0.5:
            samples.extend(([v] * 4 + [v ^ 1] * 5) * rng.randrange(20, 200))
        else:
            for _ in range(rng.randrange(20, 100)):
                samples.extend([v] * rng.randrange(1, 9))
                v ^= 1 << rng.randrange(16)
    return _tile(T.encode_tokens(samples, repeat=True), nbytes)


def best(fn, repeats: int) -> float:
    t = []
    for _ in range(repeats):
        s = time.perf_counter()
        fn()
        t.append(time.perf_counter() - s)
    return min(t)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=float, default=3.0)
    ap.add_argument("--repeats", type=int, default=7)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-c", action="store_true", help="C 版（tools/build_la_tokens_c.py で作る）があっても使わず、numpy の版を測る")
    a = ap.parse_args()
    if a.no_c and hasattr(T, "_C"):
        T._C = None
    n = int(a.mb * 1_000_000) // 16 * 16
    c = "あり" if getattr(T, "c_available", lambda: False)() else "なし"
    print(f"塊 約 {n} バイト、最短 {a.repeats} 回、numpy {np.__version__}、C 版 {c}")
    has_threads = "threads" in inspect.signature(T.decode_tokens).parameters   # 直す前の版には無い
    prev = 0x1234                                                  # 流れの続きの塊として復号する
    for name, make in (("one_byte", make_one_byte), ("mixed", make_mixed), ("repeat", make_repeat)):
        data = make(n)
        d = T.decode_tokens(data, 0, prev)
        tokens, changes = d.tokens, len(d.values)
        if has_threads:
            rows = [("decode_1t", lambda: T.decode_tokens(data, 0, prev, threads=1)),
                    ("decode_mt", lambda: T.decode_tokens(data, 0, prev, threads=a.threads)),
                    ("extent_1t", lambda: T.token_extent(data, 0, prev, threads=1)),
                    ("extent_mt", lambda: T.token_extent(data, 0, prev, threads=a.threads))]
        else:
            rows = [("decode_1t", lambda: T.decode_tokens(data, 0, prev)), ("extent_1t", lambda: T.token_extent(data, 0, prev))]
        for fname, fn in rows:
            sec = best(fn, a.repeats)
            print(f"{name:9s} {fname:10s} {tokens / sec / 1e6:7.1f} Mトークン/s  {changes / sec / 1e6:7.1f} M変化点/s  "
                  f"{len(data) / sec / 1e6:7.1f} MB/s  (bytes {len(data)}, tokens {tokens}, changes {changes}, {sec * 1000:.1f} ms)")


if __name__ == "__main__":
    main()
