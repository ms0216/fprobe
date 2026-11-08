"""ロジアナの RLE トークン形式（fprobe.la_tokens）: 参照符号器と復号器の往復・境・誤りの報告・numpy と純 Python の一致。
仕様: docs/superpowers/specs/2026-11-07-la-rle2-design.md §3。"""

import itertools
import random
from array import array

import pytest

from fprobe import la_tokens as T
from fprobe.la_wave import decode_transitions

BEAT = T.BEAT_BYTES


# --- 道具 -----------------------------------------------------------------------------------------------

def runs_of(samples):
    """サンプル列 → 変化点 (値の列, 開始の列)（ふつうの run-length。参照）。"""
    values, starts = [], []
    prev = None
    for s, v in enumerate(samples):
        if v != prev:
            values.append(v)
            starts.append(s)
            prev = v
    return values, starts


def decoded(data, **kw):
    d = T.decode_tokens(data, **kw)
    return [int(v) for v in d.values], [int(s) for s in d.starts], d


def level_samples(levels):
    return itertools.chain.from_iterable(itertools.repeat(v, n) for v, n in levels)


def random_signal(rng, n, kind):
    if kind == "hold":                                     # 値を保ち、ときどき ch を 1〜16 個変える
        v, out = rng.randrange(1 << 16), []
        while len(out) < n:
            out.extend([v] * rng.choice([1, 1, 2, 3, 7, 8, 70, 71, 300]))
            v ^= rng.randrange(1, 1 << 16) if rng.random() < 0.3 else 1 << rng.randrange(16)
        return out[:n]
    if kind == "clock":                                    # ch 3 のクロック（半周期 4）
        return [(i // 4 & 1) << 3 for i in range(n)]
    if kind == "clock_slow":                               # クロック + 低速の信号（ch 9、1 変化 / 5000 サンプル）
        return [(i // 4 & 1) << 3 | (i // 5000 & 1) << 9 for i in range(n)]
    if kind == "every":                                    # 毎サンプル変化（全 ch ランダム）
        return [rng.randrange(1 << 16) for _ in range(n)]
    if kind == "k3":                                       # 毎サンプルちょうど 3 ch が変わる
        v, out = 0, []
        for _ in range(n):
            v ^= sum(1 << c for c in rng.sample(range(16), 3))
            out.append(v)
        return out
    if kind == "sparse":                                   # 長い間隔（尾のバイトが 1〜4 個）
        v, out = 0, []
        while len(out) < n:
            out.extend([v] * rng.choice([1, 6, 7, 8, 70, 71, 8198, 8199, 9000]))
            v ^= 1 << rng.randrange(16)
        return out[:n]
    raise AssertionError(kind)


KINDS = ["hold", "clock", "clock_slow", "every", "k3", "sparse"]


# --- 往復 ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seed", range(4))
def test_roundtrip(kind, seed):
    rng = random.Random(1000 * KINDS.index(kind) + seed)
    n = rng.randrange(1, 30000)
    samples = random_signal(rng, n, kind)
    data = T.encode_tokens(samples)
    assert len(data) % BEAT == 0 and data
    values, starts, d = decoded(data)
    assert (values, starts) == runs_of(samples)
    assert d.end_time == starts[-1]                         # 最後の出来事 = 最後の変化
    assert list(T.expand_samples(data)) == samples[:d.end_time + 1]
    assert d.pad_bytes < BEAT * (len(data) // BEAT)


@pytest.mark.parametrize("kind", KINDS)
def test_numpy_and_pure_python_agree(kind, monkeypatch):
    rng = random.Random(7 + KINDS.index(kind))
    samples = random_signal(rng, 20000, kind)
    data = T.encode_tokens(samples)
    want = T.decode_tokens(data)
    ext = T.token_extent(data)
    monkeypatch.setattr(T, "_np", None)
    got = T.decode_tokens(data)
    assert [int(v) for v in got.values] == [int(v) for v in want.values]
    assert [int(s) for s in got.starts] == [int(s) for s in want.starts]
    assert got[2:] == want[2:]                              # end_time・last_value・pad_bytes・tokens
    assert T.token_extent(data) == ext


@pytest.mark.parametrize("kind", KINDS)
def test_chunked_decode_chains_state(kind):
    """拍のどこで切っても、(first, previous) をつなぐと全体の復号と同じ（束が拍をまたがない）。1 拍ずつの切りも通る。"""
    rng = random.Random(11 + KINDS.index(kind))
    samples = random_signal(rng, 8000, kind)
    data = T.encode_tokens(samples)
    nbeats = len(data) // BEAT
    whole = decoded(data)
    for size in (1, 2, 5, nbeats // 3 + 1):
        values, starts, first, previous, pad, advance = [], [], 0, None, 0, 0
        for b in range(0, nbeats, size):
            piece = data[b * BEAT:(b + size) * BEAT]
            v, s, d = decoded(piece, first=first, previous=previous)
            assert T.token_extent(piece, first, previous) == (d.end_time - first, d.last_value, d.pad_bytes)
            values += v
            starts += s
            first, previous, pad = d.end_time, d.last_value, pad + d.pad_bytes
        assert (values, starts) == whole[:2]
        assert (first, previous, pad) == (whole[2].end_time, whole[2].last_value, whole[2].pad_bytes)


def test_records_and_tokens_give_the_same_transitions():
    rng = random.Random(5)
    samples = random_signal(rng, 12000, "hold")
    values, starts = runs_of(samples)
    records = array("I")                                     # レコード形式: 値 + 繰り返し − 1（65536 で閉じる）
    for v, s, e in zip(values, starts, starts[1:] + [len(samples)]):
        left = e - s
        while left:
            take = min(left, 65536)
            records.append(v | take - 1 << 16)
            left -= take
    records.extend([0] * (-len(records) % 4))                # 拍に揃える（末尾の端数は値 0 の 1 サンプル。比較は先頭の部分）
    rv, rs, _ = decode_transitions(records.tobytes(), True)
    tv, ts, _ = decoded(T.encode_tokens(samples))[:3]
    assert list(rv)[:len(tv)] == tv and list(rs)[:len(ts)] == ts


# --- バイト数 --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("delta, size", [(1, 1), (6, 1), (7, 2), (70, 2), (71, 3), (8198, 3), (8199, 4), (1048582, 4),
                                         (1048583, 5)])
def test_bytes_per_change_follows_the_design_table(delta, size):
    """1 本の ch が delta サンプルごとに反転: 1 変化あたりのバイト数（設計書 §3.2・§2.3）。先頭の VALUE（4 B）と最後の NOP を除く。"""
    changes = 2 if delta > 100000 else 6
    samples = level_samples([(v & 1, delta) for v in range(changes + 1)])   # 0, 1, 0, ... を delta ずつ
    data = T.encode_tokens(samples)
    d = T.decode_tokens(data)
    assert len(d.values) == changes + 1
    assert len(data) - d.pad_bytes == 4 + changes * size


def test_delta_zero_toggles_in_one_group_are_one_byte_each():
    samples = [0, 0b111]                                      # 同じサンプルで 3 ch: TOGGLE を 3 個（1 B ずつ）
    data = T.encode_tokens(samples)
    assert data[:4] == bytes([0x0F, 0xC0, 0x80, 0x80]) and data[4:7] == bytes([0x01, 0x08, 0x10])
    assert decoded(data)[:2] == ([0, 7], [0, 1])


@pytest.mark.parametrize("kind", ["every", "k3", "hold"])
def test_never_more_than_four_bytes_per_sample(kind):
    rng = random.Random(3)
    n = 5000
    samples = random_signal(rng, n, kind) if kind != "hold" else [rng.randrange(1 << 16) if rng.random() < .5 else rng.randrange(8) for _ in range(n)]
    data = T.encode_tokens(samples)
    d = T.decode_tokens(data)
    assert len(data) - d.pad_bytes <= 4 * n
    assert len(data) <= 4 * n + BEAT - 1


def test_k_three_is_three_toggles_and_k_four_is_value():
    three = T.encode_tokens([0, 0b1011])
    four = T.encode_tokens([0, 0b11011])
    assert T.decode_tokens(three).tokens == 1 + 3
    assert T.decode_tokens(four).tokens == 2                # VALUE 2 個（先頭と k = 4）
    assert decoded(three)[:2] == ([0, 0b1011], [0, 1]) and decoded(four)[:2] == ([0, 0b11011], [0, 1])


# --- 拍・NOP・tick・欠け -----------------------------------------------------------------------------------

def test_a_group_never_crosses_a_beat_and_is_padded_with_nop():
    """3 B の束（SCK + MOSI + MISO）を並べると、1 拍に 5 個（15 B）+ NOP 1 個。"""
    samples = [0]
    v = 0
    for _ in range(40):
        v ^= 0b111
        samples.append(v)
    data = T.encode_tokens(samples)
    d = T.decode_tokens(data)
    assert d.pad_bytes > 0 and len(data) == BEAT * (len(data) // BEAT)
    for b in range(0, len(data), BEAT):
        beat = data[b:b + BEAT]
        assert beat[0] < 0x80                               # 拍の先頭は頭
    assert len(decoded(data)[0]) == 41


def test_tick_flushes_partial_beats_and_reports_time_with_wait():
    samples = [0, 1] + [1] * 5000 + [0] + [0] * 3000
    plain = T.encode_tokens(samples)
    ticked = T.encode_tokens(samples, tick_samples=1000)
    pv, ps, pd = decoded(plain)
    tv, ts, td = decoded(ticked)
    assert (tv, ts) == (pv, ps) == runs_of(samples)
    assert td.end_time > pd.end_time and td.end_time >= len(samples) - 1000 - 1   # WAIT で時刻が進んだ
    assert td.pad_bytes > pd.pad_bytes and len(ticked) > len(plain)                 # 端数の拍を NOP で埋めて出した
    assert T.expand_samples(ticked) == array("H", samples[:td.end_time + 1])


def test_resync_value_after_a_gap_restarts_decoding():
    rng = random.Random(9)
    samples = random_signal(rng, 6000, "hold")
    r = 3000
    data = T.encode_tokens(samples, resync_at={r})
    nbeats = len(data) // BEAT
    for b in range(1, nbeats):                              # 再提示の VALUE から始まる拍
        prefix = T.decode_tokens(data[:b * BEAT])
        try:
            rest = T.decode_tokens(data[b * BEAT:], first=prefix.end_time, previous=None)
        except T.TokenError:                                 # VALUE から始まらない拍
            continue
        if len(rest.starts) and int(rest.starts[0]) == r:
            break
    else:
        pytest.fail("再提示の拍が見つからない")
    assert b >= 2
    lost = T.decode_tokens(data[b * BEAT:], first=prefix.end_time, previous=None)    # b 拍までを捨てても続きは復号できる
    tv, ts = runs_of(samples[r:])
    got = ([int(v) for v in lost.values], [int(s) for s in lost.starts])
    assert got == (tv, [s + r for s in ts])                  # 欠けの直後に必ず変化点（値の提示）。以降は元と一致
    full = decoded(data)
    assert full[2].end_time == lost.end_time


def test_long_idle_is_split_with_wait():
    n = 1 << 26
    data = T.encode_tokens(level_samples([(1, 1), (0, 1)]))          # 小さい値の確認 + 下は長い間隔
    assert decoded(data)[:2] == ([1, 0], [0, 1])
    assert T.decode_tokens(T.encode_tokens(level_samples([(0, 1), (1, n)]))).end_time == 1


# --- 誤りの報告 -------------------------------------------------------------------------------------------

def pad_beat(tokens: bytes) -> bytes:
    return tokens + bytes([T.NOP]) * (-len(tokens) % BEAT)


def test_errors_are_reported_not_skipped():
    value0 = bytes([0x0F, 0xC0, 0x80, 0x80])                 # VALUE 0, Δ = 0
    ok = pad_beat(value0)
    assert decoded(ok)[:2] == ([0], [0])
    with pytest.raises(T.TokenError, match="倍数"):
        T.decode_tokens(ok[:-1])
    with pytest.raises(T.TokenError, match="先頭が尾"):
        T.decode_tokens(pad_beat(bytes([0x80]) + value0))
    with pytest.raises(T.TokenError, match="REPEAT の対象"):
        T.decode_tokens(pad_beat(value0 + bytes([0x17, 0xC0])))   # cccc = 2: 直前は VALUE（繰り返せない）
    with pytest.raises(T.TokenError, match="予約"):
        T.decode_tokens(pad_beat(value0 + bytes([0x1F, 0xC0])))   # cccc = 3
    with pytest.raises(T.TokenError, match="尾がある"):
        T.decode_tokens(pad_beat(value0 + bytes([0x01, 0x80])))   # TOGGLE（間隔の欄 1）の後ろに尾
    with pytest.raises(T.TokenError, match="尾が"):
        T.decode_tokens(pad_beat(value0 + bytes([0x07]) + bytes([0x80] * 7)))
    with pytest.raises(T.TokenError, match="VALUE でない"):
        T.decode_tokens(pad_beat(bytes([0x01])))                  # 先頭が VALUE でない
    with pytest.raises(T.TokenError, match="チャンネル数 8"):
        T.decode_tokens(pad_beat(bytes([0x0F, 0xC0, 0x80, 0x80, 0x49])), width=8)    # ch 9 の TOGGLE
    with pytest.raises(T.TokenError, match="8 ビットに収まらない"):
        T.decode_tokens(pad_beat(bytes([0x0F, 0xFF, 0x87, 0x80])), width=8)   # VALUE 0x1FF


@pytest.mark.parametrize("use_numpy", [True, False])
def test_errors_match_in_both_decoders(use_numpy, monkeypatch):
    if not use_numpy:
        monkeypatch.setattr(T, "_np", None)
    value0 = bytes([0x0F, 0xC0, 0x80, 0x80])
    for bad, text in [(pad_beat(bytes([0x80]) + value0), "先頭が尾"), (pad_beat(value0 + bytes([0x17, 0xC0])), "REPEAT の対象"),
                      (pad_beat(bytes([0x01])), "VALUE でない")]:
        with pytest.raises(T.TokenError, match=text):
            T.decode_tokens(bad)
    with pytest.raises(T.TokenError, match="拍をまたいで"):                       # 続きのチャンクの先頭の間隔が 0
        T.decode_tokens(pad_beat(bytes([0x08])), first=5, previous=0)


def test_all_nop_chunk_changes_nothing():
    d = T.decode_tokens(bytes([T.NOP]) * BEAT, first=9, previous=3)
    assert len(d.values) == 0 and (d.end_time, d.last_value, d.pad_bytes, d.tokens) == (9, 3, BEAT, 0)
    assert T.token_extent(bytes([T.NOP]) * BEAT, 9, 3) == (0, 3, BEAT)


# --- REPEAT -------------------------------------------------------------------------------------------

def clock(n, half, ch=3):
    return [(i // half & 1) << ch for i in range(n)]


def test_steady_clock_costs_almost_nothing_with_repeat():
    samples = clock(2_000_000, 4)                              # 12.5 MHz 相当（半周期 4）: 50 万変化
    plain = T.encode_tokens(samples)
    packed = T.encode_tokens(samples, repeat=True)
    d = T.decode_tokens(packed)
    assert d.repeat_tokens >= 1 and len(d.values) == 500_000
    assert len(plain) - T.decode_tokens(plain).pad_bytes - 4 == 499_999 * 1               # repeat なし: 1 変化 1 バイト
    assert len(packed) / len(d.values) < 0.001                                      # ほぼ 0 バイト / 変化
    assert (list(d.values), list(d.starts)) == runs_of(samples)


def test_duty_not_fifty_percent_uses_two_token_pattern():
    samples = [(i % 4 == 3) << 5 for i in range(40000)]         # 低 3、高 1 を繰り返す
    data = T.encode_tokens(samples, repeat=True)
    d = T.decode_tokens(data)
    assert d.repeat_tokens >= 1 and len(data) <= 5 * BEAT
    assert (list(d.values), list(d.starts)) == runs_of(samples)


def test_jittery_clock_gets_no_repeat_and_round_trips():
    steps = [4 + i % 50 for i in range(300)]                    # 間隔が毎回違う（同じ束が続かない）
    samples, v = [], 0
    for h in steps:
        samples += [v] * h
        v ^= 1 << 3
    plain = T.encode_tokens(samples)
    packed = T.encode_tokens(samples, repeat=True)
    assert packed == plain and T.count_repeats(packed) == 0
    rng = random.Random(1)                                       # 乱数のゆらぎ（半周期 4〜39）: ほぼ付かず、必ず無損失・大きくならない
    samples, v = [], 0
    for _ in range(20000):
        samples += [v] * rng.randrange(4, 40)
        v ^= 1 << 3
    packed = T.encode_tokens(samples, repeat=True)
    d = T.decode_tokens(packed)
    assert (list(d.values), list(d.starts)) == runs_of(samples)
    assert len(packed) <= len(T.encode_tokens(samples))
    assert d.repeat_tokens < 0.01 * len(d.values)


@pytest.mark.parametrize("seed", range(6))
def test_repeat_round_trip_with_tick_chunks_and_both_decoders(seed, monkeypatch):
    rng = random.Random(seed)
    samples, v = [], 0
    while len(samples) < 40000:                                  # 規則的なクロックの区間と不規則な区間を混ぜる
        half = rng.choice([1, 2, 3, 4, 6, 7, 40])
        runs = rng.choice([2, 5, 30, 400, 3000])
        for _ in range(runs):
            samples += [v] * half
            v ^= 1 << rng.choice([3, 3, 3, 10])
        samples += [v] * rng.randrange(1, 100)
    kw = {"tick_samples": rng.choice([None, 500, 4000])}
    data = T.encode_tokens(samples, repeat=True, **kw)
    want = runs_of(samples)
    d = T.decode_tokens(data)
    assert (list(d.values), list(d.starts)) == want and d.repeat_tokens > 0
    nbeats = len(data) // BEAT
    first, previous, values, starts = 0, None, [], []
    for b in range(0, nbeats, 3):                                # 3 拍ずつにつないでも同じ（対象は同じ拍の中）
        piece = data[b * BEAT:(b + 3) * BEAT]
        c = T.decode_tokens(piece, first, previous)
        assert T.token_extent(piece, first, previous) == (c.end_time - first, c.last_value, c.pad_bytes)
        values += [int(x) for x in c.values]
        starts += [int(x) for x in c.starts]
        first, previous = c.end_time, c.last_value
    assert (values, starts) == want
    ext = T.token_extent(data)
    monkeypatch.setattr(T, "_np", None)
    q = T.decode_tokens(data)
    assert ([int(x) for x in q.values], [int(x) for x in q.starts]) == want
    assert (q.end_time, q.last_value, q.pad_bytes, q.tokens, q.repeat_tokens) == (d.end_time, d.last_value, d.pad_bytes, d.tokens, d.repeat_tokens)
    assert T.token_extent(data) == ext


def test_repeat_is_never_split_from_its_target_by_a_beat():
    samples = clock(100000, 3) + clock(77, 5)
    for tick in (None, 300):
        data = T.encode_tokens(samples, repeat=True, tick_samples=tick)
        for b in range(0, len(data), BEAT):                      # 1 拍ずつ単独で復号できる（対象が拍の中にある）
            beat = data[b:b + BEAT]
            T.decode_tokens(beat, first=0, previous=0) if b else T.decode_tokens(beat)


def test_repeat_errors():
    value0 = bytes([0x0F, 0xC0, 0x80, 0x80])
    toggle = bytes([0x1C])                                       # ch 3、間隔 4
    ok = pad_beat(value0 + toggle + bytes([0x17, 0xC0 | 2]))     # 直前の 1 個を 2 回（P = 0 + 2 × 1）
    assert decoded(ok)[:2] == ([0, 8, 0, 8], [0, 4, 8, 12])
    with pytest.raises(T.TokenError, match="対象"):              # B = 2 だが対象が 1 つしか無い
        T.decode_tokens(pad_beat(value0 + toggle + bytes([0x17, 0xC0 | 1])))
    with pytest.raises(T.TokenError, match="対象"):              # 間隔 0 のトークン（束の途中）
        T.decode_tokens(pad_beat(value0 + bytes([0x1C, 0x18, 0x17, 0xC0])))
    two = pad_beat(value0)[:16]
    with pytest.raises(T.TokenError, match="対象"):              # 対象が前の拍にある
        T.decode_tokens(two + pad_beat(bytes([0x17, 0xC0])), first=0, previous=None)


# --- 速い復号（スレッドで分ける・型を絞る）が、純 Python の参照と完全に一致する ----------------------------------------

def _mixed_samples(rng, n):
    """間隔が 1 バイト・2 バイト・長い WAIT 級、束（同じサンプルで複数 ch）・VALUE（4 ch 以上）が混ざるサンプル列。"""
    v, samples = 0, []
    for _ in range(n):
        r = rng.random()
        gap = 1 + (rng.randrange(6) if r < 0.6 else rng.randrange(80) if r < 0.9 else rng.randrange(20000))
        samples += [v] * gap
        if rng.random() < 0.05:
            v = rng.randrange(1 << 16)
        else:
            for _ in range(rng.choice([1, 1, 1, 2, 3])):
                v ^= 1 << rng.randrange(16)
        samples.append(v)
    return samples


def _result(d):
    return ([int(x) for x in d.values], [int(x) for x in d.starts], d.end_time, d.last_value, d.pad_bytes, d.tokens, d.repeat_tokens)


@pytest.mark.parametrize("seed", range(8))
def test_threaded_decode_equals_serial_and_pure_python(seed, monkeypatch):
    rng = random.Random(seed)
    data = T.encode_tokens(_mixed_samples(rng, 3000), repeat=bool(seed & 1), tick_samples=rng.choice([None, 40, 3000]))
    monkeypatch.setattr(T, "PARALLEL_MIN_BYTES", 64)               # 分ける道を小さな塊でも通す
    monkeypatch.setattr(T, "PIECE_MIN_BYTES", 32)
    cases = [(0, None, data), (123456, 0x5A5A, data[BEAT:]), (7, 0, data[BEAT * 3:])]    # 先頭から・続きのチャンクとして
    for first, previous, chunk in cases:
        if previous is not None:
            try:
                T.decode_tokens(chunk, first, previous, threads=1)
            except T.TokenError:                                   # 束が拍の境をまたぐ位置では始められない（形式の約束）
                continue
        want = _result(T.decode_tokens(chunk, first, previous, threads=1))
        ext = T.token_extent(chunk, first, previous, threads=1)
        for threads in (2, 3, 8):
            assert _result(T.decode_tokens(chunk, first, previous, threads=threads)) == want
            assert T.token_extent(chunk, first, previous, threads=threads) == ext
        assert _result(T.decode_tokens(chunk, first, previous)) == want         # 既定のスレッド数
        with monkeypatch.context() as m:
            m.setattr(T, "_np", None)
            assert _result(T.decode_tokens(chunk, first, previous)) == want       # 参照（純 Python）
            assert T.token_extent(chunk, first, previous) == ext


def test_threaded_decode_reports_the_same_error_as_serial(monkeypatch):
    monkeypatch.setattr(T, "PARALLEL_MIN_BYTES", 64)
    monkeypatch.setattr(T, "PIECE_MIN_BYTES", 32)
    data = bytearray(T.encode_tokens(_mixed_samples(random.Random(5), 2000)))
    assert len(data) > BEAT * 40
    at = len(data) - BEAT * 3                                      # 後ろの部分に不正（TOGGLE の後ろに尾）を埋め込む
    data[at:at + 2] = bytes([0x01, 0x80])
    msgs = set()
    for threads in (1, 4):
        with pytest.raises(T.TokenError) as e:
            T.decode_tokens(bytes(data), threads=threads)
        msgs.add(str(e.value))
        with pytest.raises(T.TokenError) as e:
            T.token_extent(bytes(data), threads=threads)
        msgs.add(str(e.value))
    assert len(msgs) == 1


def test_all_nop_pieces_before_the_first_event_do_not_start_the_stream_early(monkeypatch):
    monkeypatch.setattr(T, "PARALLEL_MIN_BYTES", 64)
    monkeypatch.setattr(T, "PIECE_MIN_BYTES", 32)
    nops = bytes([T.NOP]) * (BEAT * 6)
    d = T.decode_tokens(nops + T.encode_tokens([1, 1, 3, 3, 2]), threads=4)
    assert d.pad_bytes >= len(nops) and [int(x) for x in d.values] == [1, 3, 2]
    with pytest.raises(T.TokenError, match="VALUE でない"):        # 先頭が VALUE でない流れは、NOP の部分をいくつ挟んでも誤り
        T.decode_tokens(nops + pad_beat(bytes([0x01])), threads=4)


def test_many_repeats_expand_like_the_run_length_reference():
    samples = clock(5000, 3) + clock(400, 5) + clock(33, 2)
    data = T.encode_tokens(samples, repeat=True)
    assert T.count_repeats(data) > 0
    d = T.decode_tokens(data, threads=1)
    assert (list(d.values), list(d.starts)) == runs_of(samples)
