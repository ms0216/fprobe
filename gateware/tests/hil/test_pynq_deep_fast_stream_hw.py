"""PYNQ-Z2 の fprobe_pynq_deep_fast（端子の ISERDESE2 で 1 周期に 8 サンプル = 800 MS/s を、深いバッファの垂れ流しに N 並列で符号にする）:
垂れ流しでも 1.25 ns の格子のまま、欠けなく、長く流せることをジャンパ線のループバックで確かめる。
設計 docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §9。

準備: `python -m fprobe_gw build --build-dir C:/fw65 fprobe_pynq_deep_fast` のビットストリームを PYNQ に書いておく（このテストは書かない）。
**RPi ヘッダの 32 番（GPIO12、試験用の方形波）→ PMOD JB の 1 番（ch 10 = in0）、GND: RPi ヘッダ 34 番 ↔ JB の 5 番**。
環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行する。FPROBE_PYNQ_TESTGEN_CH で受ける ch を変えられる。
FPROBE_DEEP_FAST_SECONDS で長い流しの秒数（既定 10）。つながっていなければ skip する。

方形波は testgen_clock_hz（400 MHz）で刻むので、周期・high の 1 単位 = 細かいサンプル 2 個。確かめること:
  1. RLE（16 ch・800 MS/s）: ch の変化の位置が細かいサンプルの格子に揃い（1 の長さ・0 の長さが、流れ全体でそれぞれ隣り合う 2 通り以内）、
     立ち上がりの辺の位相（位置 mod 2 × 周期）が流れ全体で隣り合う 2 値以内 = 欠けも重複も無い（欠ければその後の辺が全部ずれる）。
     辺がサンプルの境目の近くにあると 1 サンプル揺れる（アナログ。隣の 1 周期が − 1・+ 1 の組になる）ので、1 周期は 2 × 周期 ± 1 まで
     許し、揺れた回数を print する。1 の長さに奇数が出る（400 MHz より細かい直接の証拠）。
     溢れ（overrun）なし。リング（DDR）と帯域の使用量（usage）を print する（判定には使わない）。
  2. RAW（1 ch = 受ける ch だけ、first_channel）: 400 MS/s（50 MB/s = 申告の持続できる速さちょうど）は長さが 2.5 ns の格子で欠けなし。
     800 MS/s（100 MB/s）は申告を超えるので、指定しなければ始める前に断る（ValueError）。上限を明示して短く流すと、リングが溜まる間は
     欠けなし、溢れたらその事実（overrun）が立ち、届いた分は先頭から正しい。
限界の数（1 の長さ − 設定、細いパルス）は print に出す（fprobe_pynq_fast の内蔵バッファの記録 la-resolution §12 と並べる）。"""

import os
import queue
import threading
import time
from collections import Counter

import numpy as np
import pytest

from fprobe.la import DeepAnalyzer, LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]
CH = int(os.environ.get("FPROBE_PYNQ_TESTGEN_CH", "10"))
SECONDS = float(os.environ.get("FPROBE_DEEP_FAST_SECONDS", "10"))
TOLERANCE = 4                       # 1 の長さ − 設定の許容（細かいサンプル。アナログの差。fprobe 本体は既定を持たない。この試験が明示する）


@pytest.fixture(scope="module")
def board():
    with Session.open_eth(HOST) as s:
        caps = s.device.capabilities()
        if caps.design != "fprobe_pynq_deep_fast":
            pytest.skip("fprobe_pynq_deep_fast が載っていない")
        gen, deep = LogicAnalyzer(s.device), DeepAnalyzer(s.device)
        assert deep.lanes == 8 and caps.la_sample_hz == 800_000_000 and caps.la_fine >> CH & 1
        print(f"\nN {deep.lanes}、{caps.la_sample_hz / 1e6:.0f} MS/s、方形波 {gen.testgen_clock_hz / 1e6:.0f} MHz、受ける ch {CH}、"
              f"申告の持続できる速さ {caps.la_stream_bytes_per_s / 1e6:.0f} MB/s")
        gen.testgen(period=300, high=150, edge=1)
        time.sleep(0.02)
        s_ = deep.stream(rle=True)
        seen = set()
        for chunk in s_.chunks(0.05):
            seen |= {v >> CH & 1 for v in chunk.transitions()[0]}
        if len(seen) < 2:
            gen.testgen_off()
            pytest.skip(f"RPi ヘッダ 32 番と ch {CH} の入力が、ジャンパ線でつながっていない（ch {CH} が動かない）")
        yield gen, deep
        gen.testgen_off()


class Runs:
    """ch 1 本の (値, 長さ) の度数と、隣り合う 2 区間の和（1 周期）の度数。最初の区間（流れの始めで途中から）と、まだ終わらない最後の
    区間は数えない。drop_tail は終わった区間のうち最後のいくつかも数えない（溢れて止まった流れの終わり）。
    区間 1 つずつを Python の値にすると 1 秒に数百万の変化（周期 150 の RLE・RAW 400 MS/s）で受け取りが遅れて溢れる（板ではなく試験の
    数え方の限界）ので、かたまりごとに numpy で度数にまとめる。"""

    def __init__(self, drop_tail=0, period=None):
        self.lengths, self.cycles, self.rise_phase = Counter(), Counter(), Counter()   # rise_phase: 1 の区間の開始 mod period
        self._period = period
        self._prev_n = None                     # 数えた最後の区間の長さ（次の区間と 1 周期を作る）
        self._held = (np.empty(0, np.int64),) * 3      # 終わったが drop_tail のためにまだ数えない区間（値, 長さ, 開始）
        self._drop_tail, self._skip_first = drop_tail, True

    def add(self, v, n, s):
        v, n, s = (np.concatenate((held, x)) for held, x in zip(self._held, (v, n, s)))
        keep = max(len(n) - self._drop_tail, 0)
        self._held = (v[keep:], n[keep:], s[keep:])
        v, n, s = v[:keep], n[:keep], s[:keep]
        if self._skip_first and len(n):
            v, n, s, self._skip_first = v[1:], n[1:], s[1:], False
        if not len(n):
            return
        if self._period:
            counts = np.bincount(s[v == 1] % self._period)
            for key in np.flatnonzero(counts):
                self.rise_phase[int(key)] += int(counts[key])
        counts = np.bincount(n * 2 + v)                 # 長さは小さい整数なので、並べ替え（unique）より数えるほうが速い
        for key in np.flatnonzero(counts):
            self.lengths[(int(key) & 1, int(key) >> 1)] += int(counts[key])
        both = n if self._prev_n is None else np.concatenate(([self._prev_n], n))
        counts = np.bincount(both[1:] + both[:-1])
        for key in np.flatnonzero(counts):
            self.cycles[int(key)] += int(counts[key])
        self._prev_n = int(n[-1])

    def of(self, value):
        return {n: c for (v, n), c in self.lengths.items() if v == value}


def bit_runs(stream, seconds, bit, usage_every_s=1.0, drop_tail=0, period=None):
    """流れ全体の ch bit の Runs と、usage の並び。受け取り（このスレッド）と数え（別のスレッド。numpy の重い所は GIL を手放す）を
    分ける: 1 本では RAW 1 ch 400 MS/s（50 MB/s）の変化点と度数の数えが約 43 MB/s で、受け取りが遅れて溢れる。"""
    runs, chunks, failed = Runs(drop_tail, period), queue.Queue(), []

    def count():
        last_v = last_s = None
        try:
            while (chunk := chunks.get()) is not None:
                values, starts = chunk.transitions()
                if not len(values):
                    continue
                b = (np.frombuffer(values, dtype=np.uint16) >> bit) & 1      # 写さずに見る（array の値は uint16、位置は uint64）
                s = np.frombuffer(starts, dtype=np.int64)
                keep = np.empty(len(b), dtype=bool)
                keep[0] = last_v is None or b[0] != last_v
                keep[1:] = b[1:] != b[:-1]
                b, s = b[keep], s[keep]
                if len(b):
                    if last_v is not None:          # 前のかたまりから続く区間が、このかたまりの最初の変化で終わる
                        b, s = np.concatenate(([last_v], b)), np.concatenate(([last_s], s))
                    runs.add(b[:-1].astype(np.int64), np.diff(s), s[:-1])
                    last_v, last_s = int(b[-1]), int(s[-1])
        except BaseException as e:                  # 数えの誤りは受け取りの後に投げ直す（黙って捨てない）
            failed.append(e)
            while chunks.get() is not None:
                pass

    worker = threading.Thread(target=count)
    worker.start()
    last_u = time.monotonic()
    try:
        for chunk in stream.chunks(seconds):
            chunks.put(chunk)
            if time.monotonic() - last_u >= usage_every_s:
                stream.usage()
                last_u = time.monotonic()
    finally:
        chunks.put(None)
        worker.join()
    if failed:
        raise failed[0]
    stream.usage()
    return runs, stream.usage_history


def check_square(runs, period_samples, high_samples, label):
    highs, lows = runs.of(1), runs.of(0)
    assert sum(highs.values()) > 100, f"{label}: 変化が少ない（{sum(highs.values())}）"
    p = period_samples
    phases = sorted(runs.rise_phase)
    assert len(phases) <= 2 and (len(phases) < 2 or (phases[1] - phases[0]) % p in (1, p - 1)),         f"{label}: 立ち上がりの位相が {phases[:8]}（mod {p}。隣り合う 2 値以内のはず）= 欠けか重複"
    assert set(runs.cycles) <= {p - 1, p, p + 1}, f"{label}: 1 周期が {sorted(runs.cycles)[:8]}（{p} ± 1 のはず）= 欠けか重複"
    assert max(highs) - min(highs) <= 1 and max(lows) - min(lows) <= 1, f"{label}: 長さが格子に揃わない"
    assert abs(min(highs) - high_samples) <= TOLERANCE
    print(f"{label}: 周期 {sum(highs.values())} 回、1 の長さ {sorted(highs)}（設定 {high_samples}、差 {min(highs) - high_samples:+d}）、"
          f"0 の長さ {sorted(lows)}、辺が 1 サンプル揺れた {runs.cycles[p - 1]} 回、立ち上がりの位相 {dict(runs.rise_phase)}")
    return highs


def print_usage(usage, label):
    last = usage[-1]
    peak = max((u["ring_peak_beats"] or 0) for u in usage)
    rates = [u["encoded_bytes_per_s"] for u in usage if u["encoded_bytes_per_s"] is not None]
    print(f"{label}: リング {last['ring_beats']} 拍のうち最大 {peak} 拍が未読、符号 {last['encoded_bytes'] / 1e6:.1f} MB"
          f"（毎秒 {min(rates, default=0) / 1e6:.1f}〜{max(rates, default=0) / 1e6:.1f} MB/s）、受け取り "
          f"{last['received_bytes'] / 1e6:.1f} MB、overrun {last['overrun']}")


@pytest.mark.parametrize("period", [300, 150])
def test_rle_streams_every_change_on_the_1_25_ns_grid(board, period):
    gen, deep = board
    unit = deep.clock_hz // gen.testgen_clock_hz
    gen.testgen(period=period, high=period // 2, edge=1)
    time.sleep(0.02)
    stream = deep.stream(rle=True)
    runs, usage = bit_runs(stream, SECONDS, CH, period=unit * period)
    print_usage(usage, f"RLE 周期 {period}")
    assert not stream.stats().overrun
    check_square(runs, unit * period, unit * (period // 2), f"RLE 周期 {period}")
    assert stream.stats().duration_s >= SECONDS * 0.9              # 奇数の長さ（細かさの直接の証拠）は細いパルスの試験で


def test_rle_thin_pulses_show_odd_lengths(board):
    # 細いパルス（周期 40、1 の幅 3〜6 単位 = 7.5〜15 ns）: 1 の長さに奇数（400 MHz の刻みより細かい）が出る
    gen, deep = board
    odd = False
    for high in (3, 4, 5, 6):
        gen.testgen(period=40, high=high, edge=1)
        time.sleep(0.02)
        stream = deep.stream(rle=True)
        runs, _ = bit_runs(stream, 0.5, CH)
        assert not stream.stats().overrun
        highs = sorted(runs.of(1))
        print(f"細いパルス {high * 2.5:.1f} ns: 1 の長さ {highs}")
        odd |= any(n % 2 for n in highs)
    assert odd, "1 の長さがどれも偶数（400 MHz の刻みより細かく取れていない?）"


def test_raw_one_channel_at_400_msps_and_the_800_msps_refusal(board):
    gen, deep = board
    gen.testgen(period=60, high=30, edge=1)
    time.sleep(0.02)
    stream = deep.stream(pack_channels=1, first_channel=CH, rate_hz=400e6)      # 50 MB/s
    runs, usage = bit_runs(stream, SECONDS, 0, period=60)
    print_usage(usage, "RAW 1 ch 400 MS/s")
    assert not stream.stats().overrun
    check_square(runs, 60, 30, "RAW 1 ch 400 MS/s（1 サンプル = 2.5 ns）")
    with pytest.raises(ValueError, match="MB/s"):
        deep.stream(pack_channels=1, first_channel=CH)                           # 800 MS/s = 100 MB/s は断る（始めない）
    # 上限を明示して 800 MS/s を短く: リングが溜まる間は欠けなし。溢れたら overrun が立つ（事実。届いた分は先頭から正しい）
    stream = deep.stream(pack_channels=1, first_channel=CH, max_bytes_per_s=100e6)
    runs, usage = bit_runs(stream, 1.0, 0, usage_every_s=0.1, drop_tail=2, period=120)
    print_usage(usage, "RAW 1 ch 800 MS/s（上限を明示）")
    check_square(runs, 120, 60, "RAW 1 ch 800 MS/s")
    print(f"RAW 1 ch 800 MS/s: overrun {stream.stats().overrun}、{stream.stats().duration_s * 1e3:.1f} ms 分")
