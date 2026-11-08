"""REPEAT の巨大な展開でもメモリが有界であること（iter_decode_tokens / StreamChunk.iter_changes / changes の上限）。
100 MS/s の方形波は REPEAT で 10 秒が約 240 KB に縮むが、decode_tokens は全変化点を 1 つの配列に展開する
（変化点の数に比例するメモリ）。有界の道は同じ変化点を max_changes 個以下の断片に分けて返す。結果の意味は変えない。"""

import itertools
import random
import tracemalloc

import numpy as np
import pytest

from fprobe import la_tokens as T
from fprobe.la_stream import StreamChunk
from fprobe.la_wave import ChangeLimitExceeded
from test_la_tokens import BEAT, _mixed_samples, clock, pad_beat

VALUE0 = bytes([0x0F, 0xC0, 0x80, 0x80])                   # VALUE 0、間隔 0
TOGGLE_CH1_D1 = bytes([0x09])                              # ch 1、間隔 1


def repeat(pattern: int, count: int) -> bytes:
    return T._repeat(pattern, count)


def square_wave(n: int) -> bytes:
    """毎サンプル反転する方形波（周期 2）が全部で n 個の変化点（VALUE 0、TOGGLE 1 回、REPEAT n − 2 回）。1 拍に収まる。"""
    return pad_beat(VALUE0 + TOGGLE_CH1_D1 + (repeat(1, n - 2) if n >= 3 else b""))


def joined(pieces):
    pieces = list(pieces)
    if not pieces:
        return np.empty(0, np.uint16), np.empty(0, np.uint64)
    return np.concatenate([np.asarray(v) for v, _ in pieces]), np.concatenate([np.asarray(s) for _, s in pieces])


def same(data, limit, **kw):
    want = T.decode_tokens(data, **kw)
    pieces = list(T.iter_decode_tokens(data, max_changes=limit, **kw))
    assert all(0 < len(v) <= limit and len(v) == len(s) for v, s in pieces)
    v, s = joined(pieces)
    assert v.dtype == np.uint16 and s.dtype == np.uint64
    assert np.array_equal(v, want.values) and np.array_equal(s, want.starts)
    return len(pieces)


def peak_of(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# --- 再現: 全展開のメモリは展開の大きさに比例する ----------------------------------------------------------

def test_full_expansion_memory_grows_with_the_expanded_size_but_bounded_does_not():
    small, big = square_wave(1 << 20), square_wave(1 << 23)                    # 入力はどちらも 1 拍（16 バイト）
    assert len(small) == len(big) == BEAT
    full_small = peak_of(lambda: T.decode_tokens(small))
    full_big = peak_of(lambda: T.decode_tokens(big))
    assert full_big > 6 * full_small and full_big > 8 * (1 << 23)              # 変化点 1 つにつき 10 バイト以上: 展開に比例する

    def bounded(data):
        return lambda: [None for _ in T.iter_decode_tokens(data, max_changes=1 << 16)]

    assert peak_of(bounded(big)) < 100 * (1 << 16)                              # 断片の大きさで決まる（展開 840 万に依らない）
    assert peak_of(bounded(big)) < 2 * peak_of(bounded(small)) + (1 << 20)


def test_two_to_the_thirty_changes_are_streamed_with_constant_memory():
    data = square_wave(1 << 30)                                                # 全展開なら 10 GiB 超
    assert T.expanded_tokens(data) == 1 << 30                                  # 展開せずに数えられる
    assert T.token_extent(data)[0] == (1 << 30) - 1                                  # 時刻の進みも展開せずに求まる

    def head():
        got = list(itertools.islice(T.iter_decode_tokens(data, max_changes=1 << 18), 3))
        assert [len(v) for v, _ in got] == [1 << 18] * 3
        assert int(got[2][1][-1]) == 3 * (1 << 18) - 1                         # 毎サンプル変化

    assert peak_of(head) < 100 * (1 << 18)


# --- 全展開と一致する -----------------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [1, 2, 3, 7, 100, 1 << 20])
def test_pieces_equal_full_decode_for_a_small_repeat(limit):
    for n in (2, 3, 10, 257):
        same(square_wave(n), limit)


@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("use_c", [True, False])
def test_pieces_equal_full_decode_for_random_streams_with_repeat(seed, use_c, monkeypatch):
    if not use_c:
        monkeypatch.setattr(T, "_C", None)
    rng = random.Random(seed)
    samples = clock(3000, rng.choice([1, 2, 4]), ch=rng.randrange(16)) + _mixed_samples(rng, 300) + clock(2000, 3)
    data = T.encode_tokens(samples, repeat=True, tick_samples=rng.choice([None, 50]))
    assert T.count_repeats(data) > 0
    for limit in (1, 5, 64, 1000, 10_000_000):
        for first in (0, 123):
            same(data, limit, first=first, previous=None)


def test_a_group_straddling_the_repeat_is_kept_together():
    # TOGGLE ch1 (Δ1) を 3 回繰り返した直後に Δ0 の TOGGLE ch2: 繰り返しの最後の束に合流する。塊の境がどこでも同じ変化点
    tail = bytes([0x10])                                                       # ch 2、間隔 0
    for count in (2, 3, 6, 9):
        data = pad_beat(VALUE0 + TOGGLE_CH1_D1 + repeat(1, count) + tail + TOGGLE_CH1_D1)
        for limit in (1, 2, 3, 4, 5, 100):
            same(data, limit)
    pair = bytes([0x09, 0x11])                                                 # ch1 Δ1、ch2 Δ1: B = 2
    for count in (1, 2, 5):
        data = pad_beat(VALUE0 + pair + repeat(2, count) + tail)
        for limit in (1, 2, 3, 5, 100):
            same(data, limit)


def test_errors_are_the_same():
    bad = pad_beat(TOGGLE_CH1_D1)                                              # 流れの先頭が VALUE でない
    with pytest.raises(T.TokenError, match="VALUE"):
        list(T.iter_decode_tokens(bad, max_changes=4))
    with pytest.raises(T.TokenError, match="倍数"):
        list(T.iter_decode_tokens(b"\x00" * 15, max_changes=4))
    with pytest.raises(ValueError):
        list(T.iter_decode_tokens(b"", max_changes=0))
    assert list(T.iter_decode_tokens(b"", max_changes=4)) == []


# --- StreamChunk ---------------------------------------------------------------------------------------

def chunk_of(data):
    return StreamChunk(data=data, rle=True, first=0, previous=None, rle_format="tokens")


def test_chunk_iter_changes_equals_changes():
    c = chunk_of(square_wave(5000))
    v, s = c.changes()
    pv, ps = joined(c.iter_changes(1000))
    assert np.array_equal(v, pv) and np.array_equal(s, ps)
    assert [len(x) for x, _ in c.iter_changes(1000)] == [1000] * 5


def test_chunk_changes_refuses_with_the_facts_instead_of_allocating():
    c = chunk_of(square_wave(1 << 30))
    with pytest.raises(ChangeLimitExceeded) as e:
        c.changes(max_changes=1 << 20)
    assert e.value.limit == 1 << 20 and "max_changes" in str(e.value)
    c = chunk_of(square_wave(100))
    v, s = c.changes(max_changes=100)                                         # 上限ちょうどは通る
    assert len(v) == 100 and len(c.transitions()[0]) == 100


def test_chunk_iter_pieces_cover_the_chunk_without_gaps():
    c = chunk_of(square_wave(2500))
    ends = []
    prev_end = 0
    for v, s, end in c.iter_transitions(1000):
        assert int(s[0]) >= prev_end - 0 and end >= int(s[-1])
        ends.append(end)
        prev_end = end
    assert ends[-1] == c.first + c.length
    assert ends[0] == 1000                                                     # 次の断片の最初の変化点の時刻
