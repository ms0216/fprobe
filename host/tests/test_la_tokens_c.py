"""ロジアナの RLE トークン形式の復号の C 版（fprobe/_la_tokens_c.c）: numpy の版と全入力で一致すること（結果も誤りの文面も）。
C 版は任意なので、コンパイラが無く作れないときは skip する（その場合も numpy の版は test_la_tokens.py が確かめる）。"""

import importlib.util
import random
from pathlib import Path

import pytest

from fprobe import la_tokens as T
from test_la_tokens import BEAT, _mixed_samples, clock, pad_beat

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def c_version():
    """C 版を読む（無ければ作ってみる。作れなければ skip）。モジュールの終わりに元へ戻す。"""
    saved = T._C
    if T._C is None:
        spec = importlib.util.spec_from_file_location("_build_la_tokens_c", ROOT / "tools" / "build_la_tokens_c.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        try:
            mod.build()
        except (SystemExit, OSError, Exception):  # noqa: BLE001 - コンパイラが無い・失敗は skip
            pytest.skip("C 版をビルドできない（コンパイラが無い）")
        T._C = T._load_c()
    if T._C is None:
        pytest.skip("C 版を読めない")
    yield T._C
    T._C = saved


def numpy_only(fn, *a, **kw):
    """C 版を外して（numpy の版で）同じ呼び出しをする。(結果 | 誤りの文面) を返す。"""
    c = T._C
    T._C = None
    try:
        return outcome(fn, *a, **kw)
    finally:
        T._C = c


def outcome(fn, *a, **kw):
    try:
        r = fn(*a, **kw)
    except T.TokenError as e:
        return ("error", str(e))
    if isinstance(r, T.TokenDecode):
        return ("ok", [int(x) for x in r.values], [int(x) for x in r.starts], r.end_time, r.last_value, r.pad_bytes, r.tokens,
                r.repeat_tokens)
    return ("ok", r)


def both(data, first, previous, width=16, threads=1):
    for fn in (T.decode_tokens, T.token_extent):
        assert outcome(fn, data, first, previous, width=width, threads=threads) == \
            numpy_only(fn, data, first, previous, width=width, threads=threads), (fn.__name__, threads, width)


@pytest.fixture
def small_pieces(monkeypatch):
    monkeypatch.setattr(T, "PARALLEL_MIN_BYTES", 64)       # 分ける道を小さな塊でも通す
    monkeypatch.setattr(T, "PIECE_MIN_BYTES", 32)


def test_c_version_is_used(c_version):
    data = T.encode_tokens(_mixed_samples(random.Random(1), 500))
    calls = []
    real = T._C
    T._C = lambda *a: (calls.append(1), real(*a))[1]
    try:
        T.decode_tokens(data, threads=1)
    finally:
        T._C = real
    assert calls


@pytest.mark.parametrize("seed", range(12))
def test_valid_streams_match_numpy(seed, small_pieces):
    rng = random.Random(seed)
    samples = _mixed_samples(rng, 3000) if seed % 3 else clock(900, 3) + _mixed_samples(rng, 800) + clock(300, 2)
    data = T.encode_tokens(samples, repeat=bool(seed & 1), tick_samples=rng.choice([None, 40, 3000]))
    for first, previous, chunk in [(0, None, data), (123456, 0x5A5A, data[BEAT:]), (7, 0, data[BEAT * 3:])]:
        for threads in (1, 2, 3, 8):
            both(chunk, first, previous, threads=threads)
    both(data, 0, None, width=16, threads=4)
    both(data[:BEAT * 5], 0, None, width=8)                # ch が width 以上の誤りの文面も一致


@pytest.mark.parametrize("seed", range(40))
def test_corrupted_and_random_streams_match_numpy(seed, small_pieces):
    """正しい流れの数バイトを壊した列と、頭が多めの乱数列で、結果も誤りの文面も numpy の版と同じ。"""
    rng = random.Random(1000 + seed)
    data = bytearray(T.encode_tokens(_mixed_samples(rng, 600), repeat=bool(seed & 1)))
    if seed % 4 == 3:                                      # 乱数列（頭 7 割・尾 2 割・NOP 1 割くらい）
        data = bytearray(rng.choice([rng.randrange(0, 0x80), rng.randrange(0x80, 0x100), 0x07, 0x0F, 0xC0, 0x80]
                                    + [rng.randrange(0, 0x80)] * 4) for _ in range(BEAT * 20))
        data[::BEAT] = bytes(b & 0x7F for b in data[::BEAT])
    for _ in range(rng.choice([0, 1, 2, 5])):
        data[rng.randrange(len(data))] = rng.randrange(256)
    data = bytes(data)
    for first, previous, chunk in [(0, None, data), (99, 0x1234, data), (99, 0x1234, data[BEAT:]), (0, 0, data[BEAT * 2:])]:
        for width in (16, 8):
            for threads in (1, 3):
                both(chunk, first, previous, width=width, threads=threads)


def test_huge_repeat_is_left_to_numpy_and_gives_the_same_answer():
    value0 = bytes([0x0F, 0xC0, 0x80, 0x80])
    toggle = bytes([0x09])                                 # ch 1、間隔 1
    for rep_tail in (bytes([0x17, 0xC0 | 0x3F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]), bytes([0x17, 0xC0 | 0x04])):
        both(pad_beat(value0 + toggle + rep_tail), 0, None)


def test_repeat_expansion_larger_than_the_first_guess_is_retried():
    data = T.encode_tokens(clock(20000, 3), repeat=True)
    assert T.count_repeats(data) > 0
    c = outcome(T.decode_tokens, data, 0, None, threads=1)
    assert c[0] == "ok" and len(c[1]) > len(data)           # 出力の容量（バイト数）より変化点のほうが多い
    assert c == numpy_only(T.decode_tokens, data, 0, None, threads=1)


def test_falls_back_without_the_library(monkeypatch):
    data = T.encode_tokens(_mixed_samples(random.Random(3), 300))
    want = outcome(T.decode_tokens, data)
    monkeypatch.setattr(T, "_C", None)
    assert not T.c_available()
    assert outcome(T.decode_tokens, data) == want
    monkeypatch.setenv("FPROBE_LA_TOKENS_C", "0")
    assert T._load_c() is None
