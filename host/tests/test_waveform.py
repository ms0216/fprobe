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
    t0 = time.process_time()
    levels = w.view(0, 2 * n, 2000)
    assert time.process_time() - t0 < 0.5
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
    import fprobe.probe_la as la_module
    built = []

    class Counting(Waveform):
        def __init__(self, t):
            built.append(t)
            super().__init__(t)
    monkeypatch.setattr(la_module, "Waveform", Counting)
    p = m.Probe("unused")
    p._keep_capture(tr([0, 1], [0, 3], 6, channels=1), "la.capture")
    assert built == []
    p.la_view(width=6)
    p.la_view(width=3)
    assert len(built) == 1


def test_the_fast_edge_tables_match_the_plain_ones(monkeypatch):
    # numpy があれば変化点の表を一括の計算で作る（SWD のクロック等で数千万の変化点の記録を数秒で開く）。無くても同じ結果
    pytest.importorskip("numpy")
    import random

    from fprobe import la
    rng = random.Random(3)
    values, starts, v, s = array("H"), array("Q"), 0, 0
    for _ in range(20_000):
        values.append(v)
        starts.append(s)
        v ^= rng.choice([1, 2, 3, 0x8000, 0x0400 | 1, rng.randrange(1 << 16)])
        s += rng.randint(1, 50)
    tr = Transitions(values, starts, s + 10, 0, 0, 1e-8, 16)
    fast = Waveform(tr)
    monkeypatch.setattr(la, "_np", None)
    plain = Waveform(tr)
    assert fast._initial == plain._initial
    assert [list(e) for e in fast._edges] == [list(e) for e in plain._edges]
    assert [e.typecode for e in fast._edges] == [e.typecode for e in plain._edges]


def test_the_capture_tile_does_not_wait_for_a_long_device_operation():
    # 時間軸の捕捉のタイルは読みのスレッドで: 機器の長い操作（Probe._lock を持つ la_collect 等）を待たない
    import threading

    from fprobe.mcp_server import Probe
    p = Probe("unused")
    tr = Transitions(array("H", [0, 1]), array("Q", [0, 5]), 10, 0, 0, 1e-8, 2)
    p._keep_capture(tr, "test")
    held, release = threading.Event(), threading.Event()

    def long_operation():
        with p._lock:
            held.set()
            release.wait(5)
    worker = threading.Thread(target=long_operation)
    worker.start()
    held.wait(5)
    t = time.monotonic()
    try:
        wave, info = p._capture_wave()
        assert time.monotonic() - t < 1.0 and info["length"] == 10 and wave.level_at(0, 6) == 1
    finally:
        release.set()
        worker.join()


def test_the_edge_tables_are_compact():
    # 変化点の表だけを持ち（元の並びは持たない）、サンプル番号が 32 ビットに収まれば 4 バイトの整数で（大きな記録の
    # 時間軸でデーモンのメモリが 1 GB を超えた）
    tr = Transitions(array("H", [0, 1, 0]), array("Q", [0, 5, 7]), 10, 0, 0, 1e-8, 2)
    w = Waveform(tr)
    assert not hasattr(w, "tr") and w.length == 10
    assert all(e.typecode == "I" for e in w._edges) and w.nbytes == 2 * 4
    assert w.view(0, 10, 2) == ["0x", "00"] and list(w.edges_between(0, 0, 10)) == [5, 7]
