"""2026-11-03 のレビュー（記録と VCD。docs/reports/2026-11-03-cloud-A.md）: 変化点・VCD の一括・並列の道を、乱数の入力（かたまりの境目をまたぐ・境目の前と同じ値・欠け）で、
自分で書いた素朴な逐次の実装とバイト比較する。"""
import random
import threading
from array import array

import pytest

np = pytest.importorskip("numpy")

from fprobe import la_vcd, la_vcd_render, la_wave
from fprobe.la_vcd import VcdWriter
from fprobe.la_wave import stream_transitions


def _samples(data: bytes, rle: bool, pack: int):
    if rle:
        out = []
        for i in range(0, len(data), 4):
            r = int.from_bytes(data[i:i + 4], "little")
            out += [r & 0xFFFF] * ((r >> 16) + 1)
        return out
    width, count = 16 >> pack, 1 << pack
    out = []
    for i in range(0, len(data), 2):
        w = int.from_bytes(data[i:i + 2], "little")
        out += [(w >> (width * j)) & ((1 << width) - 1) for j in range(count)]
    return out


def _naive(data, rle, pack, first, previous):
    values, starts = [], []
    for k, s in enumerate(_samples(data, rle, pack)):
        if s != previous:
            values.append(s)
            starts.append(first + k)
            previous = s
    return values, starts


def _data(rng, rle, pack, beats):
    if rle:
        recs = []
        for _ in range(beats * 4):
            run = rng.choice([0, 0, 1, 5, 0xFFFF])
            recs.append((run << 16) | rng.choice([0, 1, 0xFFFF, rng.randrange(1 << 16)]))
        return b"".join(r.to_bytes(4, "little") for r in recs)
    width = 16 >> pack
    words = []
    for _ in range(beats * 8):
        w = 0
        for j in range(1 << pack):
            w |= (rng.choice([0, 1, (1 << width) - 1, rng.randrange(1 << width)]) & ((1 << width) - 1)) << (width * j)
        words.append(w)
    return b"".join(w.to_bytes(2, "little") for w in words)


@pytest.mark.parametrize("seed", range(60))
def test_stream_transitions_match_the_naive_one_across_chunks(seed):
    rng = random.Random(seed)
    rle = rng.random() < 0.3
    pack = 0 if rle else rng.choice([0, 1, 2, 3, 4])
    data = _data(rng, rle, pack, rng.randrange(1, 40))
    unit = 4 if rle else 16
    cuts = sorted({0, len(data), *(rng.randrange(0, len(data) // unit + 1) * unit for _ in range(rng.randrange(0, 6)))})
    first = rng.choice([0, 1, (1 << 32) - 3, (1 << 40) + 7])
    whole_v, whole_s = _naive(data, rle, pack, first, None)
    got_v, got_s, previous, pos = [], [], None, first
    for a, b in zip(cuts, cuts[1:]):
        part = data[a:b]
        v, s = stream_transitions(part, rle, pack, pos, previous)
        nv, ns = _naive(part, rle, pack, pos, previous)
        assert [int(x) for x in v] == nv and [int(x) for x in s] == ns
        got_v += nv
        got_s += ns
        samples = _samples(part, rle, pack)
        if samples:
            previous = samples[-1]
        pos += len(samples)
    assert got_v == whole_v and got_s == whole_s                 # つなぐと全体の変化点（境目に重複・欠けが無い）
    assert all(x < y for x, y in zip(got_s, got_s[1:]))          # 時刻は狭義の単調増加


def _naive_vcd(path, channels, period_s, ops):
    """VcdWriter（ずれ無し）の素朴な逐次の実装。"""
    ids = [chr(33 + ch) for ch in range(channels)]
    per = period_s * 1e9
    from fprobe.la_vcd import _vcd_header, vcd_comments, vcd_unit
    unit, label, exact = vcd_unit([per])
    _, _, _, header = _vcd_header(channels, None, trigger=False, unit=(unit, label),
                                  comments=vcd_comments(exact, [], {**la_vcd.DEFAULT_NAMES}))
    per = per / unit
    out = ["\n".join(header) + "\n"]
    previous = None
    for op in ops:
        if op[0] == "gap":
            out.append(f"#{round(op[1] * per)}\n" + "".join(f"x{i}\n" for i in ids))
            previous = None
            continue
        _, values, starts = op
        current = None
        for v, t in zip(values, starts):
            changed = ((1 << channels) - 1) if previous is None else (v ^ previous) & ((1 << channels) - 1)
            previous = v
            if not changed:
                continue
            if t != current:
                out.append(f"#{round(t * per)}\n")
                current = t
            out += [f"{v >> ch & 1}{ids[ch]}\n" for ch in range(channels) if changed >> ch & 1]
    with open(path, "w", newline="\n") as f:
        f.write("".join(out))


@pytest.mark.parametrize("seed", range(40))
def test_vcdwriter_matches_the_naive_one(seed, tmp_path, monkeypatch):
    rng = random.Random(1000 + seed)
    channels = rng.choice([1, 3, 8, 16])
    period = rng.choice([1e-8, 1.25e-9, 1 / 3e8])
    ops, t, previous = [], rng.choice([0, (1 << 32) - 5]), None
    for _ in range(rng.randrange(1, 12)):
        if rng.random() < 0.15:
            ops.append(("gap", t))
            t += rng.randrange(1, 50)
            continue
        values, starts = array("H"), array("Q")
        n = rng.randrange(0, 300)
        for k in range(n):
            if k == 0 and previous is not None and rng.random() < 0.5:
                v = previous                                       # 境目の直前と同じ値（chunk の transitions ではあり得るか）
            else:
                v = rng.randrange(1 << 16)                          # ch の外のビットも立てる
            values.append(v)
            starts.append(t)
            t += rng.randrange(1, 40)
            previous = v
        ops.append(("w", values, starts))
    monkeypatch.setattr(la_vcd, "_PIECE", rng.choice([1, 7, 1 << 16]))
    monkeypatch.setattr(la_vcd_render, "_UNIFORM_MIN", rng.choice([1, 2, 64]))
    with VcdWriter(tmp_path / "fast.vcd", channels, period, workers=rng.choice([1, 2, 5])) as vcd:
        for op in ops:
            vcd.gap(op[1]) if op[0] == "gap" else vcd.write(op[1], op[2])
    _naive_vcd(tmp_path / "naive.vcd", channels, period, ops)
    assert (tmp_path / "fast.vcd").read_bytes() == (tmp_path / "naive.vcd").read_bytes()


@pytest.mark.parametrize("seed", range(40))
def test_skewed_vcdwriter_matches_the_python_path(seed, tmp_path, monkeypatch):
    rng = random.Random(2000 + seed)
    channels = rng.choice([2, 5, 16])
    shifts = [rng.choice([0.0, 0.5, -2.0, 3.25, 40.0]) for _ in range(channels)]
    ops, t = [], rng.randrange(0, 100)
    for _ in range(rng.randrange(1, 10)):
        if rng.random() < 0.2:
            ops.append(("gap", t))
            t += rng.randrange(1, 30)
            continue
        n = rng.randrange(0, 200)
        values = array("H", (rng.randrange(1 << 16) for _ in range(n)))
        starts = array("Q")
        for _ in range(n):
            starts.append(t)
            t += rng.randrange(1, 6)
        ops.append(("w", values, starts))

    def run(path, python):
        with monkeypatch.context() as m:
            if python:
                m.setattr(la_vcd, "_np", None)
            else:
                m.setattr(la_vcd, "_PIECE", rng.choice([1, 5, 1 << 16]))
            with VcdWriter(path, channels, 1e-8, shifts=shifts, workers=None if python else rng.choice([1, 3])) as vcd:
                for op in ops:
                    vcd.gap(op[1]) if op[0] == "gap" else vcd.write(op[1], op[2])
        return path.read_bytes()
    assert run(tmp_path / "a.vcd", False) == run(tmp_path / "b.vcd", True)


def test_skewed_close_error_shuts_down_the_threads(tmp_path, monkeypatch):
    """ずらす ch があるとき、描く途中の例外が with を抜けて出た後、描くスレッド・書き手のスレッドが残らないこと
    （DeepAnalyzer の `with VcdWriter(...)` と同じ使い方）。今は close の _flush(None) が先に例外を出し、
    _OrderedOutput.close（スレッドの後始末）が呼ばれない。"""
    import time

    def broken(*args):
        raise RuntimeError("render failed")
    before = {t.ident for t in threading.enumerate()}
    with pytest.raises(RuntimeError):
        with VcdWriter(tmp_path / "e.vcd", 2, 1e-8, shifts=[0.0, 1.5], workers=3) as vcd:
            vcd.write(array("H", [1, 2, 3]), array("Q", [0, 5, 9]))
            monkeypatch.setattr(la_vcd, "_render", broken)
            for k in range(5):
                vcd.write(array("H", [1, 2]), array("Q", [20 + k * 10, 25 + k * 10]))
                time.sleep(0.05)
    time.sleep(0.2)
    left = [t.name for t in threading.enumerate() if t.ident not in before and t.name.startswith("vcd")]
    assert not left, left
