"""PYNQ-Z2 の中継のゼロコピーの送信の道（relay.c の relay_zc_*）。
設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.4・1.7.9

PL は Python の模型（窓のレジスタ + 深いバッファの領域）。ソケットも模型で、MSG_ZEROCOPY の呼び出しは「番地と長さ」だけを
覚え、中身は NIC が読む時（完了通知を返す直前 = ack）に読む。PL の模型は、CONSUMED が許す所まで（読み終えていない拍は
上書きしない）新しい拍を書き続ける。中継が完了の前に CONSUMED を進めれば、NIC が読む前に PL が上書きし、ホストの検査で分かる。"""
import ctypes
import random

import pytest

from fprobe import generated as g
from fprobe.eth_link import RELAY_STATUS_FIELDS, decode_relay_status
from fprobe.la import SegmentSplitter, StreamFormatError, split_segments
from tests.pynq_window import WindowModel
from tests.test_pynq_relay import ID, VERSION, PlModel, Stats, from_pc, lib  # noqa: F401

BEATS = 16384                                             # 深いバッファの拍数: イベントのリング 1024 拍、ロジアナのリング 15360 拍
MEM_BYTES = BEATS * 16
EV_RING = BEATS // 16
LA_RING = BEATS - EV_RING
LA_CONSUMED, EV_CONSUMED, STREAM_BEATS, POS_TAG = g.WIN_LA_CONSUMED, g.WIN_EV_CONSUMED, g.WIN_STREAM_BEATS, g.WIN_POS_TAG
WRITING, READING = 1, 4                                   # ZcPl.status のビット（模型の書き方）
CHUNK_MAX = 4096
AGAIN, NOBUFS, BROKEN = -1, -2, -3
RUN, DRAIN, DONE = 0, 1, 2
ZC_STATS = RELAY_STATUS_FIELDS[RELAY_STATUS_FIELDS.index("zc_calls"):]   # relay.h の RELAY_ZC_STATS（RELAY_STATUS_FIELDS の後ろの部分）


class Iov(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


SEND = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p, ctypes.POINTER(Iov), ctypes.c_int, ctypes.c_int)


class SockOps(ctypes.Structure):
    _fields_ = [("send", SEND), ("ctx", ctypes.c_void_p)]


class ZcStats(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ZC_STATS]


def la_beat(n):
    return bytes([n & 0xFF]) * 8 + n.to_bytes(8, "little")    # 拍の中身: 通し番号が分かる 16 バイト


def event_value(k):
    return (0xE0000000 + k).to_bytes(16, "little")


class ZcPl(WindowModel):
    """窓（版 4）と、深いバッファの領域（ctypes の記憶。中継はその番地を iovec に置く）。"""
    LA_RING, EV_RING, MEM_BYTES = LA_RING, EV_RING, MEM_BYTES

    def __init__(self):
        super().__init__()
        self.mem = (ctypes.c_uint8 * MEM_BYTES)()
        self.addr = ctypes.addressof(self.mem)
        self.events_written = 0                                 # イベントの総数（ev_wpos はその mod EV_RING + ev_start）

    def accept(self, addr, value):
        if addr == LA_CONSUMED:
            assert value - self.la_consumed & 0xFFFFFFFF <= LA_RING, "CONSUMED が戻った・飛んだ"

    def put(self, beat_index, data):
        ctypes.memmove(self.addr + beat_index * 16, data, 16)

    def write_la(self, count):
        """ロジアナのリングに count 拍を書く。読み終えていない拍（CONSUMED より後）は上書きしない（PL の溢れの手前で止まる）。"""
        room = self.la_consumed + LA_RING - self.la_written
        count = min(count, room)
        for n in range(self.la_written, self.la_written + count):
            self.put(n % LA_RING, la_beat(n))
        self.la_written += count
        return count

    def write_events(self, count):
        room = self.ev_consumed + EV_RING // 2 - self.events_written    # 中継の未読の判定（リングの 3/4 未満）に収まるだけ
        count = max(0, min(count, room))
        for k in range(self.events_written, self.events_written + count):
            self.put(LA_RING + (self.ev_start + k) % EV_RING, event_value(k))
        self.events_written += count
        self.ev_wpos = (self.ev_start + self.events_written) % EV_RING
        return count


class FakeSock:
    """TCP の模型。MSG_ZEROCOPY の呼び出しは番地だけを覚え、ack で中身を読んで完了通知を返す。普通の send はすぐ写す。"""

    def __init__(self, zc):
        self.zc = zc
        self.entries = []                                  # 流れの順: ["zc", 番号, [(番地, 長さ)], 読んだ中身 or None] / ["copy", bytes]
        self.next_id = 0
        self.cap = None                                    # 1 回に受け取る上限（None = 全部）
        self.results = []                                  # 次の呼び出しの結果を決めておく（AGAIN など）
        self.log = []                                      # (zerocopy, 受け取ったバイト数)
        self.cb = SEND(self._send)
        self.ops = SockOps(self.cb, None)

    def _send(self, _ctx, iov, n, zerocopy):
        if self.results:
            r = self.results.pop(0)
            if r < 0:
                self.log.append((zerocopy, r))
                return r
        pieces, left = [], self.cap if self.cap is not None else 1 << 62
        for i in range(n):
            take = min(iov[i].len, left)
            if take:
                pieces.append((iov[i].base, take))
            left -= take
        total = sum(p[1] for p in pieces)
        assert total > 0
        if zerocopy:
            self.entries.append(["zc", self.next_id, pieces, None])
            self.next_id += 1
        else:
            self.entries.append(["copy", b"".join(ctypes.string_at(a, k) for a, k in pieces)])
        self.log.append((zerocopy, total))
        return total

    def pending(self):
        return [e for e in self.entries if e[0] == "zc" and e[3] is None]

    def ack(self, count=None, copied=False, now=0, ids=None):
        """古い順に count 個の MSG_ZEROCOPY の呼び出しの中身を NIC が読み（今の記憶の中身）、完了通知を返す。"""
        todo = self.pending() if ids is None else [e for e in self.pending() if e[1] in ids]
        todo = todo if count is None else todo[:count]
        for e in todo:
            e[3] = b"".join(ctypes.string_at(a, k) for a, k in e[2])
        # 続いた番号はまとめて 1 つの通知にする（カーネルと同じ）
        ids_done = sorted(e[1] for e in todo)
        start = None
        for i, k in enumerate(ids_done):
            if start is None:
                start = k
            if i + 1 == len(ids_done) or ids_done[i + 1] != k + 1:
                self.zc.complete(start, k, copied, now)
                start = None
        return len(todo)

    def stream(self):
        assert not self.pending(), "NIC がまだ読んでいない呼び出しがある"
        return b"".join(e[3] if e[0] == "zc" else e[1] for e in self.entries)


class Zc:
    def __init__(self, lib, pl):
        self.lib, self.pl = lib, pl
        lib.relay_zc_size.restype = ctypes.c_size_t
        lib.relay_ps_size.restype = ctypes.c_size_t
        lib.relay_rd_size.restype = ctypes.c_size_t
        lib.relay_ps_init.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.relay_zc_init.argtypes = [ctypes.c_void_p]
        lib.relay_rd_init.argtypes = [ctypes.c_void_p]
        lib.relay_zc_step.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(type(pl.ops)),
                                      ctypes.POINTER(SockOps), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int64]
        lib.relay_zc_step.restype = ctypes.c_int
        lib.relay_zc_complete.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int, ctypes.c_int64]
        lib.relay_zc_complete.restype = ctypes.c_uint32
        lib.relay_zc_state.argtypes = [ctypes.c_void_p]
        lib.relay_zc_get_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(ZcStats)]
        lib.relay_zc_abandon.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(type(pl.ops))]
        lib.relay_zc_chunk_max.restype = ctypes.c_uint32
        self.state = ctypes.create_string_buffer(lib.relay_zc_size())
        self.ps = ctypes.create_string_buffer(lib.relay_ps_size())
        self.rd = ctypes.create_string_buffer(lib.relay_rd_size())
        assert lib.relay_ps_init(self.ps, MEM_BYTES) == 0
        lib.relay_rd_init(self.rd)
        lib.relay_zc_init(self.state)
        self.sock = FakeSock(self)
        self.ring = pl.addr                                # /dev/fprobe_ring の写し（試験では同じ記憶）
        self.dev = pl.addr                                 # /dev/mem の写し

    def step(self, now=0, rd=False):
        return self.lib.relay_zc_step(self.state, self.ps, self.rd if rd else None, ctypes.byref(self.pl.ops),
                                      ctypes.byref(self.sock.ops), self.ring, self.dev, now)

    def complete(self, lo, hi, copied=False, now=0):
        return self.lib.relay_zc_complete(self.state, lo, hi, int(copied), now)

    @property
    def state_code(self):
        return self.lib.relay_zc_state(self.state)

    def stats(self):
        s = ZcStats()
        self.lib.relay_zc_get_stats(self.state, ctypes.byref(s))
        return {name: getattr(s, name) for name in ZC_STATS}

    def run(self, steps=50, now=0, rd=False):
        for _ in range(steps):
            r = self.step(now, rd)
            assert r >= 0


@pytest.fixture
def zc(lib):  # noqa: F811
    return Zc(lib, ZcPl())


def check_stream(data, la_first=0, ev_start=0):
    """ホストの分割と番号の検査を通し、ロジアナの拍・イベントの中身が PL の書いたとおりか。(ロジアナの拍数, イベントの数, かたまり)"""
    segs = split_segments(data)
    la, ev = la_first, 0
    for s in segs:
        n = len(s.data) // 16
        assert 1 <= n <= g.STREAM_SEG_MAX_BEATS
        if s.kind == g.STREAM_SEG_LA:
            assert s.seq == la & 0xFFFFFFFF
            assert s.data == b"".join(la_beat(k) for k in range(la, la + n)), f"ロジアナの拍 {la}.. の中身が違う"
            la += n
        else:
            assert s.kind == g.STREAM_SEG_EVENTS and s.seq == (ev_start + ev) % EV_RING
            assert s.data == b"".join(event_value(k) for k in range(ev, ev + n)), f"イベント {ev}.. の中身が違う"
            ev += n
    return la - la_first, ev, segs


def test_the_chunk_limit_fits_what_the_host_accepts(zc):
    assert zc.lib.relay_zc_chunk_max() == CHUNK_MAX <= g.STREAM_SEG_MAX_BEATS
    assert g.STREAM_PL_CHUNK_BEATS == 256                    # PL と複写の道（relay.h の RELAY_PS_CHUNK_MAX）は変えない


def test_consumed_moves_only_after_the_completion_of_the_calls_that_carried_the_chunk(zc):
    pl = zc.pl
    pl.write_la(5000)
    zc.step()
    assert zc.sock.pending() and pl.la_consumed == 0          # 送ったが、NIC はまだ読んでいない
    assert pl.stream_beats == 4096 + 1                        # 出すと決めた拍（見出しを含む）は、渡す前に書く
    zc.run()
    assert pl.la_consumed == 0
    zc.sock.ack()
    zc.step()
    assert pl.la_consumed == 4096
    zc.step(now=2_000_000)                                    # 残りの 904 拍は、溜まり始めてから 1 ms 経ってから
    zc.sock.ack()
    zc.step(now=2_000_000)
    assert pl.la_consumed == 5000 and pl.stream_beats == 5000 + 2
    la, _, segs = check_stream(zc.sock.stream())
    assert la == 5000 and [len(s.data) // 16 for s in segs] == [4096, 904]


def test_a_chunk_goes_when_it_is_full_old_or_the_writer_has_stopped(zc):
    pl = zc.pl
    pl.write_la(4095)
    zc.step(now=0)
    zc.step(now=999_999)
    assert zc.stats()["zc_chunks"] == 0                       # 1 拍足りない・1 ms 経っていない
    zc.step(now=1_000_000)
    assert zc.stats()["zc_chunks"] == 1                       # 溜まり始めて 1 ms
    pl.write_la(10)
    pl.status = READING                                       # 書き込みが止まった: すぐ出す
    zc.step(now=1_000_001)
    assert zc.stats()["zc_chunks"] == 2


def test_a_chunk_is_split_at_the_end_of_the_ring(zc):
    pl = zc.pl
    pl.la_written = pl.la_consumed = LA_RING - 100            # 前の中継がリングの終わりの 100 拍手前まで読んでいた
    pl.write_la(300)
    pl.status = READING                                       # 書き込みが止まっている（すぐ出す）
    zc.run()
    zc.sock.ack()
    zc.step()
    la, _, segs = check_stream(zc.sock.stream(), la_first=LA_RING - 100)
    assert la == 300 and [len(s.data) // 16 for s in segs] == [100, 200]
    assert segs[1].seq == LA_RING                             # 2 つ目の見出しは続きの番号
    assert pl.la_consumed == LA_RING + 200


def test_events_across_the_end_of_their_ring_are_split_too(zc):
    pl = zc.pl
    pl.ev_start = EV_RING - 10
    pl.write_events(30)
    pl.status = READING
    zc.run()
    zc.sock.ack()
    zc.step()
    _, ev, segs = check_stream(zc.sock.stream(), ev_start=EV_RING - 10)
    assert ev == 30 and [(s.kind, len(s.data) // 16, s.seq) for s in segs] == [(2, 10, EV_RING - 10), (2, 20, 0)]
    assert pl.ev_consumed == 30


def test_a_long_stream_with_late_nic_reads_and_a_writer_that_fills_the_ring_is_intact(zc):
    # PL は CONSUMED が許す所まで書き続け、NIC はばらばらに遅れて読む。CONSUMED が早すぎれば、NIC が読む前に PL が上書きする
    pl, rng = zc.pl, random.Random(1)
    zc.sock.cap = 100_000                                     # 途中までしか受け取らない回も混ぜる
    total = 0
    for i in range(400):
        total += pl.write_la(rng.randrange(0, 6000))
        if i % 3 == 0:
            pl.write_events(rng.randrange(0, 50))
        zc.step(now=i * 1000)
        if rng.random() < 0.4:
            zc.sock.ack(rng.randrange(1, 4), now=i * 1000 + 500)
    pl.status = READING                                       # 書き込みを止め、出し切る
    for i in range(400, 600):
        zc.step(now=i * 1000)
        zc.sock.ack(now=i * 1000)
    zc.run()
    la, ev, segs = check_stream(zc.sock.stream())
    assert la == total == pl.la_written > 3 * LA_RING         # リングを何周もした
    assert ev == pl.events_written and pl.la_consumed == pl.la_written and pl.ev_consumed == pl.events_written
    assert max(len(s.data) // 16 for s in segs) == CHUNK_MAX
    st = zc.stats()
    assert st["zc_calls"] == st["zc_completed"] > 0 and st["zc_copied_calls"] == 0 and st["zc_outstanding_calls"] == 0
    assert st["zc_la_held_max_beats"] <= LA_RING and st["zc_la_held_beats"] == 0
    assert st["zc_latency_count"] == st["zc_calls"] and st["zc_latency_max_ns"] > 0
    assert pl.stream_beats == len(zc.sock.stream()) // 16     # 出すと決めた拍 = 届いた拍（見出し込み）


def test_completions_out_of_order_release_only_the_prefix(zc):
    pl = zc.pl
    zc.sock.cap = 70_000                                      # 1 回で 1 かたまり（65552 バイト）+ 少し
    pl.write_la(3 * 4096)
    zc.run(5)
    ids = [e[1] for e in zc.sock.pending()]
    assert ids == [0, 1, 2]
    zc.sock.ack(ids=[1])                                      # 2 番目が先に終わる
    zc.step()
    assert pl.la_consumed == 0                                # 1 番目が終わるまで、どれも返さない
    zc.sock.ack(ids=[0])
    zc.step()
    assert pl.la_consumed == 2 * 4096                         # 1・2 番目が運んだ 140000 バイトに全部入るかたまり（2 つ）まで
    zc.sock.ack()
    zc.run()
    assert check_stream(zc.sock.stream())[0] == 3 * 4096


def test_unknown_and_repeated_completions_are_counted_and_ignored(zc):
    zc.pl.write_la(100)
    zc.pl.status = READING
    zc.step()
    assert zc.sock.ack() == 1
    assert zc.complete(0, 0) == 0                             # 済んだ番号
    assert zc.complete(5, 7) == 0                             # 送っていない番号
    assert zc.stats()["zc_unknown_completions"] == 4


def test_eagain_and_enobufs_are_counted_and_the_same_bytes_are_sent_next_time(zc):
    pl = zc.pl
    pl.write_la(200)
    pl.status = READING
    zc.sock.results = [AGAIN, NOBUFS]
    assert zc.step() & 2 and zc.step() & 2                    # 送れなかった（ソケットを待ってよい）
    zc.step()
    zc.sock.ack()
    zc.step()
    st = zc.stats()
    assert st["zc_eagain"] == 1 and st["zc_enobufs"] == 1 and st["zc_calls"] == 1
    assert check_stream(zc.sock.stream())[0] == 200 and pl.la_consumed == 200


def test_a_broken_connection_is_reported(zc):
    zc.pl.write_la(100)
    zc.pl.status = READING
    zc.sock.results = [BROKEN]
    assert zc.step() < 0


def test_copied_stops_zerocopy_sends_the_rest_from_the_device_mapping_and_hands_over_after_the_completions(zc):
    pl = zc.pl
    stale = (ctypes.c_uint8 * MEM_BYTES)(*([0xEE] * MEM_BYTES))   # キャッシュありの写像の古い中身の代わり
    zc.ring = ctypes.addressof(stale)
    zc.sock.cap = 3001                                        # 1 回 3001 バイト（かたまりの途中・拍の途中で切れる）
    pl.write_la(1000)
    pl.status = READING
    zc.step()
    zc.step()
    assert len(zc.sock.pending()) == 2
    zc.sock.ack(1, copied=True)                               # カーネルが写した（古い中身を送りえた）
    assert zc.state_code == DRAIN
    zc.run(40)
    assert all(z == 0 for z, _ in zc.sock.log[2:])             # 以後 MSG_ZEROCOPY は使わない
    assert zc.state_code == DRAIN and pl.la_consumed == 0      # ゼロコピーで出した 2 つ目の完了をまだ待っている
    zc.sock.ack()
    zc.step()
    assert zc.state_code == DONE and pl.la_consumed == 1000
    data = zc.sock.stream()
    assert len(data) == 16 * 1001
    assert data[6002:] == check_bytes_from(6002, data, 1000)    # 写して送った分は /dev/mem の写し（正しい中身）
    st = zc.stats()
    assert st["zc_copied_calls"] == 1 and st["zc_copied_bytes"] == 3001 and st["zc_bounce_bytes"] == len(data) - 6002


def check_bytes_from(offset, data, la_beats):
    """正しい流れ（見出し + ロジアナの拍）を組み、offset から後ろを返す（見出しは中継が作った形を data から読む）。"""
    good = bytearray()
    pos, beat = 0, 0
    while beat < la_beats:
        head = data[pos:pos + 16]
        n = int.from_bytes(head[2:4], "little")
        good += head + b"".join(la_beat(k) for k in range(beat, beat + n))
        pos += 16 * (1 + n)
        beat += n
    return bytes(good[offset:])


def test_after_the_handover_the_copy_path_continues_the_stream_without_a_gap(zc):
    pl = zc.pl
    pl.write_la(5000)
    zc.step()
    zc.sock.ack(copied=True)
    zc.run()
    assert zc.state_code == DONE
    first = zc.sock.stream()
    pl.write_la(700)
    pl.status = READING
    zc.lib.relay_ps_fill_bytes.argtypes = [ctypes.c_void_p, ctypes.POINTER(type(pl.ops)), ctypes.c_void_p,
                                           ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int64]
    zc.lib.relay_ps_fill_bytes.restype = ctypes.c_size_t
    rest = b""
    for _ in range(20):
        out = ctypes.create_string_buffer(16384)
        n = zc.lib.relay_ps_fill_bytes(zc.ps, ctypes.byref(pl.ops), zc.dev, out, len(out), 0)
        rest += out.raw[:n]
    la, _, segs = check_stream(first + rest)
    assert la == 5700 and max(len(s.data) // 16 for s in segs[-3:]) <= 256 and pl.la_consumed == 5700


def test_a_new_stream_does_not_get_the_positions_of_the_old_one(zc):
    pl = zc.pl
    pl.write_la(3000)
    zc.step()
    pl.epoch += 1                                             # 新しい流れ（ホストが前の流れを出し切らずに始めた）
    pl.la_consumed = pl.la_written = 0
    zc.sock.ack()
    zc.step()
    assert all(addr != LA_CONSUMED for addr, _ in pl.writes)   # 古い流れの位置を新しい流れに書かない
    pl.write_la(500)
    pl.status = READING
    zc.run()
    zc.sock.ack()
    zc.step()
    assert pl.la_consumed == 500
    segs = split_segments(zc.sock.stream())
    assert segs[-1].seq == 0 and len(segs[-1].data) // 16 == 500


def test_a_capture_read_request_goes_before_the_stream_and_is_acknowledged(zc):
    pl = zc.pl
    for k in range(40):
        pl.put(k, la_beat(10_000 + k))
    pl.rd_epoch, pl.rd_region, pl.rd_first, pl.rd_count = 1, 0, 0, 40
    pl.mode = 0                                               # 取り込みの読み出し（ストリームは止まっている）
    zc.run(5, rd=True)
    assert pl.rd_ack == 1
    assert zc.sock.stream() == b"".join(la_beat(10_000 + k) for k in range(40))
    assert all(z == 0 for z, _ in zc.sock.log)                 # 写して普通に送る（今の道と同じ中身）


def test_the_rest_of_a_chunk_the_udp_path_started_goes_first(zc):
    pl, lib = zc.pl, zc.lib
    pl.write_la(100)
    pl.status = READING
    lib.relay_ps_step.argtypes = [ctypes.c_void_p, ctypes.POINTER(type(pl.ops)), ctypes.c_void_p, ctypes.c_char_p,
                                  ctypes.c_size_t, ctypes.POINTER(Stats), ctypes.c_int64]
    lib.relay_ps_step.restype = ctypes.c_size_t
    st = Stats()
    out = ctypes.create_string_buffer(600)
    n = lib.relay_ps_step(zc.ps, ctypes.byref(pl.ops), zc.dev, out, 600, ctypes.byref(st), 0)   # UDP で 512 バイトだけ出した
    first = out.raw[2:n]
    assert len(first) == 512
    pl.write_la(50)
    zc.run()
    zc.sock.ack()
    zc.step()
    la, _, _ = check_stream(first + zc.sock.stream())
    assert la == 150 and pl.la_consumed == 150


def test_abandoning_returns_everything_taken_like_the_copy_path_drops_its_ring(zc):
    pl = zc.pl
    pl.write_la(3000)
    pl.status = READING
    zc.step()
    zc.lib.relay_zc_abandon(zc.state, zc.ps, ctypes.byref(pl.ops))
    st = zc.stats()
    assert pl.la_consumed == 3000 and st["zc_outstanding_calls"] == 0
    # 完了していないまま返したかたまりは数える（A8。呼び手 main.c は先に SO_LINGER 0 で打ち切る）
    assert st["zc_abandoned_chunks"] >= 1 and st["zc_abandoned_bytes"] == 16 * (3000 + st["zc_abandoned_chunks"])


def test_a_release_after_the_next_stream_started_does_not_write_the_old_position(zc):
    # ゼロコピーの道の解放（完了通知の後）が、次の流れが始まった後に来ても、前の流れの位置を新しい流れに書かない（A6・C1）
    pl = zc.pl
    pl.write_la(3000)
    pl.status = READING
    zc.step()
    pl.epoch += 1                                             # ホストが次の流れを始めた（窓の位置は 0）
    pl.pub_epoch = pl.epoch - 1                               # PL はまだ前の流れを公開している
    zc.sock.ack()
    zc.step()
    assert pl.la_consumed == 0 and pl.ev_consumed == 0


def test_the_descriptor_table_limit_is_counted(zc):
    pl = zc.pl
    pl.status = WRITING | READING
    for i in range(6000):                                     # 古くなった小さなかたまりを、完了を返さずに作り続ける
        pl.write_la(1)
        zc.step(now=i * 2_000_000)
    st = zc.stats()
    assert st["zc_desc_full"] > 0 and st["zc_chunks"] == 2048
    zc.sock.ack()
    for i in range(10):
        zc.step(now=10**12 + i * 2_000_000)
    assert zc.stats()["zc_chunks"] > 2048


def gap_record(time, beats_before, samples):
    """段 B の欠けの記録（種類 19 = EVENT_KIND_LA_GAP。fprobe.events の頭の形: [47:0] 時刻、[79:48] 欠けの前の拍の数、
    [119:80] 捨てたサンプルの数）。"""
    return (time | beats_before << 48 | samples << 80 | g.EVENT_KIND_LA_GAP << 120).to_bytes(16, "little")


def aux_record(time, value):
    return (time | value << 80 | 6 << 120).to_bytes(16, "little")     # 種類 6（補助マイコンの計測値）


def test_gap_records_mixed_in_the_event_ring_go_out_unchanged_with_the_zerocopy_chunks(zc):
    # 段 B（欠けを記録して続ける。la-deep-fast §12）の欠けの記録は、ほかのイベントと同じくイベントのリングに置かれる。
    # ゼロコピーの道（ロジアナは 4096 拍のかたまり）で、中継は種類を見ずにリングの中身をそのまま運ぶ（設計 1.7.9）
    from fprobe.events import KIND_LA_GAP, decode_events
    pl = zc.pl
    pl.ev_start = EV_RING - 25                                # イベントのリングの終わりもまたぐ
    records = [gap_record(1000 + k, 4096 * k, 77 + k) if k % 3 == 1 else aux_record(1000 + k, k) for k in range(120)]

    def put_events(batch):
        for r in batch:
            pl.put(LA_RING + (pl.ev_start + pl.events_written) % EV_RING, r)
            pl.events_written += 1
        pl.ev_wpos = (pl.ev_start + pl.events_written) % EV_RING

    for i in range(6):
        pl.write_la(4096 + 100)
        put_events(records[20 * i:20 * (i + 1)])
        zc.step(now=i * 2_000_000)
        zc.sock.ack(now=i * 2_000_000 + 1)
        zc.step(now=i * 2_000_000 + 2)
    pl.status = READING                                       # 書き込みを止め、出し切る
    for i in range(6, 40):
        zc.step(now=i * 2_000_000)
        zc.sock.ack(now=i * 2_000_000)
    zc.run()
    assert not zc.sock.pending()
    segs = split_segments(zc.sock.stream())
    la = [s for s in segs if s.kind == g.STREAM_SEG_LA]
    ev = [s for s in segs if s.kind == g.STREAM_SEG_EVENTS]
    assert b"".join(s.data for s in la) == b"".join(la_beat(k) for k in range(pl.la_written))
    assert max(len(s.data) // 16 for s in la) == CHUNK_MAX                     # ゼロコピーの道のかたまり
    assert all(z for z, _ in zc.sock.log)                                       # どれも MSG_ZEROCOPY で送った
    assert b"".join(s.data for s in ev) == b"".join(records)                    # 種類 19 も、そのままの中身・順で
    seq = EV_RING - 25
    for s in ev:                                                                # イベントの見出しの番号は続いている
        assert s.seq == seq % EV_RING
        seq += len(s.data) // 16
    gaps = [e for e in decode_events(b"".join(s.data for s in ev)) if e.kind == KIND_LA_GAP]
    assert [(e.time, e.address, e.data) for e in gaps] == [(1000 + k, 4096 * k, 77 + k) for k in range(1, 120, 3)]
    assert pl.ev_consumed == len(records) and pl.la_consumed == pl.la_written


# --- デバイスツリーの照合・状態の問い合わせ -------------------------------------------------------------------------

def make_dt(tmp_path, nodes, cells=1):
    d = tmp_path / "reserved-memory"
    d.mkdir()
    (d / "#address-cells").write_bytes(cells.to_bytes(4, "big"))
    (d / "#size-cells").write_bytes(cells.to_bytes(4, "big"))
    for name, reg in nodes:
        (d / name).mkdir()
        (d / name / "reg").write_bytes(b"".join(v.to_bytes(4, "big") for v in reg))
    return d


@pytest.mark.parametrize("nodes, ok", [
    ([("fprobe_ring@1e000000", (0x1E000000, 0x2000000))], 1),
    ([("fprobe_ring", (0x1E000000, 0x2000000))], 1),
    ([("fprobe_ring@1e000000", (0x1E000000, 0x1000000))], 0),                 # 小さい
    ([("fprobe_ring@1d000000", (0x1D000000, 0x3000000))], 0),                 # 始まりが違う（/dev/fprobe_ring の 0 が base でない）
    ([("other@1e000000", (0x1E000000, 0x2000000))], 0),
    ([("fprobe_ring@1e000000", (0x1E000000, 0x2000000)), ("fprobe_ring@1", (0x1, 0x1))], 0),   # 2 つ
    ([("fprobe_ring_x", (0x1E000000, 0x2000000))], 0),
])
def test_the_ringmap_node_must_be_the_deep_buffer_region(lib, tmp_path, nodes, ok):  # noqa: F811
    lib.relay_ringmap_region_ok.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    d = make_dt(tmp_path, nodes)
    assert lib.relay_ringmap_region_ok(str(d).encode(), 0x1E000000, 0x2000000) == ok


def test_the_ringmap_node_with_two_cell_addresses_is_refused(lib, tmp_path):  # noqa: F811
    lib.relay_ringmap_region_ok.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    d = make_dt(tmp_path, [("fprobe_ring", (0, 0x1E000000, 0, 0x2000000))], cells=2)
    assert lib.relay_ringmap_region_ok(str(d).encode(), 0x1E000000, 0x2000000) == 0


def test_a_status_request_is_answered_by_the_relay_and_not_passed_to_the_pl(lib):  # noqa: F811
    pl, st = PlModel(), Stats()
    assert from_pc(lib, pl, bytes([0x41, 0]), st) == 2 and pl.frames == []
    assert from_pc(lib, pl, bytes([0x41, 0, 0]), st) < 0 and st.dropped_bad == 1


def test_a_status_request_does_not_move_the_pc_address(lib):  # noqa: F811
    # fprobe_gw pynq status は別のソケットから問い合わせる。使っている PC の UDP の応答の宛先を奪わない
    lib.relay_dgram_sets_peer.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    assert lib.relay_dgram_sets_peer(bytes([0x41, 0]), 2) == 0
    assert lib.relay_dgram_sets_peer(bytes([0, 0]), 2) == 1                  # 空の CONTROL（宛先の結び付け）
    assert lib.relay_dgram_sets_peer(bytes([0x40, 0, 0, 1]), 4) == 1         # まとめ送りの要求
    assert lib.relay_dgram_sets_peer(bytes([1]), 1) == 0                     # 形が崩れたもの
    assert lib.relay_dgram_sets_peer(bytes([1, 0]) + bytes(513), 515) == 0  # payload が長すぎる


def test_the_status_names_and_layout_match_the_host_decoder(lib):  # noqa: F811
    lib.relay_status_count.restype = ctypes.c_int
    lib.relay_status_field_name.restype = ctypes.c_char_p
    lib.relay_status_field_name.argtypes = [ctypes.c_int]
    names = tuple(lib.relay_status_field_name(i).decode() for i in range(lib.relay_status_count()))
    assert names == RELAY_STATUS_FIELDS

    class Status(ctypes.Structure):
        _fields_ = [("zc_state", ctypes.c_uint8), ("tcp_path", ctypes.c_uint8)] + [(n, ctypes.c_uint64) for n in names]

    s = Status(2, 3, *range(1000, 1000 + len(names)))
    lib.relay_status_encode.argtypes = [ctypes.POINTER(Status), ctypes.c_uint8, ctypes.c_char_p, ctypes.c_size_t]
    lib.relay_status_encode.restype = ctypes.c_size_t
    out = ctypes.create_string_buffer(1024)
    n = lib.relay_status_encode(ctypes.byref(s), 77, out, len(out))
    got = decode_relay_status(out.raw[:n])
    assert got["version"] == 2 and got["zc_state"] == "stopped_copied" and got["tcp_path"] == "zerocopy_draining"
    assert got["token"] == 77                                              # 問い合わせの番号を写す（A10）
    assert [got[k] for k in names] == list(range(1000, 1000 + len(names)))
    assert lib.relay_status_encode(ctypes.byref(s), 77, out, n - 1) == 0   # 入りきらなければ組まない


def test_the_host_splitter_accepts_the_zerocopy_chunks_and_refuses_larger_ones():
    def head(count, seq=0):
        return (g.STREAM_SEG_LA | count << 16 | seq << 32 | g.STREAM_SEG_MAGIC << 64).to_bytes(16, "little")
    big = head(CHUNK_MAX) + bytes(16 * CHUNK_MAX)
    assert len(SegmentSplitter().feed(big)[0].data) == 16 * CHUNK_MAX
    with pytest.raises(StreamFormatError):
        SegmentSplitter().feed(head(g.STREAM_SEG_MAX_BEATS + 1) + bytes(16))
