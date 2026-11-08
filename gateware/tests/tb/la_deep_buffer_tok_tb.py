"""la_deep_buffer の RLE のトークン形式（RLE_TOKENS = true、control.rle_fmt = 1）の、端子から PS の読みまでの端から端のテストベンチ。
ADDR_BITS = 12、STREAM_EVENTS・STREAM_GAPS = true、PS が DDR のリングを直接読む流れ。仕様: docs/superpowers/specs/2026-11-07-la-rle2-design.md §3.6

入力はサンプルの位置の関数 f。PS の役が読んだロジアナの拍と欠けの記録（イベント）を、ホストと同じ GapPlacer（トークン形式の StreamCursor）で置き、
復号した変化点（fprobe.la_tokens.decode_tokens）を f と突き合わせる:
  - 変化点の値は f の値と一致する（欠けの後の最初の点は再提示の VALUE。変化でなくても点になる）
  - 欠けをまたがない隣り合う変化点の間は、f が一定（変化を取りこぼさない）
  - 欠けの長さ q が違えば、後ろの全部の変化点の時刻がずれて合わない。"""

import os

import cocotb
from cocotb.triggers import ClockCycles
from fprobe.la_gaps import GapPlacer, StreamCursor
from fprobe.la_tokens import decode_tokens
from tests.tb.la_deep_buffer_gap_tb import Input, Ps, run_stream, setup


def f_mixed(i):
    """12.5 MHz のクロック(ch0) + ゆっくりの信号(ch3) + 29 サンプルごとに最大 5 本が同時に変わる(ch5〜9)。"""
    h = (i // 29) * 2654435761 & 0xFFFFFFFF
    burst = (h >> 7 & 0x1F) << 5
    return ((i // 4) & 1) | (((i // 97) & 1) << 3) | burst


def f_busy(i):
    """毎サンプル数本〜十本が変わる（1 周期に最大 4 バイトの束 → 4 周期に 1 拍）。"""
    return i * 0x5B5B & 0xFFFF


def token_pieces(ps):
    """Ps が集めた pieces → (変化点 [(時刻, 値)], 欠けの窓 [(開始, 終わり)])。"""
    points, windows = [], []
    for piece in ps.pieces:
        if piece[0] == "gap":
            windows.append((piece[1].start, piece[1].start + piece[1].samples))
            continue
        _, data, first, previous, _ = piece
        d = decode_tokens(bytes(data), first, previous)
        points += [(int(t), int(v)) for t, v in zip(d.starts, d.values)]
    return points, windows


def check_points(points, windows, f):
    """変化点と f の突き合わせ。起点の offset は値と「欠けをまたがない隣り合う点の間は f が一定」で決める。
    先頭の 64 サンプルは見ない（開始の直後の入力を置く前の値・同期化の段）。"""
    points = [(t, v) for t, v in points if t >= 64]
    assert points, "変化点が無い"
    t0, v0 = points[0]
    offsets = [o for o in range(-16, 400) if f(t0 + o) == v0]
    last = None
    for o in offsets:
        bad = [(t, v, f(t + o)) for t, v in points if f(t + o) != v]
        if bad:
            last = ("値", o, bad[:3], len(bad))
            continue
        for (t1, v1), (t2, _) in zip(points, points[1:]):
            if any(a >= t1 and b < t2 for a, b in windows):      # 欠けの窓（最後の出来事の時刻の後〜再開の VALUE の前）をまたぐ
                continue
            step = max(1, (t2 - t1) // 50)
            if not (all(f(t + o) == v1 for t in range(t1, t2, step)) and f(t2 - 1 + o) == v1):
                last = ("一定でない", o, t1, t2, v1)
                break
        else:
            return o
    raise AssertionError(f"どの起点でも合わない: 最初 {points[:4]}、最後に調べた起点の不一致 {last}")


async def tok_setup(dut, **kw):
    mem, lanes = await setup(dut, **kw)
    dut.rle_fmt.value = 1
    dut.rle_repeat.value = int(os.environ.get("LDB_REPEAT") == "1")      # 1 = REPEAT を使う（generic TOK_REPEAT_EN = true の構成）
    dut.tick.value = 0
    return mem, lanes


class TokPs(Ps):
    """Ps のトークン形式版: GapPlacer のサンプルの数えを、出来事の時刻で進める StreamCursor にする。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.placer = GapPlacer(True, 0, hold_bytes=64 << 20, cursor=StreamCursor(True, 0, "tokens"))


async def run_tok(dut, mem, lanes, *, cycles, f, tick=0, stall=lambda c: False, ev_every=3, gaps=1):
    import tests.tb.la_deep_buffer_gap_tb as gt
    orig = gt.Ps
    gt.Ps = TokPs                                             # run_stream が作る Ps をトークン形式のものにする
    try:
        dut.tick.value = tick
        return await run_stream(dut, mem, lanes, cycles=cycles, f=f, rle=1, stall=stall, ev_every=ev_every, gaps=gaps)
    finally:
        gt.Ps = orig


@cocotb.test()
async def tokens_without_gaps_end_to_end(dut):
    mem, lanes = await tok_setup(dut)
    ps, reported = await run_tok(dut, mem, lanes, cycles=20_000, f=f_mixed)
    assert int(dut.overrun.value) == 0
    assert reported == 0 and ps.placer.unplaced_beats == 0
    points, windows = token_pieces(ps)
    assert not windows and len(points) > 2000
    check_points(points, windows, f_mixed)
    assert int(dut.tok_pad_bytes.value) >= 0


@cocotb.test()
async def tokens_with_tick_deliver_quiet_signals(dut):
    """変化のほとんどない信号でも tick で拍が出る（変化 1 つだけの記録が、tick 無しだと拍が満ちず届かない）。"""
    mem, lanes = await tok_setup(dut)
    f = lambda i: 1 if (i // 3000) % 2 else 0
    ps, reported = await run_tok(dut, mem, lanes, cycles=20_000, f=f, tick=500)
    assert int(dut.overrun.value) == 0
    points, windows = token_pieces(ps)
    assert len(points) >= 5, points
    check_points(points, windows, f)
    assert int(dut.tok_pad_bytes.value) > 0                     # tick で NOP を埋めた


@cocotb.test()
async def tokens_ps_stall_makes_gaps_resynced_by_value(dut):
    """PS が読まない間にリングの未読が迫ると拍を捨てる。欠けの後は符号器が値を再提示し、時刻は q で続く。"""
    mem, lanes = await tok_setup(dut)
    ps, reported = await run_tok(dut, mem, lanes, cycles=90_000, f=f_busy,
                                 stall=lambda c: 2_000 <= c < 60_000)
    assert int(dut.overrun.value) == 0
    points, windows = token_pieces(ps)
    assert windows, "欠けが起きなかった（試験の前提）"
    assert reported == ps.placer._records and ps.placer.unplaced_beats == 0
    assert all(b > a for a, b in windows)
    assert points[-1][0] > windows[-1][1], "欠けの後に変化点が続かない"
    check_points(points, windows, f_busy)


@cocotb.test()
async def tokens_slow_writer_makes_gaps_without_stopping(dut):
    """メモリの書き込みが符号器に追いつかない（毎サンプル 3 本変化 = 5 周期に 1 拍、メモリの待ち 90 %）。"""
    mem, lanes = await tok_setup(dut, mem_stall=0.9)
    ps, reported = await run_tok(dut, mem, lanes, cycles=30_000, f=f_busy, ev_every=1)
    assert int(dut.overrun.value) == 0
    points, windows = token_pieces(ps)
    assert len(windows) >= 2
    assert reported == ps.placer._records and ps.placer.unplaced_beats == 0
    check_points(points, windows, f_busy)
