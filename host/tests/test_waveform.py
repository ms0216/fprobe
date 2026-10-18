"""fprobe.la.Waveform: 変化点の捕捉を、画面の画素ごとに縮めた波形（0 / 1 / x）にする。"""

import time
from array import array

import pytest

from fprobe.la import Transitions, Waveform


def tr(values, starts, length, trigger=0, channels=2):
    return Transitions(array("H", values), array("Q", starts), length, trigger, 0, 1e-8, channels)


def test_each_pixel_is_constant_or_changing():
    # ch0: 0..9 = 0、10..19 = 1。ch1: ずっと 1
    w = Waveform(tr([0b10, 0b11], [0, 10], 20))
    assert w.view(0, 20, 4) == ["0011", "1111"]                                     # 区間 [0,5)[5,10)[10,15)[15,20)
    assert w.view(0, 20, 3)[0] == "0x1"                                            # [0,6.67) [6.67,13.3) [13.3,20)
    assert w.view(8, 12, 4)[0] == "0011"


def test_zoomed_in_beyond_one_sample_per_pixel():
    w = Waveform(tr([0, 1], [0, 3], 6, channels=1))
    assert w.view(2, 4, 8) == ["00001111"]                                         # 1 サンプル = 4 画素


def test_the_view_is_clamped_to_the_capture():
    w = Waveform(tr([0, 1], [0, 3], 6, channels=1))
    assert w.view(-5, 100, 6) == ["000111"]
    with pytest.raises(ValueError):
        w.view(4, 4, 10)


def test_a_million_transitions_are_summarized_quickly():
    n = 1_000_000
    w = Waveform(tr([i & 0xFFFF for i in range(n)], range(0, 2 * n, 2), 2 * n, channels=16))
    t0 = time.perf_counter()
    levels = w.view(0, 2 * n, 2000)
    assert time.perf_counter() - t0 < 0.5
    assert levels[0] == "x" * 2000 and len(levels) == 16
    assert levels[15][:65] == "0" * 65 and levels[15][65] == "x"             # ch15 は 65536 サンプルごとに変わる


def test_the_probe_keeps_the_last_capture_for_the_screen():
    from fprobe.mcp_server import Probe
    p = Probe("unused")                                              # 機器は開かない
    with pytest.raises(RuntimeError):
        p.la_view()
    p._keep_capture(tr([0, 1], [0, 3], 6, trigger=3, channels=1), "la.capture")
    first = p.la_view(width=6)
    assert first["levels"] == ["000111"] and (first["start"], first["end"], first["width"]) == (0, 6, 6)
    assert first["capture"] == {"id": 1, "length": 6, "sample_period_ns": 10.0, "trigger_index": 3, "channels": 1,
                                "source": "la.capture"}
    p._keep_capture(tr([1], [0], 4, channels=1), "la.deep.collect")
    assert p.la_view(start=1, end=3, width=2)["capture"]["id"] == 2


def test_pixel_bounds_are_exact_integers():
    # 浮動小数の誤差で区間の終わりが 1 サンプル延び、表示の外のすぐ右の変化で最後の画素が 'x' にならない
    w = Waveform(tr([0, 1], [0, 15], 20, channels=1))
    assert w.view(0, 15, 1919) == ["0" * 1919]


def test_the_edge_tables_are_built_only_when_the_screen_asks(monkeypatch):
    # 捕捉のたびに作ると、画面を使わない AI の操作まで遅くなる（深いバッファの数千万変化点）
    import fprobe.mcp_server as m
    built = []

    class Counting(Waveform):
        def __init__(self, t):
            built.append(t)
            super().__init__(t)
    monkeypatch.setattr(m, "Waveform", Counting)
    p = m.Probe("unused")
    p._keep_capture(tr([0, 1], [0, 3], 6, channels=1), "la.capture")
    assert built == []
    p.la_view(width=6)
    p.la_view(width=3)
    assert len(built) == 1
