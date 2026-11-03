"""記録の VCD の書き出しの試験の場面（test_recording_vcd_golden が使う。期待のハッシュは、一括の書き出しにする前の
変化点ごとの実装（2026-11-03 の 2692a8b6）で書いた VCD のもの）。

どの場面も決まった乱数で作る（同じ場面は同じ記録）。make(directory, name) が記録を作り、export_vcd の引数を返す。
"""

import random
from array import array

from fprobe.la import Transitions
from fprobe.recording import Recorder

SIGNALS = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1}, {"name": "count", "lsb": 8, "width": 8}],
                      "1": [{"name": "state", "lsb": 0, "width": 3}]}}


def _wave(rng: random.Random, transitions: int, channels: int, gap: int, period_s: float, skew=()) -> Transitions:
    values, starts, t, previous = array("H"), array("Q"), 0, None
    for _ in range(transitions):
        v = rng.randrange(1 << channels)
        if v == previous:
            v ^= 1 << rng.randrange(channels)
        values.append(v)
        starts.append(t)
        previous = v
        t += rng.randrange(1, gap + 1)
    return Transitions(values, starts, t + 1, 0, 0, period_s, channels, tuple(skew))


def _fpga(rng: random.Random, rec: Recorder, span: int, count: int) -> None:
    for _ in range(count):
        t = rng.randrange(span)
        r = rng.random()
        if r < 0.85:
            rec.add("fpga", "value", t, a=rng.choice([0, 1, 7]), b=rng.randrange(1 << 16))
        elif r < 0.92:
            rec.add("fpga", "group", t, a=rng.choice([0, 1]))
        elif r < 0.97:
            rec.add("fpga", "gap", t, a=0)
        else:
            rec.add("fpga", "value_untimed", t, a=0, b=rng.randrange(1 << 16))


SCENARIOS = {
    # 名前: (タイムベースの Hz, かたまりの並び [(開始の刻み, 変化点, ch, 変化の最大の間隔, 1 サンプルの秒, ずれ)], FPGA の出来事, channels)
    "plain": (1e8, [(0, 400, 4, 9, 1e-8, ()), (5000, 300, 4, 9, 1e-8, ())], 0, None),
    "skew_across_chunks": (1e8, [(0, 300, 6, 5, 1e-8, (0.0, 2.5, None, -1.0, 0.25, 2.5)),
                                 (1200, 300, 6, 5, 1e-8, (0.0, 2.5, None, -1.0, 0.25, 2.5)),
                                 (2400, 50, 6, 5, 1e-8, (0.0, 2.5, None, -1.0, 0.25, 2.5))], 0, None),
    "subset_unsorted": (1e8, [(0, 500, 8, 4, 1e-8, ()), (2100, 500, 8, 4, 1e-8, ())], 0, [5, 1, 6, 1]),
    "fpga_mixed": (1e8, [(0, 600, 3, 7, 2e-8, (0.0, 1.0, 0.0)), (8000, 600, 3, 7, 2e-8, (0.0, 1.0, 0.0))], 1500, None),
    "fpga_only": (1e8, [], 400, None),
    "rounded_unit": (3e8, [(7, 700, 2, 3, 1 / 7e8, ()), (2000, 700, 2, 3, 1 / 7e8, ())], 300, None),
    "negative_start": (1e8, [(-900, 200, 2, 6, 1e-8, (0.5, 0.0))], 100, None),
    "sixteen_square": (1e8, [(k * 4000, 1500, 16, 1, 1e-8, ()) for k in range(3)], 0, None),
    "big_times": (1e9, [(10 ** 15, 300, 4, 3, 1e-9, (0.0, 0.5)), (10 ** 15 + 2000, 300, 4, 3, 1e-9, (0.0, 0.5))],
                  200, None),
}


def make(directory, name: str):
    """場面の記録を directory に作る。export_vcd の channels を返す。"""
    hz, chunks, fpga, channels = SCENARIOS[name]
    rng = random.Random(name)
    rec = Recorder(directory, timebase_hz=hz, meta={"fpga.signals": SIGNALS})
    span = 1
    for start, transitions, ch, gap, period_s, skew in chunks:
        tr = _wave(rng, transitions, ch, gap, period_s, skew)
        rec.add_waveform(tr, start)
        span = max(span, start + round(tr.length * period_s * hz))
    if fpga:
        lo = min((c[0] for c in chunks), default=0)
        _fpga(rng, rec, span, fpga)
        rec.add("fpga", "value", lo, a=0, b=1)                        # 波形の最初の変化と同じ時刻
    rec.close()
    return channels
