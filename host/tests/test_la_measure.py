"""ロジアナの測定（fprobe.la_measure）: 周波数・周期・デューティ・パルス幅、チャンネル間の時間差、範囲。"""

import pytest

from fprobe.la import Capture, Transitions
from fprobe.la_measure import measure, measure_recording
from fprobe.recording import Recorder, Recording

PERIOD_S = 10e-9                                   # 100 MS/s


def wave(*channels: str, period_s: float = PERIOD_S, trigger: int = 0) -> Transitions:
    """チャンネルごとの '0'/'1' の文字列（1 文字 = 1 サンプル）から変化点の表現を作る。"""
    n = len(channels[0])
    samples = [sum(int(c[i]) << ch for ch, c in enumerate(channels)) for i in range(n)]
    return Transitions.from_capture(Capture(samples, trigger, 0, period_s, len(channels)))


def clock(high: int, low: int, cycles: int, lead: int = 0) -> str:
    return "0" * lead + ("1" * high + "0" * low) * cycles


def test_a_clock_has_its_frequency_period_duty_and_pulse_widths():
    out = measure(wave(clock(3, 7, 10, lead=5)))           # 周期 10 サンプル = 100 ns、high 30 ns
    ch = out["channels"][0]
    assert ch["rises"] == 10 and ch["falls"] == 10
    assert ch["period"]["count"] == 9
    assert ch["period"]["mean_ns"] == pytest.approx(100) and ch["period"]["stdev_ns"] == pytest.approx(0)
    assert ch["frequency_hz"] == pytest.approx(10e6)
    assert ch["duty"] == pytest.approx(0.3)
    assert ch["high"]["count"] == 10 and ch["high"]["mean_ns"] == pytest.approx(30)
    assert ch["low"]["count"] == 9 and ch["low"]["mean_ns"] == pytest.approx(70)   # 最後の low は終わらない
    assert out["resolution_ns"] == pytest.approx(10)


def test_the_extremes_are_located_by_sample():
    signal = "0" + "1100" * 3 + "11100" + "1100" * 2           # 4 番目の周期だけ長い（5 サンプル）
    ch = measure(wave(signal))["channels"][0]
    assert ch["period"]["max_ns"] == pytest.approx(50) and ch["period"]["min_ns"] == pytest.approx(40)
    assert ch["period"]["max_at"] == 13                          # 長い周期の始まりの立ち上がり
    assert measure(wave(signal))["at_unit"] == "sample"


def test_the_range_limits_what_is_measured():
    signal = clock(5, 5, 4) + clock(2, 2, 10)                   # 前半 100 ns 周期、後半 40 ns 周期
    full = measure(wave(signal))["channels"][0]
    later = measure(wave(signal), start=40)["channels"][0]
    assert full["period"]["min_ns"] == pytest.approx(40) and full["period"]["max_ns"] == pytest.approx(100)
    assert later["period"]["min_ns"] == pytest.approx(40) and later["period"]["max_ns"] == pytest.approx(40)
    assert measure(wave(signal), start=0, end=40)["channels"][0]["period"]["mean_ns"] == pytest.approx(100)


def test_high_ratio_counts_the_time_spent_high_even_without_a_full_period():
    ch = measure(wave("0011111111"))["channels"][0]
    assert ch["high_ratio"] == pytest.approx(0.8) and ch["period"]["count"] == 0 and ch["frequency_hz"] is None
    assert ch["initial"] == 0


def test_quiet_channels_are_left_out_unless_asked_for():
    tr = wave(clock(1, 1, 5), "1" * 10)
    assert list(measure(tr)["channels"]) == [0]
    quiet = measure(tr, channels=[1])["channels"][1]
    assert quiet["rises"] == 0 and quiet["initial"] == 1 and quiet["high_ratio"] == 1.0


def test_delay_from_an_edge_on_one_channel_to_the_next_edge_on_another():
    a = "0" + "1111100000" * 3
    b = "000" + "1111100000" * 2 + "11111000"                     # a の立ち上がりから 2 サンプル後
    out = measure(wave(a, b), relations=[{"type": "delay", "from": 0, "from_edge": "rise", "to": 1, "to_edge": "rise"}])
    rel = out["relations"][0]
    assert rel["count"] == 3 and rel["mean_ns"] == pytest.approx(20) and rel["unmatched"] == 0


def test_setup_and_hold_of_data_around_the_clock_edge():
    clk = "0000011111" * 3 + "00000"
    # データは 1 回目の立ち上がり（5）の 3 サンプル前・2 サンプル後、2 回目（15）の 1 サンプル前・4 サンプル後に変わる
    dat = "00" + "11111" + "0000000" + "11111" + "0" * 16
    out = measure(wave(clk, dat), relations=[{"type": "setup_hold", "clock": 0, "clock_edge": "rise", "data": [1],
                                              "setup_min_ns": 20, "hold_min_ns": 10}])
    rel = out["relations"][0]
    assert rel["setup"]["min_ns"] == pytest.approx(10) and rel["setup"]["min_at"] == 15
    assert rel["hold"]["min_ns"] == pytest.approx(20) and rel["hold"]["min_at"] == 5
    assert rel["setup"]["count"] == 3 and rel["hold"]["count"] == 2           # 3 回目の後はデータが変わらない
    assert rel["violation_count"] == 1
    assert rel["violations"] == [{"at": 15, "setup_ns": pytest.approx(10)}]


def test_a_data_change_on_the_clock_edge_is_zero_setup_and_hold():
    clk = "0001111000"
    dat = "0001111111"
    rel = measure(wave(clk, dat), relations=[{"type": "setup_hold", "clock": 0, "data": 1}])["relations"][0]
    assert rel["setup"]["min_ns"] == 0 and rel["hold"]["min_ns"] == 0


def test_unknown_relations_and_channels_are_errors():
    with pytest.raises(ValueError):
        measure(wave("0101"), relations=[{"type": "magic"}])
    with pytest.raises(ValueError):
        measure(wave("0101"), channels=[3])
    with pytest.raises(ValueError):
        measure(wave("0101"), start=3, end=2)


def test_a_recording_is_measured_across_its_chunks_by_time(tmp_path):
    hz = 100_000_000                                             # 1 サンプル = 1 タイムベース
    rec = Recorder(tmp_path / "r", hz)
    rec.add_waveform(wave(clock(2, 3, 4, lead=1)), start_ticks=1000)     # 周期 50 ns、立ち上がり 1・6・11・16
    rec.add_waveform(wave(clock(3, 7, 3, lead=1)), start_ticks=5000)     # 周期 100 ns、立ち上がり 1・11・21
    rec.close()
    r = Recording.open(tmp_path / "r")
    try:
        both = measure_recording(r)["channels"][0]
        assert both["period"]["count"] == 3 + 2                  # かたまりをまたいだ周期は数えない
        assert both["period"]["min_at"] == 1001 and both["period"]["max_at"] == 5001
        second = measure_recording(r, t0=4000)
        assert second["channels"][0]["period"]["mean_ns"] == pytest.approx(100) and second["at_unit"] == "timebase"
        assert measure_recording(r, t0=1000, t1=1010)["channels"][0]["rises"] == 2
    finally:
        r.close()
