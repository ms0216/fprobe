"""記録の VCD の書き出し（Recording.export_vcd）のバイト列を固定する。

期待のハッシュは、一括の書き出し（numpy）にする前の変化点ごとの実装（2026-11-03 の 2692a8b6）で同じ場面を書いた VCD の
もの（改行を LF にそろえた SHA-256。書き出しの改行は OS の既定: 前の実装と同じ）。一括の道と numpy を使わない道の両方が
同じバイト列を書くことも、場面と乱数の記録で確かめる。
"""

import hashlib
import os
import random

import pytest

from fprobe import la_vcd, recording_vcd
from fprobe.recording import Recording

from recording_vcd_scenarios import SCENARIOS, make

GOLDEN = {
    "plain": "b62b965fa8ebfc2e56e2fd1d6d3a579ed9b63288836fb0e868ec5e3349c5e328",
    "skew_across_chunks": "57f48de7fb0e0448fa8bbf7b24eaf48aeed08bdd0bb1fd68edd82638d0429421",
    "subset_unsorted": "0927d23e6f7aa1eab1c32bfdbd21ca5eb453258fcbf321f0b94cde6b5893c162",
    "fpga_mixed": "7615ca7802fc7e046009bcddcc5e62cf3a46b92bac0f5950eb611b8a3895f1b4",
    "fpga_only": "34c55d45e5a6f5394dc71d757f0ccd9cd95ced86d4a40edbe6342e26fb1f345e",
    "rounded_unit": "01de6cf6daf2db2696e681e68e63a09e957f0871186443e457ecb32af1512981",
    "negative_start": "f6db7ea87a4430f2e09566d2567d45b58deefca93a21c846514219869747141f",
    "sixteen_square": "6040e0b9519ae079778497717d1d908bc60bb02329c651f9d53783981703283a",
    "big_times": "2b9980884e826c2351612e27abc807b3b3f64639335357766fca941387f7ec26",
}


def export(directory, channels, out, python: bool, monkeypatch, workers: int | None = None) -> bytes:
    calls = []
    with monkeypatch.context() as m:
        if python:
            m.setattr(la_vcd, "_np", None)
        if workers is not None:
            m.setattr(la_vcd, "default_workers", lambda: workers)
        real = recording_vcd.write_fast
        m.setattr(recording_vcd, "write_fast", lambda *a, **k: calls.append(1) or real(*a, **k))
        r = Recording.open(directory)
        try:
            r.export_vcd(out, channels)
        finally:
            r.close()
    assert calls == ([] if python else [1])                         # どちらの道で書いたか
    return out.read_bytes()


def normalized_hash(data: bytes) -> str:
    if os.linesep == "\r\n":
        assert data.count(b"\r\n") == data.count(b"\n")             # 改行は OS の既定（前の実装と同じ）
    else:
        assert b"\r" not in data
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("python", [False, True], ids=["numpy", "python"])
def test_the_vcd_is_byte_identical_to_the_per_change_implementation(tmp_path, monkeypatch, name, python):
    channels = make(tmp_path / "r", name)
    data = export(tmp_path / "r", channels, tmp_path / "out.vcd", python, monkeypatch)
    assert normalized_hash(data) == GOLDEN[name]


@pytest.mark.parametrize("seed", range(12))
def test_random_recordings_are_written_the_same_by_both_paths(tmp_path, monkeypatch, seed):
    import recording_vcd_scenarios as s
    rng = random.Random(seed)
    hz = rng.choice([1e8, 3e8, 1e6])
    chunks, t = [], rng.randrange(-50, 50)
    for _ in range(rng.randrange(1, 5)):
        ch = rng.choice([1, 2, 5, 16])
        skew = tuple(rng.choice([0.0, 0.5, -2.0, None, 3.25]) for _ in range(ch)) if rng.random() < 0.6 else ()
        period = rng.choice([1 / hz, 2 / hz, 1 / (hz * 4)])
        chunks.append((t, rng.randrange(1, 900), ch, rng.choice([1, 3, 40]), period, skew))
        t += rng.randrange(0, 3000)
    channels = rng.choice([None, [0], [3, 0], [1, 2, 9]])
    s.SCENARIOS[f"random{seed}"] = (hz, chunks, rng.choice([0, 50, 800]), channels)
    try:
        make(tmp_path / "r", f"random{seed}")
    finally:
        del s.SCENARIOS[f"random{seed}"]
    monkeypatch.setattr(la_vcd, "_PIECE", rng.choice([3, 50, 1 << 16]))   # 描く単位の境で FPGA の行を差し込む所も通す
    fast = export(tmp_path / "r", channels, tmp_path / "a.vcd", False, monkeypatch, workers=rng.choice([1, 3]))
    slow = export(tmp_path / "r", channels, tmp_path / "b.vcd", True, monkeypatch)
    assert fast == slow
