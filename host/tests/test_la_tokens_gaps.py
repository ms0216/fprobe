"""RLE のトークン形式の欠けの区間: 回路（la_stream_gap のトークン形式）が出す記録と同じ形（位置 b・長さ q = 捨てた最後の拍の最後の出来事の
時刻 − 直前に出した拍の最後の出来事の時刻）を、ホストの GapPlacer（トークン形式の StreamCursor）が置いて復号できること。
再提示の拍（先頭が VALUE の拍）から出し直すので、欠けの後は直前の値が無くても復号でき、時刻は q で続く。
回路側の試験は gateware/tests/tb/la_stream_gap_tb.py・la_deep_buffer_tok_tb.py。仕様: docs/superpowers/specs/2026-11-07-la-rle2-design.md §3.6"""

import bisect
import random

import pytest

from fprobe import la_tokens as T
from fprobe.la_gaps import GapPlacer, StreamCursor
from test_la_tokens import random_signal

HOLD = 64 << 20
VALUE_HEAD = (T.KIND_VALUE << 3 | T.DELTA_EXT)                 # 0x0F: 拍の先頭が VALUE


def beats_and_positions(data):
    """拍の列と、拍ごとの「最後の出来事の時刻」（回路が拍に付ける in_pos と同じ）。"""
    beats = [data[i:i + 16] for i in range(0, len(data), 16)]
    first, prev, pos = 0, None, []
    for b in beats:
        d = T.decode_tokens(b, first, prev)
        first, prev = d.end_time, d.last_value
        pos.append(first)
    return beats, pos


def value_at(points, t):
    """変化点の列（時刻の昇順）から、時刻 t の値。"""
    k = bisect.bisect_right(points, (t, 1 << 20)) - 1
    return points[k][1] if k >= 0 else None


def run_case(samples, drops, *, resync_at=()):
    data = T.encode_tokens(samples, resync_at=resync_at)
    beats, pos = beats_and_positions(data)
    whole = T.decode_tokens(data)
    orig = list(zip((int(t) for t in whole.starts), (int(v) for v in whole.values)))
    placer = GapPlacer(True, 0, hold_bytes=HOLD, cursor=StreamCursor(True, 0, "tokens"))
    kept, records, last, dropped = [], [], 0, 0
    for i, j in drops:                                          # 拍 i〜j−1 を捨てる。拍 j は VALUE から始まる
        assert beats[j][0] == VALUE_HEAD
        kept.append(b"".join(beats[last:i]))
        records.append((i - dropped, pos[j - 1] - pos[i - 1], 0))      # 位置 b = その前に出した拍の数（捨てた拍は数えない）
        dropped += j - i
        last = j
    kept.append(b"".join(beats[last:]))
    # 回路と同じ順: 記録は拍より先に（位置 b = その前に出した拍の数）届く。置く側は拍の数と記録の件数で決める
    for data_part in kept:
        placer.add_beats(data_part)
    for rec in records:
        placer.add_record(*rec)
    pieces = placer.finish(len(records), records[-1][0])
    return orig, pos, pieces, placer


@pytest.mark.parametrize("kind", ["hold", "every", "k3", "sparse"])
def test_points_after_a_gap_continue_in_time_by_q(kind):
    rng = random.Random(5)
    samples = list(random_signal(rng, 400000 if kind == "sparse" else 40000, kind))
    resync_at = [len(samples) // 4, len(samples) // 2, 3 * len(samples) // 4]            # 再提示の拍（先頭が VALUE の拍）を作る
    data = T.encode_tokens(samples, resync_at=resync_at)
    beats, pos = beats_and_positions(data)
    starts = [j for j, b in enumerate(beats) if j > 8 and b[0] == VALUE_HEAD]
    assert len(starts) >= 2, kind
    j1, j2 = starts[0], starts[len(starts) // 2]
    drops = [(j1 - 5, j1), (j2 - 1, j2)]
    orig, pos, pieces, placer = run_case(samples, drops, resync_at=resync_at)
    assert placer.unplaced_beats == 0 and len(placer.gaps) == 2
    got, windows = [], []
    for piece in pieces:
        if piece[0] == "gap":
            windows.append((piece[1].start, piece[1].start + piece[1].samples))
            continue
        _, part, first, previous, _ = piece
        d = T.decode_tokens(bytes(part), first, previous)
        got += [(int(t), int(v)) for t, v in zip(d.starts, d.values)]
    assert windows == [(pos[j1 - 6], pos[j1 - 1]), (pos[j2 - 2], pos[j2 - 1])]
    # 欠けの窓の外の点は元の復号と同じ。欠けの後の最初の点は再提示の VALUE（元の値と同じ時刻・値）
    for (lo, hi) in windows:
        assert not any(lo < t <= hi for t, _ in got)
    inside = lambda t: any(lo < t <= hi for lo, hi in windows)            # noqa: E731
    expected = [(t, v) for t, v in orig if not inside(t)]
    known = set(expected)
    after_gap = [pt for pt in got if pt not in known]                      # 元の変化点に無い点 = 変化でない再提示の点
    assert len(after_gap) <= len(windows)
    for t, v in after_gap:
        assert v == value_at(orig, t) and any(hi < t for _, hi in windows), (t, v)
    assert [pt for pt in got if pt in known] == expected
    # 欠けの後の値はすべて元の値
    assert all(v == value_at(orig, t) for t, v in got)


def test_the_cursor_counts_gap_samples_as_time_and_keeps_extent():
    rng = random.Random(6)
    samples = random_signal(rng, 20000, "hold")
    resync_at = [9000]
    data = T.encode_tokens(samples, resync_at=resync_at)
    beats, pos = beats_and_positions(data)
    j = next(j for j, b in enumerate(beats) if j > 30 and b[0] == VALUE_HEAD)
    orig, pos, pieces, placer = run_case(samples, [(j - 3, j)], resync_at=resync_at)
    cursor = placer.cursor
    # 位置 = 最後の出来事の時刻（欠けの q を含む）。値のあるサンプルの数 = 位置 − 欠けのサンプル + 1
    assert cursor.position == pos[-1]
    assert cursor.gap_samples == pos[j - 1] - pos[j - 4]
    assert cursor.samples == cursor.position - cursor.gap_samples + 1
