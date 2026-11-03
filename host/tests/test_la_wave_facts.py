"""raw_facts: 拍の列から、変化点を作らずに (最初の値, 最後の値, 違うビット) を求める。変化点の値の列から求めたものと同じ。"""

import random
from array import array

import pytest

from fprobe import la_wave
from fprobe.la_wave import raw_facts, stream_transitions


def toggles_of(values) -> int:
    first, out = int(values[0]), 0
    for v in set(int(x) for x in values):
        out |= v ^ first
    return out


def beats(rng, rle: bool, pack: int, n: int) -> bytes:
    if rle:
        recs = array("I", [rng.choice([0, rng.randrange(40)]) << 16 | rng.choice([5, 5, 7, rng.randrange(1 << 16)])
                           for _ in range(n * 4)])
        return recs.tobytes()
    width = 16 >> pack
    words = array("H")
    for _ in range(n * 8):
        w = 0
        for j in range(1 << pack):
            w |= (rng.choice([3, 3, rng.randrange(1 << width)]) & ((1 << width) - 1)) << (width * j)
        words.append(w)
    return words.tobytes()


@pytest.fixture(params=["numpy", "python"])
def mode(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(la_wave, "_np", None)
    return request.param


@pytest.mark.parametrize("seed", range(30))
def test_facts_equal_the_facts_of_the_transitions(mode, seed):
    rng = random.Random(seed)
    rle = rng.random() < 0.4
    pack = 0 if rle else rng.choice([0, 0, 1, 2, 3, 4])
    data = beats(rng, rle, pack, rng.randint(1, 40))
    cut = [0]
    while cut[-1] < len(data):                                   # 拍の境で切った複数のかたまり
        cut.append(min(len(data), cut[-1] + 16 * rng.randint(1, 9)))
    pieces = [memoryview(data)[a:b] for a, b in zip(cut, cut[1:])]
    values, _starts = la_wave._stream_transitions_py(data, rle, pack, 0, None)
    assert raw_facts(pieces, rle, pack) == (int(values[0]), int(values[-1]), toggles_of(values))


def test_no_beats_is_refused():
    with pytest.raises(ValueError):
        raw_facts([b""], False, 0)


def test_a_bit_that_differs_only_between_pieces_is_counted(mode):
    zeros = array("H", [0] * 8).tobytes()
    ones = array("H", [0b101] * 8).tobytes()
    assert raw_facts([zeros, ones], False, 0) == (0, 0b101, 0b101)         # 各かたまりの中は一定。かたまりの間だけ違う
    assert raw_facts([ones, zeros, ones], False, 0) == (0b101, 0b101, 0b101)
