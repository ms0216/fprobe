"""2026-11-03 のレビュー（記録と VCD。docs/reports/2026-11-03-cloud-A.md）: Recording.export_vcd の一括の道と変化点ごとの道を、かたまりの境目・ずれ・FPGA の行が密な乱数の
記録でバイト比較する（既存の 12 通りより厳しい場面）。"""
import random

import pytest

pytest.importorskip("numpy")

from fprobe import la_vcd, la_vcd_render
from fprobe.recording import Recording

import recording_vcd_scenarios as s
from test_recording_vcd_golden import export


@pytest.mark.parametrize("seed", range(80))
def test_dense_random_recordings(tmp_path, monkeypatch, seed):
    rng = random.Random(77_000 + seed)
    hz = rng.choice([1e8, 3e8])
    chunks, t = [], rng.randrange(-20, 20)
    for _ in range(rng.randrange(2, 7)):
        ch = rng.choice([1, 2, 5, 16])
        skew = tuple(rng.choice([0.0, 0.5, -7.0, None, 3.25, 30.0]) for _ in range(ch)) if rng.random() < 0.7 else ()
        period = rng.choice([1 / hz, 2 / hz])
        chunks.append((t, rng.randrange(1, 120), ch, rng.choice([1, 2, 5]), period, skew))
        t += rng.randrange(0, 400)                                   # 次のかたまりがすぐ（重なりうる）
    channels = rng.choice([None, [0], [3, 0], [1, 2, 9], [15, 0, 15]])
    name = f"rv{seed}"
    s.SCENARIOS[name] = (hz, chunks, rng.choice([50, 400]), channels)
    try:
        s.make(tmp_path / "r", name)
    finally:
        del s.SCENARIOS[name]
    monkeypatch.setattr(la_vcd, "_PIECE", rng.choice([1, 2, 3, 17]))
    monkeypatch.setattr(la_vcd_render, "_UNIFORM_MIN", rng.choice([1, 2, 64]))
    fast = export(tmp_path / "r", channels, tmp_path / "a.vcd", False, monkeypatch, workers=rng.choice([1, 3]))
    slow = export(tmp_path / "r", channels, tmp_path / "b.vcd", True, monkeypatch)
    assert fast == slow
