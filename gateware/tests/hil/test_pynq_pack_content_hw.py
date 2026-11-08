"""PYNQ-Z2 の fprobe_pynq_deep: チャンネル詰め（la_pack）が、ch 0〜7 の中身を正しい並びで運ぶことを確かめる。
仕様: docs/superpowers/specs/2026-10-31-la-stream-pack-design.md 5 章（実機の中身の確認が未了だった）

配線: RPi ヘッダの 32 番（GPIO12、la_testgen の出力）を、PMOD JA の SWO の穴（JA9、ロジアナの ch 6）と、PMOD JB の 1 番（ch 10）の
両方につなぐ。環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行する。つながっていなければ skip する。
許容する幅はこの試験が明示する（fprobe 本体は既定を持たない）。
"""

import os
import time

import pytest

from fprobe.la import DeepAnalyzer, LogicAnalyzer
from fprobe.session import Session

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]
SWO = 6                                            # PMOD JA9 = ch 6
IN0 = 10                                           # PMOD JB の 1 番 = ch 10
SYS_HZ = 100_000_000
PERIOD, HIGH = 40, 20                              # sys の 40 周期（400 ns）のうち 20 周期が High
TOLERANCE = 1                                      # 辺が標本の時刻に重なるので、1 と 0 の長さは ±1 サンプルずれる（この試験が決める値）


def check_period(ones, zeros, cycles):
    """1 の連続・0 の連続は、それぞれ 1 通りで、期待の ±TOLERANCE 以内。隣り合う 1 と 0 の和は周期に一致する（欠けなし）。"""
    assert len(ones) == 1 and len(zeros) == 1, (ones, zeros)
    (one,), (zero,) = ones, zeros
    assert abs(one - HIGH // cycles) <= TOLERANCE and abs(zero - (PERIOD - HIGH) // cycles) <= TOLERANCE, (one, zero)
    assert one + zero == PERIOD // cycles, (one, zero)


@pytest.fixture(scope="module")
def session():
    with Session.open_eth(HOST, link_up_wait_s=8) as s:
        caps = s.device.capabilities()
        if not (getattr(caps, "la_testgen", False) and getattr(caps, "la_pack", False)):
            pytest.skip("この PYNQ の回路に la_testgen と la_pack が無い（fprobe_pynq_deep を入れる）")
        yield s


def stream(session, rate_hz, seconds, **kw):
    la = DeepAnalyzer(session.device)
    lost0 = session.link.lost_frames
    st = la.stream(rate_hz=rate_hz, rle=False, **kw)
    samples = []
    for chunk in st.chunks(duration_s=seconds):
        samples.extend(chunk.samples())
    return samples, st.stats(), session.link.lost_frames - lost0


def runs(bits):
    out, n = [], 1
    for a, b in zip(bits, bits[1:]):
        if a == b:
            n += 1
        else:
            out.append((a, n))
            n = 1
    out.append((bits[-1], n))
    return out[1:-1]                               # 最初と最後は途中から


def lengths(samples, ch):
    found = runs([s >> ch & 1 for s in samples])
    return {n for v, n in found if v == 1}, {n for v, n in found if v == 0}, len(found)


@pytest.fixture(scope="module", autouse=True)
def square_wave(session):
    la = LogicAnalyzer(session.device)
    la.testgen(period=PERIOD, high=HIGH)
    time.sleep(0.05)
    yield
    la.testgen_off()


def test_ch6_sees_the_same_square_wave_as_ch10_in_the_unpacked_stream(session):
    samples, stats, lost = stream(session, 10e6, 2.0)
    cycles = SYS_HZ // 10_000_000
    ones6, zeros6, n6 = lengths(samples, SWO)
    ones10, zeros10, _ = lengths(samples, IN0)
    print(f"ch6: 1 の連続 {sorted(ones6)}、0 の連続 {sorted(zeros6)}、{n6} 回。ch10: {sorted(ones10)}、{sorted(zeros10)}")
    if n6 < 20:
        pytest.skip("RPi ヘッダ 32 番と PMOD JA9 が、ジャンパ線でつながっていない（ch 6 が動かない）")
    check_period(ones6, zeros6, cycles)
    if not ones10:
        pytest.skip("RPi ヘッダ 32 番と PMOD JB の 1 番が、ジャンパ線でつながっていない（ch 10 が動かない）")
    check_period(ones10, zeros10, cycles)
    assert not stats.overrun and lost == 0


@pytest.mark.parametrize("rate_mhz", [10, 50])
def test_packed_8ch_stream_carries_the_same_runs_on_ch6(session, rate_mhz):
    cycles = SYS_HZ // (rate_mhz * 1_000_000)
    samples, stats, lost = stream(session, rate_mhz * 1e6, 2.0, pack_channels=8)
    ones, zeros, n = lengths(samples, SWO)
    print(f"{rate_mhz} MS/s・8 ch 詰め: ch6 の 1 の連続 {sorted(ones)}、0 の連続 {sorted(zeros)}、{n} 回、{stats.samples} サンプル")
    if n <= 20:
        pytest.skip("RPi ヘッダ 32 番と PMOD JA9 が、ジャンパ線でつながっていない（ch 6 が動かない）")
    check_period(ones, zeros, cycles)
    assert not stats.overrun and lost == 0
    assert all(s >> 8 == 0 for s in samples)       # 詰めたとき、上位の 8 チャンネルは記録されない
