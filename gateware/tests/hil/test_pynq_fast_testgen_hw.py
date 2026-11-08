"""PYNQ-Z2 の fprobe_pynq_fast（端子を ISERDESE2 で取る。段 1 = 600 MS/s・N 6、段 2 = 800 MS/s・N 8）: 細かい時間分解能を、
ジャンパ線のループバックで実機で確かめる。N・方形波を刻むクロックはケイパビリティから読む（決め打ちしない）。

準備: `python -m fprobe_gw build fprobe_pynq_fast` で作ったビットストリームを PYNQ に書いておく（このテストは書かない）。
**RPi ヘッダの 32 番（GPIO12、試験用の方形波）とロジアナの入力がつながっていること**。既定は PMOD JB の 1 番（ch 10 = in0。
GND の 2 本目: RPi ヘッダ 34 番 ↔ JB の 5 番）。環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行する。
FPROBE_PYNQ_TESTGEN_CH で受ける ch を変えられる（段 2 で ISERDESE2 に載せた PMOD JA の ch 0〜7 を確かめるとき。例: 4 = JA7 = 予備
（基板の引き下げあり）。JA の線にターゲットをつないだままにしない: 方形波の線と、ターゲットの出力がぶつかる）。
つながっていなければ skip する。

方形波は testgen_clock_hz（段 1: 300 MHz、段 2: 400 MHz）で刻むので、周期・high の 1 単位 = 細かいサンプル PER_UNIT 個
（la_sample_hz / testgen_clock_hz = 2。DDR で取るので、どの N でも 2）。確かめること（2026-11-01 の段 1 の実機の事実から）:
  1. 遅い方形波で、立ち上がりの間隔が PER_UNIT × 周期 ± 1（欠けも重複も無い）、1 の長さは 1 回の取り込みの中で隣り合う 2 通り以内、
     設定からのずれが TOLERANCE 以内。ずれの値そのもの（段 1 で High が +1 細かいサンプル長い = 線のアナログの差）は print する。
  2. 1 回の取り込みの中で、変化の位置が細かいサンプルの格子に揃い（周期の余りが 1 通り）、1 の長さに奇数が出る（testgen_clock_hz の
     周期 = PER_UNIT サンプルより細かく取れていることの直接の証拠）。取り込みのたびに方形波の位相は変わるので、取り込みをまたいで位置は
     比べない。
限界の全体（細いパルス）は print に出す（アナログの帯域は ISERDES では変わらないはず、の比べ。段 1・fprobe_pynq_cap の記録と並べる）。
"""

import os
import time

import pytest

from fprobe.la import LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]
TOLERANCE = 4                                     # 長さのずれの許容（細かいサンプル数。実機のアナログの差: 600 MS/s で +1、800 MS/s で +2〜+3 = 2.5〜3.75 ns、と境の 1）。fprobe 本体は既定を持たない。この試験が明示する
CH = int(os.environ.get("FPROBE_PYNQ_TESTGEN_CH", "10"))   # 方形波を受ける ch（既定 = PMOD JB の 1 番 = la_in[0]）


@pytest.fixture(scope="module")
def la():
    with Session.open_eth(HOST, link_up_wait_s=8) as s:
        analyzer = LogicAnalyzer(s.device)
        caps = s.device.capabilities()
        if caps.design != "fprobe_pynq_fast":
            pytest.skip("fprobe_pynq_fast が載っていない")
        assert analyzer.lanes in (6, 8) and caps.la_sample_hz == caps.sys_clock_hz * analyzer.lanes
        assert caps.la_fine >> CH & 1, f"ch {CH} は細かく取る ch ではない（LA_FINE {caps.la_fine:#x}）"
        assert caps.la_sample_hz % analyzer.testgen_clock_hz == 0
        print(f"\nN {analyzer.lanes}、{caps.la_sample_hz / 1e6:.0f} MS/s、方形波 {analyzer.testgen_clock_hz / 1e6:.0f} MHz、"
              f"細かい ch {caps.la_fine:#06x}、受ける ch {CH}")
        analyzer.testgen(period=300, high=150, edge=1)
        time.sleep(0.02)
        if len(set(analyzer.capture(pretrigger=64).channel(CH))) < 2:
            analyzer.testgen_off()
            pytest.skip(f"RPi ヘッダ 32 番と ch {CH} の入力が、ジャンパ線でつながっていない（ch {CH} が動かない）")
        yield analyzer
        analyzer.testgen_off()


def per_unit(la) -> int:
    """方形波の 1 単位（testgen_clock_hz の 1 周期）の細かいサンプルの数。"""
    return la.clock_hz // la.testgen_clock_hz


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


def capture(la, period, high, edge, **trigger):
    la.testgen(period=period, high=high, edge=edge)
    time.sleep(0.02)
    return la.capture(pretrigger=64, **trigger)


@pytest.mark.parametrize("period", [300, 60, 30, 20])
def test_slow_square_waves_have_no_loss_and_a_constant_high_length(la, period):
    # 隣り合う 1 と 0 の合計は周期ちょうど（欠けも重複も無い）。1 の長さは取り込みの中で（ほぼ）一定で、設定からのずれは線のアナログの性質
    # （立ち上がりと立ち下がりの遅れの差。段 1 の実機で +1 細かいサンプル、毎回同じ）。ずれそのものは事実として print し、
    # 本体は補正しない（判断は使う側）。ここでは「1 の長さが隣り合う 2 通り以内」と「ずれが TOLERANCE 以内」を確かめる
    unit = per_unit(la)
    bits = [int(b) for b in capture(la, period, period // 2, edge=1).channel(CH)]
    r = runs(bits)
    assert len(r) > 20
    highs = sorted({n for v, n in r if v == 1})
    lows = sorted({n for v, n in r if v == 0})
    print(f"\nperiod {period}: 1 の長さ {highs}（設定 {unit * (period // 2)}）、0 の長さ {lows}、"
          f"1 の長さ − 設定 {[n - unit * (period // 2) for n in highs]}（細かいサンプル）")
    # 欠けなし: 立ち上がりの間隔は周期 ± 1（辺の位置が境で 1 揺れる）、最初から最後までの合計は周期の整数倍 ± 1
    rises = [i for i in range(1, len(bits)) if bits[i] and not bits[i - 1]]
    gaps = {b2 - b1 for b1, b2 in zip(rises, rises[1:])}
    assert gaps <= {unit * period - 1, unit * period, unit * period + 1}, gaps
    assert abs((rises[-1] - rises[0]) - (len(rises) - 1) * unit * period) <= 1
    assert highs[-1] - highs[0] <= 1, highs
    assert all(abs(n - unit * (period // 2)) <= TOLERANCE for n in highs), highs


def test_edges_land_on_one_fine_sample_grid_and_odd_lengths_prove_the_resolution(la):
    # 1 回の取り込みの中では、方形波の変化は毎回同じ細かいサンプルの位置に来る（周期の余りが 1 通り。揺れが 1 細かいサンプルより小さい）。
    # 1 の長さに奇数が出ることは、取り込みが testgen_clock_hz（PER_UNIT = 2 細かいサンプル）より細かいことの直接の証拠
    # （testgen_clock_hz で取るなら長さは必ず PER_UNIT の倍数）。取り込みのたびに方形波の位相は変わるので、取り込みどうしの位置は比べない
    unit = per_unit(la)
    bits = [int(b) for b in capture(la, 30, 15, 0).channel(CH)]
    period = unit * 30
    rises = [i for i in range(1, len(bits)) if bits[i] and not bits[i - 1]]
    falls = [i for i in range(1, len(bits)) if not bits[i] and bits[i - 1]]
    assert len(rises) > 20 and len(falls) > 20
    rise_phase, fall_phase = {i % period for i in rises}, {i % period for i in falls}
    lengths = {f - max(x for x in rises if x < f) for f in falls if any(x < f for x in rises)}
    print(f"\n立ち上がりの位置 {sorted(rise_phase)}、立ち下がり {sorted(fall_phase)}（周期 {period} の余り）、1 の長さ {sorted(lengths)}、"
          f"立ち上がり {len(rises)} 回")
    assert len(rise_phase) == 1 and len(fall_phase) <= 2
    # 1 の長さは取り込みごとに 1 細かいサンプル揺れうる（2026-11-01 の 800 MS/s で 32 と 33）。PER_UNIT の倍数でない長さが、
    # 何回かの取り込みのどれかで出れば、testgen_clock_hz より細かく取れている証拠になる
    seen = set(lengths)
    # 800 MS/s の実機（2026-11-01）では、半周期の方形波の 1 の長さはアナログの差（+2）で偶数のままだった。細いパルス（幅 3〜6 単位、
    # edge 0・1）では 3・7・11・13 の奇数の長さが出た（パルスは立ち上がりと立ち下がりの差の効き方が違う）。それも足して確かめる
    for k, (width, edge) in enumerate([(w, e) for w in (3, 4, 5, 6) for e in (0, 1)]):
        if any(n % unit for n in seen):
            break
        b = [int(x) for x in capture(la, 40, width, edge).channel(CH)]
        r = [i for i in range(1, len(b)) if b[i] and not b[i - 1]]
        seen |= {i - max(x for x in r if x < i) for i in range(1, len(b)) if not b[i] and b[i - 1] and any(x < i for x in r)}
    print(f"何回かの取り込みで見えた 1 の長さ {sorted(seen)}")
    assert any(n % unit for n in seen), seen


def test_print_the_whole_picture(la):
    unit = per_unit(la)
    unit_ns = 1e9 / la.testgen_clock_hz
    print()
    for edge in (0, 1):
        for width in (1, 2, 3, 4, 5, 6):
            r = runs([int(b) for b in capture(la, 40, width, edge).channel(CH)])
            highs = [n for v, n in r if v == 1]
            print(f"edge={edge} 1 の幅 {width} 単位（{width * unit_ns:.2f} ns = 細かいサンプル {unit * width} 個）: "
                  f"見えた {len(highs)} 個、長さ {sorted(set(highs))}")
