"""PYNQ-Z2 の fprobe_pynq_deep: 垂れ流し（16 ch RAW）に、試験用の方形波（la_testgen）が実際に流れてくるのを確かめる。
仕様: docs/superpowers/specs/2026-11-01-la-testgen-deep-design.md 4.3

準備: `python -m fprobe_gw build fprobe_pynq_deep` で作った、ケイパビリティ la_testgen のあるビットストリームを PYNQ に書いておく
（このテストは書かない）。**RPi ヘッダの 32 番（GPIO12）と PMOD JB の 1 番（ロジアナの ch 10 = in0）を、ジャンパ線でつなぐ**
（推奨: 34 番の GND と JB の 5 番の GND も、並べて短く。配線の指示には電源順・電圧範囲・逆流の注意を添える）。
環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行する。ケイパビリティが無い・つながっていなければ skip する。

この試験は実機で実行していない（書いただけ）。許容する幅はこの試験が明示する（fprobe 本体は既定を持たない）。

方形波は sys（100 MHz）の周期の数で出る。サンプル周期が sys の SAMPLE_CYCLES 周期のとき、period と high が SAMPLE_CYCLES の整数倍なら、
1 と 0 の連続の長さは、サンプルされる位相によらず、high / SAMPLE_CYCLES 個と (period − high) / SAMPLE_CYCLES 個になる。
"""

import os
import time

import pytest

from fprobe import generated as g
from fprobe.la import DeepAnalyzer, LogicAnalyzer
from fprobe.session import Session

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]
IN0 = 10                                           # PMOD JB の 1 番 = la_in[0]
SYS_HZ = 100_000_000
RATE_HZ = 10e6                                     # 10 MS/s（RAW 16 ch で 20 MB/s。TCP で欠けなく流れる範囲）
SAMPLE_CYCLES = round(SYS_HZ / RATE_HZ)            # 10（サンプル周期 = sys の 10 周期 = 100 ns）
DURATION_S = 2.0
COUNT_TOLERANCE = 0.02                             # サンプル数がレート × 時間と一致する許容（2 %。この試験が決める値）


@pytest.fixture(scope="module")
def session():
    with Session.open_eth(HOST, link_up_wait_s=8) as s:
        if not getattr(s.device.capabilities(), "la_testgen", False):
            pytest.skip("この PYNQ の回路にケイパビリティ la_testgen が無い（試験用の方形波入りの fprobe_pynq_deep を入れる）")
        yield s


def stream_samples(session, seconds=DURATION_S):
    """16 ch RAW を seconds 秒流して、(全サンプル, 統計, 落ちたフレーム数)。"""
    la = DeepAnalyzer(session.device)
    lost0 = session.link.lost_frames
    stream = la.stream(rate_hz=RATE_HZ, rle=False)
    samples = []
    for chunk in stream.chunks(duration_s=seconds):
        samples.extend(chunk.samples())
    return samples, stream.stats(), session.link.lost_frames - lost0


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


@pytest.fixture(scope="module")
def jumper_connected(session):
    la = LogicAnalyzer(session.device)
    la.testgen(period=40, high=20)
    time.sleep(0.05)
    samples, _, _ = stream_samples(session, 0.5)
    la.testgen_off()
    if len({s >> IN0 & 1 for s in samples}) < 2:
        pytest.skip("RPi ヘッダ 32 番と PMOD JB の 1 番が、ジャンパ線でつながっていない（ch 10 が動かない）")


@pytest.mark.parametrize("period, high", [(40, 20), (70, 30), (200, 10)])
def test_testgen_square_wave_is_streamed_with_the_exact_run_lengths(session, jumper_connected, period, high):
    # period・high が SAMPLE_CYCLES の整数倍: 1 の連続は high / SAMPLE_CYCLES、0 の連続は (period − high) / SAMPLE_CYCLES サンプルで、位相によらず一定
    assert period % SAMPLE_CYCLES == 0 and high % SAMPLE_CYCLES == 0
    LogicAnalyzer(session.device).testgen(period=period, high=high)
    time.sleep(0.05)
    samples, stats, lost = stream_samples(session)
    found = runs([s >> IN0 & 1 for s in samples])
    ones = {n for v, n in found if v == 1}
    zeros = {n for v, n in found if v == 0}
    print(f"period {period}、high {high}: 1 の連続 {sorted(ones)}、0 の連続 {sorted(zeros)}、{stats.samples} サンプル")
    assert len(found) > 20
    assert ones == {high // SAMPLE_CYCLES} and zeros == {(period - high) // SAMPLE_CYCLES}
    assert not stats.overrun and lost == 0
    assert abs(stats.samples - RATE_HZ * stats.elapsed_s) <= COUNT_TOLERANCE * RATE_HZ * stats.elapsed_s + RATE_HZ * 0.1


def test_testgen_is_hi_z_when_off(session):
    # CSR の enable = 0 の読み戻し（ジャンパ線に別の駆動を足さない確認ではなく、止めた設定が回路に残っていること）
    la = LogicAnalyzer(session.device)
    la.testgen(period=40, high=20)
    assert session.device.read32(g.LA_TESTGEN_CONTROL) >> g.LA_TESTGEN_CONTROL_ENABLE[0] & 1 == 1
    la.testgen_off()
    assert session.device.read32(g.LA_TESTGEN_CONTROL) >> g.LA_TESTGEN_CONTROL_ENABLE[0] & 1 == 0
