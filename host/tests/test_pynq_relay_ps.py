"""PYNQ-Z2 の中継の「PS が DDR のリングを直接読む」ストリーム（relay.c の relay_ps_*）。
仕様: docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md §3.4

PL は Python の模型（窓のレジスタ + DDR の配列）。中継が作るバイト列は、PL の la_deep_buffer が窓へ流す形
（見出し 1 拍 + 中身。fprobe.la.split_segments が解く形）と同じでなければならない。"""
import ctypes

import pytest

from fprobe import generated as g
from fprobe.la import split_segments
from tests.pynq_window import M32, WindowModel
from tests.test_pynq_relay import (BUNDLE, CH_STREAM, ID, VERSION, PlModel, Stats, from_pc, lib)  # noqa: F401

MEM_BYTES = 1024 * 16                                    # 1024 拍: イベントのリング 64 拍、ロジアナのリング 960 拍
LA_RING, EV_RING = 960, 64
LA_WRITTEN, STREAM_EPOCH, LA_CONSUMED, STREAM_BEATS, POS_TAG = (
    g.WIN_LA_WRITTEN, g.WIN_STREAM_EPOCH, g.WIN_LA_CONSUMED, g.WIN_STREAM_BEATS, g.WIN_POS_TAG)
RUNNING, OVERRUN, READING = 1, 2, 4                      # PsPlModel.status のビット（模型の書き方）


class PsPlModel(WindowModel):
    """窓（版 4）と、DDR（深いバッファの領域）の模型。"""
    LA_RING, EV_RING, MEM_BYTES = LA_RING, EV_RING, MEM_BYTES

    def __init__(self):
        super().__init__()
        self.ddr = bytearray(MEM_BYTES)

    def la_beat(self, n):
        return bytes([n & 0xFF]) * 8 + ((n % (1 << 64)).to_bytes(8, "little"))   # 拍の中身: 通し番号が分かる 16 バイト

    def write_la(self, first, count):
        """ロジアナのリングに、通し番号 first から count 拍を書く（PL の書き込み。番号は 32 ビットで折り返す）。"""
        for n in range(first, first + count):
            pos = self.la_pos(n % M32)
            self.ddr[pos * 16:pos * 16 + 16] = self.la_beat(n)
        self.la_written = (first + count) % M32

    def write_event(self, k, value):
        pos = LA_RING + k % EV_RING
        self.ddr[pos * 16:pos * 16 + 16] = value.to_bytes(16, "little")


class PsApi:
    def __init__(self, lib):
        self.lib = lib
        lib.relay_ps_size.restype = ctypes.c_size_t
        lib.relay_ps_init.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.relay_ps_init.restype = ctypes.c_int
        lib.relay_ps_step.argtypes = [ctypes.c_void_p, ctypes.POINTER(type(PlModel().ops)), ctypes.c_char_p,
                                      ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(Stats), ctypes.c_int64]
        lib.relay_ps_step.restype = ctypes.c_size_t
        self.state = ctypes.create_string_buffer(lib.relay_ps_size())
        assert lib.relay_ps_init(self.state, MEM_BYTES) == 0
        self.stats = Stats()

    def step(self, pl, now=0, cap=4096):
        out = ctypes.create_string_buffer(cap)
        n = self.lib.relay_ps_step(self.state, ctypes.byref(pl.ops), bytes(pl.ddr), out, cap,
                                   ctypes.byref(self.stats), now)
        return out.raw[:n]

    def drain(self, pl, now=0, cap=4096, limit=100):
        """何も出なくなるまで呼び、データグラムの列を返す。"""
        out = []
        for _ in range(limit):
            d = self.step(pl, now, cap)
            if not d:
                break
            out.append(d)
        return out


@pytest.fixture
def api(lib):  # noqa: F811
    return PsApi(lib)


def stream_bytes(datagrams):
    """データグラムの列 → STREAM チャンネルのバイト列と、seq の列（まとめた中のフレームごと）。"""
    data, seqs = bytearray(), []
    for d in datagrams:
        if d[0] == BUNDLE:
            seq, pos = d[1], 2
            while pos < len(d):
                ch, n = d[pos], int.from_bytes(d[pos + 1:pos + 3], "little")
                assert ch == CH_STREAM
                data += d[pos + 3:pos + 3 + n]
                seqs.append(seq)
                seq = (seq + 1) & 0xFF
                pos += 3 + n
        else:
            assert d[0] == CH_STREAM
            data += d[2:]
            seqs.append(d[1])
    return bytes(data), seqs


def request_bundle(api, pl, n):
    assert from_pc(api.lib, pl, bytes([BUNDLE, 0]) + n.to_bytes(2, "little"), api.stats) == 0


def test_the_window_version_4_is_accepted_and_version_1_still_is(lib, api):  # noqa: F811
    lib.relay_check_design.argtypes = [ctypes.POINTER(type(PlModel().ops))]
    assert lib.relay_check_design(ctypes.byref(PsPlModel().ops)) == 0
    assert lib.relay_check_design(ctypes.byref(PlModel().ops)) == 0              # 版 1（STREAM_* の無い PL）
    assert lib.relay_check_design(ctypes.byref(PlModel(ident=1).ops)) < 0


def test_a_ring_size_that_is_not_two_ring_blocks_is_refused(lib, api):  # noqa: F811
    state = ctypes.create_string_buffer(lib.relay_ps_size())
    assert lib.relay_ps_init(state, 0) < 0                                       # 深いバッファの無い PL
    assert lib.relay_ps_init(state, 3 * 16 * 1024) < 0                           # 2 の冪の拍数でない


def test_beats_become_a_segment_with_the_header_the_pl_would_send(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 100)
    data, _ = stream_bytes(api.drain(pl))
    segs = split_segments(data)
    assert len(segs) == 1 and segs[0].kind == 1 and segs[0].seq == 0
    assert segs[0].data == b"".join(pl.la_beat(n) for n in range(100))
    assert pl.la_consumed == 100                                                 # 読み終えた位置を返す


def test_a_chunk_is_at_most_256_beats_and_seq_is_the_beat_number(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 600)
    data, _ = stream_bytes(api.drain(pl))
    segs = split_segments(data)
    assert [(s.seq, len(s.data) // 16) for s in segs] == [(0, 256), (256, 256), (512, 88)]
    assert b"".join(s.data for s in segs) == b"".join(pl.la_beat(n) for n in range(600))


def test_a_chunk_across_the_end_of_the_ring_is_read_in_order(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 900)
    stream_bytes(api.drain(pl))                                                  # 900 拍を読み終える
    pl.write_la(900, 120)                                                        # 960 を超える（リングの終わりをまたぐ）
    data, _ = stream_bytes(api.drain(pl))
    segs = split_segments(data)
    assert b"".join(s.data for s in segs) == b"".join(pl.la_beat(n) for n in range(900, 1020))
    assert segs[0].seq == 900


def test_a_few_beats_wait_until_they_are_old_or_the_writer_has_stopped(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 10)                                                           # 64 拍に満たない
    assert api.step(pl, now=1_000) == b""                                        # 溜まるのを待つ
    assert api.step(pl, now=50_000) == b""
    data, _ = stream_bytes(api.drain(pl, now=200_000))                           # 100 µs を超えた
    assert split_segments(data)[0].data == b"".join(pl.la_beat(n) for n in range(10))
    pl.write_la(10, 5)
    pl.status = READING                                                          # 書き込みが止まった: 待たずに出す
    data, _ = stream_bytes(api.drain(pl, now=200_001))
    assert split_segments(data)[0].data == b"".join(pl.la_beat(n) for n in range(10, 15))


def test_events_are_read_by_position_and_counted_from_the_start(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.ev_start = 60                                                             # 60 番目から（リングの終わりをまたぐ）
    for k in range(10):
        pl.write_event(60 + k, 0xE0000 + k)
    pl.ev_wpos = (60 + 10) % EV_RING
    pl.status = READING
    data, _ = stream_bytes(api.drain(pl))
    segs = split_segments(data)
    assert len(segs) == 1 and segs[0].kind == 2 and segs[0].seq == 60
    assert [int.from_bytes(segs[0].data[i:i + 16], "little") for i in range(0, 160, 16)] == [
        0xE0000 + k for k in range(10)]
    assert pl.ev_consumed == 10                                                  # 開始からの個数


def test_beats_and_events_alternate_when_both_are_waiting(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 700)
    for k in range(30):                                                          # リング（64）の半分（32）未満
        pl.write_event(k, 0xE0000 + k)
    pl.ev_wpos = 30
    data, _ = stream_bytes(api.drain(pl))
    kinds = [s.kind for s in split_segments(data)]
    assert kinds.count(1) == 3 and kinds.count(2) == 1 and kinds[:2] == [1, 2]   # 交互（先頭は 1 拍の側）


def test_a_few_events_go_out_when_old_even_while_the_analyzer_keeps_streaming(api):
    # ロジアナが流れ続けて毎回出せる間も、少しのイベント（欠けの記録など。ev_min に満たない）は古くなったら出す。
    # 出さないと、ホストは欠けの位置が決まるまでロジアナの拍を置けず、流れが止まるまで抱える（2026-11-02 実機: 10 秒の流しで約 11 秒遅れ）
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    for k in range(3):
        pl.write_event(k, 0xE0000 + k)
    pl.ev_wpos = 3
    kinds, la = [], 0
    for step in range(20):                                                       # 10 µs ごとに 100 拍（いつも LA_MIN 以上）
        pl.write_la(la, 100)
        la += 100
        data, _ = stream_bytes(api.drain(pl, now=1_000 + step * 10_000))
        kinds += [s.kind for s in split_segments(data)]
    assert 2 in kinds                                                            # 200 µs のうちに（年齢 100 µs）出た


def test_a_new_stream_epoch_starts_reading_from_the_consumed_position(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 100)
    api.drain(pl)
    assert pl.la_consumed == 100
    pl.epoch += 1                                                                # 新しい流れ: PL が読み終えた位置を 0 に戻す
    pl.la_consumed = pl.ev_consumed = 0
    pl.write_la(0, 80)
    data, _ = stream_bytes(api.drain(pl))
    segs = split_segments(data)
    assert segs[0].seq == 0 and len(segs[0].data) // 16 == 80 and pl.la_consumed == 80


def test_attaching_in_the_middle_of_a_stream_continues_from_the_consumed_position(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 520)
    pl.la_consumed = 500                                                         # 前の中継が 500 拍まで読んでいた
    pl.status = READING                                                          # 書き込みが止まっている: 20 拍でもすぐ出す
    data, _ = stream_bytes(api.drain(pl))
    segs = split_segments(data)
    assert segs[0].seq == 500 and len(segs[0].data) // 16 == 20


def test_nothing_is_read_when_the_pl_is_not_in_ps_mode(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 100)
    pl.mode = 0
    assert api.step(pl) == b"" and pl.la_consumed == 0


def test_a_segment_is_split_into_datagrams_of_the_bundle_size_with_consecutive_seq(api):
    pl = PsPlModel()
    request_bundle(api, pl, 1400)
    pl.write_la(0, 256)
    datagrams = api.drain(pl)
    assert all(len(d) <= 1400 and d[0] == BUNDLE for d in datagrams) and len(datagrams) >= 3
    data, seqs = stream_bytes(datagrams)
    assert seqs == [(seqs[0] + i) & 0xFF for i in range(len(seqs))]
    assert split_segments(data)[0].data == b"".join(pl.la_beat(n) for n in range(256))


def test_without_a_bundle_request_the_stream_goes_in_plain_frames_of_512(api):
    pl = PsPlModel()
    pl.write_la(0, 64)
    datagrams = api.drain(pl)
    assert all(d[0] == CH_STREAM and len(d) <= 2 + 512 for d in datagrams)
    data, _ = stream_bytes(datagrams)
    assert split_segments(data)[0].data == b"".join(pl.la_beat(n) for n in range(64))


def test_the_stream_is_active_while_the_pl_writes_or_the_ps_has_unread_beats(api):
    lib = api.lib
    lib.relay_ps_active.argtypes = [ctypes.c_void_p]
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    api.step(pl)
    assert lib.relay_ps_active(api.state) == 1                                   # 書き込み中・読み出し中
    pl.status = READING                                                          # 書き込みは止まったが、PS が読み終えていない
    api.step(pl)
    assert lib.relay_ps_active(api.state) == 1
    pl.status = 0                                                                # 全部読み終えた
    api.step(pl)
    assert lib.relay_ps_active(api.state) == 0
    pl.status, pl.mode = RUNNING, 0                                              # モードが切れた
    api.step(pl)
    assert lib.relay_ps_active(api.state) == 0


def test_the_relay_tells_how_many_beats_it_decided_to_send_headers_included(api):
    # ホストの LaStream は stream_beats で「必ず届く量」を知る。PS が読む流れでは、中継がそれを窓に書く（見出しの 1 拍を含む）
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 100)
    api.drain(pl)
    assert pl.stream_beats == 101                                                # 見出し 1 + 中身 100
    pl.write_la(100, 600)                                                        # 256 + 256 + 188
    api.drain(pl)
    assert pl.stream_beats == 101 + 600 + 3
    for k in range(30):
        pl.write_event(k, 0xE0000 + k)
    pl.ev_wpos = 30
    pl.status = READING
    api.drain(pl)
    assert pl.stream_beats == 101 + 600 + 3 + 31                                 # イベントのかたまり（見出し 1 + 30 件）


def test_the_count_is_written_before_the_datagrams_are_handed_out_not_after(api):
    # 数を先に書く（ホストが読むのは数 → 届いたバイト。数が後だと、届いたのに「無い」と見て待たせる）
    pl = PsPlModel()
    request_bundle(api, pl, 1400)
    pl.write_la(0, 256)
    first = api.step(pl)
    assert first and pl.stream_beats == 257                                      # 最初のデータグラムを返した時点で、かたまり全体の数は書いてある


def test_attaching_in_the_middle_continues_the_count_from_the_window(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 520)
    pl.la_consumed = 500
    pl.stream_beats = 1000                                                       # 前の中継が出すと決めていた総数
    pl.status = READING
    api.drain(pl)
    assert pl.stream_beats == 1000 + 1 + 20


def test_a_new_epoch_restarts_the_count_from_the_window(api):
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 100)
    api.drain(pl)
    pl.epoch += 1
    pl.la_consumed = pl.ev_consumed = pl.stream_beats = 0                        # 新しい流れの開始で PL が 0 に戻す
    pl.write_la(0, 80)
    api.drain(pl)
    assert pl.stream_beats == 81


# --- TCP で流す（枠なしのバイト列）-----------------------------------------------------------------------------

class PsFill:
    """relay_ps_fill_bytes の呼び口。"""

    def __init__(self, api):
        self.api = api
        api.lib.relay_ps_fill_bytes.argtypes = [ctypes.c_void_p, ctypes.POINTER(type(PlModel().ops)), ctypes.c_char_p,
                                                ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int64]
        api.lib.relay_ps_fill_bytes.restype = ctypes.c_size_t

    def call(self, pl, cap, now=0):
        out = ctypes.create_string_buffer(cap)
        n = self.api.lib.relay_ps_fill_bytes(self.api.state, ctypes.byref(pl.ops), bytes(pl.ddr), out, cap, now)
        return out.raw[:n]


def test_fill_bytes_returns_the_same_stream_as_the_datagrams_without_any_framing(api):
    pl = PsPlModel()
    pl.write_la(0, 600)
    raw = b""
    fill = PsFill(api)
    while True:
        part = fill.call(pl, 1 << 16)
        if not part:
            break
        raw += part
    segs = split_segments(raw)
    assert [(s.kind, s.seq, len(s.data) // 16) for s in segs] == [(1, 0, 256), (1, 256, 256), (1, 512, 88)]
    assert b"".join(s.data for s in segs) == b"".join(pl.la_beat(n) for n in range(600))
    assert pl.la_consumed == 600 and pl.stream_beats == 603


def test_fill_bytes_packs_several_chunks_up_to_the_capacity_and_never_beyond(api):
    pl = PsPlModel()
    pl.write_la(0, 600)
    fill = PsFill(api)
    one_chunk = 16 + 256 * 16
    part = fill.call(pl, 2 * one_chunk + 100)                                    # 2 つ入って、3 つ目は入らない
    assert len(part) == 2 * one_chunk and pl.la_consumed == 512
    part2 = fill.call(pl, 2 * one_chunk + 100)
    assert len(part2) == 16 + 88 * 16 and pl.la_consumed == 600
    assert fill.call(pl, 1 << 16) == b""
    assert fill.call(pl, 10) == b""                                              # 入れ物が 1 つ分にも満たなければ、何も出さない


def test_fill_bytes_reads_the_positions_once_and_returns_the_consumed_position_once_for_all_its_chunks(api):
    # 窓（MMIO）の 1 回は約 2〜3 µs。1 つのかたまり（4 KB）ごとに約 11 回触ると、読み出しの時間の 1/4 以上になる
    pl = PsPlModel()
    pl.write_la(0, 768)
    pl.reads.clear()
    part = PsFill(api).call(pl, 3 * (16 + 256 * 16))                             # 3 つのかたまり
    assert len(part) == 3 * (16 + 256 * 16)
    assert pl.reads.count(STREAM_EPOCH) == 2 and pl.reads.count(LA_WRITTEN) == 2   # 付き始めの 1 回（読み直し）+ 写しの 1 回
    assert pl.writes == [(POS_TAG, 1), (LA_CONSUMED, 768), (STREAM_BEATS, 771)]  # 番号を添え、位置・数を最後に 1 回ずつ
    pl.reads.clear()
    pl.writes.clear()
    pl.write_la(768, 256)
    PsFill(api).call(pl, 3 * (16 + 256 * 16))                                   # 付いた後: 位置のレジスタの読みは 3 回だけ
    assert sorted(pl.reads) == sorted([STREAM_EPOCH, LA_WRITTEN, g.WIN_EV_WPOS])
    assert pl.writes == [(LA_CONSUMED, 1024), (STREAM_BEATS, 1028)]


def test_fill_bytes_follows_the_same_waiting_rules(api):
    pl = PsPlModel()
    pl.write_la(0, 10)
    fill = PsFill(api)
    assert fill.call(pl, 1 << 16, now=1_000) == b""                              # 64 拍に満たない間は溜める
    assert fill.call(pl, 1 << 16, now=200_000) != b""                            # 100 µs を超えた
    pl.write_la(10, 5)
    pl.status = READING
    assert fill.call(pl, 1 << 16, now=200_001) != b""                            # 書き込みが止まったら、待たずに出す


def test_datagrams_and_fill_bytes_share_the_position_so_switching_does_not_repeat_or_skip(api):
    # TCP がつながった・切れたとき、UDP との間で切り替わる。読み位置は 1 つ
    pl = PsPlModel()
    request_bundle(api, pl, 1400)
    pl.write_la(0, 300)
    first = api.drain(pl)
    got, _ = stream_bytes(first)
    pl.write_la(300, 300)
    more = PsFill(api).call(pl, 1 << 16)
    segs = split_segments(got + more)
    assert b"".join(s.data for s in segs) == b"".join(pl.la_beat(n) for n in range(600))


# --- トリガ取り込みの読み出しを PS が DDR から行う（窓の版 3）---------------------------------------------------

class RdApi:
    def __init__(self, api):
        self.api = api
        lib = api.lib
        lib.relay_rd_size.restype = ctypes.c_size_t
        lib.relay_rd_init.argtypes = [ctypes.c_void_p]
        lib.relay_rd_fill.argtypes = [ctypes.c_void_p, ctypes.POINTER(type(PlModel().ops)), ctypes.c_char_p,
                                      ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.relay_rd_fill.restype = ctypes.c_size_t
        self.state = ctypes.create_string_buffer(lib.relay_rd_size())
        lib.relay_rd_init(self.state)

    def fill(self, pl, cap=4096):
        out = ctypes.create_string_buffer(cap)
        n = self.api.lib.relay_rd_fill(self.state, ctypes.byref(pl.ops), bytes(pl.ddr), self.api.state, out, cap)
        return out.raw[:n]

    def read_all(self, pl, cap=4096, limit=1000):
        data = b""
        for _ in range(limit):
            part = self.fill(pl, cap)
            if not part and pl.rd_ack == pl.rd_epoch:
                break
            data += part
        return data


@pytest.fixture
def rd(api):
    return RdApi(api)


def request(pl, region, first, count):
    pl.rd_epoch = (pl.rd_epoch + 1) & 0xFF
    pl.rd_region, pl.rd_first, pl.rd_count = region, first, count


def test_without_a_request_nothing_is_read(rd):
    pl = PsPlModel()
    assert rd.fill(pl) == b"" and pl.rd_ack == 0


def test_a_logic_analyzer_request_returns_those_beats_and_acknowledges(rd):
    pl = PsPlModel()
    pl.write_la(0, 100)
    request(pl, 0, 10, 20)
    data = rd.read_all(pl)
    assert data == b"".join(pl.la_beat(n) for n in range(10, 30))
    assert pl.rd_ack == pl.rd_epoch                                              # 読み終えたので返事（PL の reading が下がる）


def test_a_request_across_the_end_of_the_ring_wraps(rd):
    pl = PsPlModel()
    pl.write_la(0, 960)
    request(pl, 0, 950, 20)
    assert rd.read_all(pl) == b"".join(pl.la_beat(n) for n in list(range(950, 960)) + list(range(0, 10)))


def test_an_event_request_reads_the_event_ring_after_the_analyzer_ring(rd):
    pl = PsPlModel()
    for k in range(10):
        pl.write_event(k, 0xE0000 + k)
    request(pl, 1, 3, 5)
    data = rd.read_all(pl)
    assert [int.from_bytes(data[i:i + 16], "little") for i in range(0, 80, 16)] == [0xE0000 + k for k in range(3, 8)]
    request(pl, 1, 62, 4)                                                        # イベントのリング（64）の終わりをまたぐ
    pl.write_event(62, 0xE0062)
    pl.write_event(63, 0xE0063)
    data = rd.read_all(pl)
    assert [int.from_bytes(data[i:i + 16], "little") for i in range(0, 64, 16)] == [0xE0062, 0xE0063, 0xE0000, 0xE0001]


def test_a_small_buffer_gets_the_data_in_pieces_and_the_acknowledgement_only_at_the_end(rd):
    pl = PsPlModel()
    pl.write_la(0, 100)
    request(pl, 0, 0, 50)
    first = rd.fill(pl, cap=100)                                                 # 100 バイト = 6 拍
    assert len(first) == 96 and pl.rd_ack != pl.rd_epoch
    rest = rd.read_all(pl, cap=100)
    assert first + rest == b"".join(pl.la_beat(n) for n in range(50)) and pl.rd_ack == pl.rd_epoch


def test_a_newer_request_replaces_one_that_is_still_being_read(rd):
    pl = PsPlModel()
    pl.write_la(0, 200)
    request(pl, 0, 0, 100)
    rd.fill(pl, cap=160)                                                         # 10 拍だけ読んだところで
    request(pl, 0, 50, 4)                                                        # 新しい要求（ホストが読み出しをやり直した）
    assert rd.read_all(pl) == b"".join(pl.la_beat(n) for n in range(50, 54))
    assert pl.rd_ack == pl.rd_epoch


def test_a_bad_request_is_acknowledged_so_that_the_pl_does_not_wait_forever(rd):
    pl = PsPlModel()
    request(pl, 0, 960, 4)                                                       # 先頭がリングの外
    assert rd.fill(pl) == b"" and pl.rd_ack == pl.rd_epoch
    request(pl, 0, 0, 961)                                                       # リングより多い
    assert rd.fill(pl) == b"" and pl.rd_ack == pl.rd_epoch
    request(pl, 0, 5, 0)                                                         # 0 拍
    assert rd.fill(pl) == b"" and pl.rd_ack == pl.rd_epoch


def test_window_versions_1_to_4_are_accepted_and_5_is_not(lib, api):  # noqa: F811
    # 1〜3 はフレームの受け渡しだけに使う（PS 直読みは版 4 だけ: main.c の setup_ps_stream）
    lib.relay_check_design.argtypes = [ctypes.POINTER(type(PlModel().ops))]
    pl = PsPlModel()
    for version in (1, 2, 3, 4):
        pl.version = version
        assert lib.relay_check_design(ctypes.byref(pl.ops)) == 0
    pl.version = 5
    assert lib.relay_check_design(ctypes.byref(pl.ops)) < 0


# --- 位置の約束（窓の版 4。docs/superpowers/specs/2026-11-03-ps-stream-position-design.md）------------------------------

def test_a_position_written_after_the_host_started_the_next_stream_is_dropped_by_the_window(api):
    # 中継が流れ 1 のかたまりを読んでいる途中に、ホストが次の流れを始めた（窓の番号が 2 になり位置が 0 に戻った）。中継が続けて
    # 書く流れ 1 の位置は、番号 1 を添えているので窓が捨てる（C1。前は窓が受け、PL が新しい流れの位置として使った）
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 600)
    api.step(pl)                                                                 # 付いて最初のかたまり（256 拍）

    def new_stream_during_the_next_read(addr, _n):
        if addr == LA_WRITTEN and pl.epoch == 1:                                 # 次の呼び出しの写しを読む所で
            pl.epoch = 2
            pl.pub_epoch = 1                                                     # PL はまだ流れ 1 を公開している
    pl.on_read = new_stream_during_the_next_read
    api.drain(pl, now=10**9)
    assert pl.epoch == 2 and pl.la_consumed == 0 and pl.stale_writes >= 1        # 流れ 2 の位置は 0 のまま
    pl.on_read = None
    pl.pub_epoch = None                                                          # PL が流れ 2 を公開した
    pl.la_written = 0
    pl.write_la(0, 80)                                                           # 流れ 2 が始まる
    pl.status = READING
    data, _ = stream_bytes(api.drain(pl))
    segs = split_segments(data)
    assert segs[-1].seq == 0 and len(segs[-1].data) // 16 == 80 and pl.la_consumed == 80
    assert pl.tag == 2


def test_nothing_is_read_or_written_while_the_published_group_is_of_an_older_stream(api):
    # ホストが次の流れを始めたが、PL はまだ前の流れを公開している（CURRENT = 0）: 中継は読まず、書かない
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 100)
    pl.epoch = 2
    pl.pub_epoch = 1
    assert api.drain(pl) == [] and pl.writes == [] and pl.la_consumed == 0
    lib = api.lib
    lib.relay_ps_active.argtypes = [ctypes.c_void_p]
    assert lib.relay_ps_active(api.state) == 1                                   # 流れは生きている（休まず見る）
    pl.pub_epoch = None                                                          # PL が新しい流れを公開した
    pl.la_written = 0
    pl.write_la(0, 70)
    data, _ = stream_bytes(api.drain(pl, now=10**9))
    assert split_segments(data)[0].seq == 0 and pl.la_consumed == 70


def test_the_positions_are_read_from_one_point_in_time(api):
    # 写し（STREAM_EPOCH の読み）の後に PL が進んでも、その呼び出しの中では写しの値を使う（C10）
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 64)
    api.drain(pl)
    seen = []

    def advance_after_the_snapshot(addr, _n):
        if addr == LA_WRITTEN:                                                   # 写しの後（写しの値を読む所）
            seen.append(pl.la_written)
            pl.write_la(pl.la_written, 64)                                       # 写した直後に PL がさらに書く
    pl.on_read = advance_after_the_snapshot
    api.step(pl)
    assert pl.la_consumed == seen[0]                                             # 写した時点まで（後から書いた分は次の回）


def test_the_stream_continues_across_the_2_32_wrap_of_the_totals(api):
    # 総数は 32 ビットで折り返す。リングの拍数（960）は 2 の冪でないので、総数 mod リングは書き位置と一致しない。中継は付き始めに
    # LA_WPTR から読み位置を決め、以後は読んだ分だけ進める（C2。前は la_read % la_ring で、2^32 拍の後にでたらめな所を読んだ）
    pl = PsPlModel()
    pl.total_start = M32 - 1500
    pl.epoch = 2                                                                 # 流れの開始（位置は total_start）
    pl.la_written = pl.total_start
    request_bundle(api, pl, 0)
    got = b""
    n = pl.total_start
    for _ in range(12):                                                          # 12 × 300 拍 = 3600 拍（折り返しをまたぐ）
        pl.write_la(n, 300)
        n += 300
        data, _ = stream_bytes(api.drain(pl))
        got += b"".join(s.data for s in split_segments(data))
    assert got == b"".join(pl.la_beat(k) for k in range(pl.total_start, n))
    assert pl.la_consumed == n % M32 and pl.stale_writes == 0


def test_impossible_positions_are_not_read_and_are_counted(api):
    # 未読がリングより多い（PL と中継の約束が崩れた）ときは読まず、回数を数える（A7。前は黙って未読 0 にした）
    lib = api.lib
    lib.relay_ps_bad_la.argtypes = lib.relay_ps_bad_ev.argtypes = [ctypes.c_void_p]
    lib.relay_ps_bad_la.restype = lib.relay_ps_bad_ev.restype = ctypes.c_uint32
    pl = PsPlModel()
    request_bundle(api, pl, 0)
    pl.write_la(0, 10)
    pl.status = READING                                                          # 書き込みが止まっている: すぐ出す
    api.drain(pl)
    assert pl.la_consumed == 10
    pl.la_written = 10 + LA_RING + 1                                             # ありえない
    pl.ev_wpos = EV_RING - 1                                                     # イベントの未読がリングの 3/4 を超える
    assert api.drain(pl, now=10**9) == []
    assert lib.relay_ps_bad_la(api.state) >= 1 and lib.relay_ps_bad_ev(api.state) >= 1
    assert pl.la_consumed == 10
