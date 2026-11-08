"""PYNQ-Z2 の fprobe_pynq_cap: 外部ピン入力の限界を、ジャンパ線のループバックで実機で確かめる。

準備: `python -m fprobe_gw build fprobe_pynq_cap` で作ったビットストリーム（試験用の方形波の回路を含む版）を PYNQ に書いておく
（このテストは書かない）。**RPi ヘッダの 32 番（GPIO12）と PMOD JB の 1 番（ロジアナの ch 10 = in0）を、ジャンパ線でつなぐ**
（推奨: 34 番の GND と JB の 5 番の GND も、並べて短く）。docs/hardware-wiring-request.md 3 章。
環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行する。つながっていなければ skip する。

確かめること（2026-10-31 の実測。ループバック全体 = 出力の駆動・線・入力のバッファの限界で、取り込み回路だけの限界ではない）:
  1. 遅い方形波（周期 20 以上 = 15 MHz 以下）を、立ち下がりで変える側（edge = 1）で取ると、1 と 0 の連続の長さが設定どおり。
  2. 1 の幅が 4 サンプル（13.3 ns）以上のパルスは、すべて見えて、幅が設定どおり（edge = 1）。
  限界の全体（幅 1〜8、edge 0・1、高い周波数）は print に出す。
"""

import os
import time

import pytest

from fprobe.la import LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]
TOLERANCE = 1                                     # 幅のずれの許容（サンプル数）。fprobe 本体は既定を持たない。この試験が明示する
IN0 = 10                                           # PMOD JB の 1 番 = la_in[0]


@pytest.fixture(scope="module")
def la():
    with Session.open_eth(HOST, link_up_wait_s=8) as s:
        analyzer = LogicAnalyzer(s.device)
        if s.device.capabilities().design != "fprobe_pynq_cap":
            pytest.skip("fprobe_pynq_cap（試験用の方形波入り）が載っていない")
        analyzer.testgen(period=300, high=150, edge=1)
        time.sleep(0.02)
        if len(set(analyzer.capture(rate_hz=None, pretrigger=64).channel(IN0))) < 2:
            analyzer.testgen_off()
            pytest.skip("RPi ヘッダ 32 番と PMOD JB の 1 番が、ジャンパ線でつながっていない（in0 が動かない）")
        yield analyzer
        analyzer.testgen_off()


def runs(bits):
    out, n = [], 1
    for a, b in zip(bits, bits[1:]):
        if a == b:
            n += 1
        else:
            out.append((a, n))
            n = 1
    out.append((bits[-1], n))
    return out[1:-1]                                   # 最初と最後は途中から


def capture_runs(la, period, high, edge):
    la.testgen(period=period, high=high, edge=edge)
    time.sleep(0.02)
    return runs([int(b) for b in la.capture(rate_hz=None, pretrigger=64).channel(IN0)])


@pytest.mark.parametrize("period", [300, 60, 30, 20])
def test_slow_square_waves_are_reproduced_exactly(la, period):
    r = capture_runs(la, period, period // 2, edge=1)
    assert len(r) > 20
    # 欠けも重複も無い（隣り合う 1 と 0 の合計が周期と一致）。立ち上がりと立ち下がりの入力遅延の差で、1 の区間が +1、0 の区間が -1 サンプル
    # ずれうる（ビルドの配置ごとに違う。2026-11-01 の fw49c・fw51 で実測）。許容は ±1 サンプルで、この試験が明示する
    assert all(abs(n - period // 2) <= TOLERANCE for _, n in r), sorted({(v, n) for v, n in r})
    assert all(a[1] + b[1] == period for a, b in zip(r[1::2], r[2::2]) if a[0] != b[0]), sorted({(v, n) for v, n in r})


@pytest.mark.parametrize("width", [4, 5, 6, 8])
def test_high_pulses_of_four_samples_or_more_are_seen_with_the_exact_width(la, width):
    r = capture_runs(la, 40, width, edge=1)
    highs = [n for v, n in r if v == 1]
    assert len(highs) >= 150 and all(abs(n - width) <= TOLERANCE for n in highs), sorted(set(highs))


def test_print_the_whole_picture(la):
    print()
    for edge in (0, 1):
        for width in (1, 2, 3, 4):
            r = capture_runs(la, 40, width, edge)
            highs = [n for v, n in r if v == 1]
            print(f"edge={edge} 1 の幅 {width} サンプル（{width * 10 / 3:.1f} ns）: 見えた {len(highs)} 個、長さ {sorted(set(highs))}")
