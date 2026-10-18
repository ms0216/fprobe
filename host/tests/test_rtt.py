"""fprobe.rtt: 偽のメモリ（バイト配列）に RTT の制御ブロックとリングを組み立て、ターゲット側の書き込み・読み出しを
模擬して、PC 側の読み出し・折り返し・下りの書き込み・読み書きの回数と順序を確かめる。"""

import struct

import pytest

from fprobe.rtt import (ControlBlockNotFound, Rtt, RttError, RttMode, find_control_block, ring_free, ring_spans,
                        ring_used)

BASE = 0x20000000
RAM = 0x8000


class FakeMemory:
    """番地 BASE から RAM バイトのメモリ。PC 側の読み書きは ops に記録する（ターゲット側の模擬は記録しない）。"""

    def __init__(self):
        self.ram = bytearray(RAM)
        self.ops: list[tuple] = []

    def read_memory(self, address: int, length: int) -> bytes:
        self.ops.append(("r", address, length))
        return bytes(self.ram[address - BASE:address - BASE + length])

    def write_memory(self, address: int, data: bytes) -> None:
        self.ops.append(("w", address, bytes(data)))
        self.ram[address - BASE:address - BASE + len(data)] = data

    # ターゲット側（記録しない）
    def poke(self, address: int, data: bytes) -> None:
        self.ram[address - BASE:address - BASE + len(data)] = data

    def peek(self, address: int, length: int) -> bytes:
        return bytes(self.ram[address - BASE:address - BASE + length])

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.peek(address, 4))[0]


def build(mem: FakeMemory, cb: int, up: list[tuple], down: list[tuple], names_at: int = BASE + 0x7000) -> None:
    """制御ブロックを組み立てる。up / down は (名前 | None, リングの番地, 大きさ, フラグ)。"""
    mem.poke(cb, b"SEGGER RTT".ljust(16, b"\0") + struct.pack("<II", len(up), len(down)))
    for i, (name, buf, size, flags) in enumerate(up + down):
        name_ptr = 0
        if name is not None:
            name_ptr = names_at
            mem.poke(names_at, name.encode() + b"\0")
            names_at += len(name) + 1
        mem.poke(cb + 24 + 24 * i, struct.pack("<IIIIII", name_ptr, buf, size, 0, 0, flags))


class Target:
    """ターゲット側の RTT（模擬）。SEGGER の振る舞いの取り決めに従う: データを書いてから WrOff を進める。"""

    def __init__(self, mem: FakeMemory, cb: int):
        self.mem, self.cb = mem, cb
        self.num_up = mem.u32(cb + 16)

    def _desc(self, index: int) -> tuple[int, int, int, int, int, int]:
        return struct.unpack("<IIIIII", self.mem.peek(self.cb + 24 + 24 * index, 24))

    def write(self, channel: int, data: bytes) -> int | None:
        """上りに書く。書いたバイト数。BLOCK_IF_FIFO_FULL で入らなければ None（本物は空くまで待つ）。"""
        desc = self.cb + 24 + 24 * channel
        _, buf, size, wr, rd, flags = self._desc(channel)
        free = ring_free(size, wr, rd)
        mode = flags & 3
        if len(data) > free:
            if mode == RttMode.NO_BLOCK_SKIP:
                return 0
            if mode == RttMode.BLOCK_IF_FIFO_FULL:
                return None
            data = data[:free]
        for i, b in enumerate(data):
            self.mem.poke(buf + (wr + i) % size, bytes([b]))
        self.mem.poke(desc + 12, struct.pack("<I", (wr + len(data)) % size))
        return len(data)

    def read(self, channel: int) -> bytes:
        """下りから読む。"""
        desc = self.cb + 24 + 24 * (self.num_up + channel)
        _, buf, size, wr, rd, _ = self._desc(self.num_up + channel)
        data = bytes(self.mem.peek(buf + (rd + i) % size, 1)[0] for i in range(ring_used(size, wr, rd)))
        self.mem.poke(desc + 16, struct.pack("<I", wr))
        return data


CB = BASE + 0x100
UP0, UP1, DOWN0 = BASE + 0x200, BASE + 0x400, BASE + 0x500


@pytest.fixture
def setup():
    """普通の配置: 制御ブロックの後ろにリング（上り 0: 256 B、上り 1: 64 B、下り 0: 32 B）。"""
    mem = FakeMemory()
    build(mem, CB, up=[("Terminal", UP0, 256, RttMode.NO_BLOCK_SKIP), ("Data", UP1, 64, RttMode.NO_BLOCK_TRIM)],
          down=[("Terminal", DOWN0, 32, RttMode.NO_BLOCK_SKIP)])
    rtt = Rtt(mem, CB)
    rtt.open()
    mem.ops.clear()
    return mem, Target(mem, CB), rtt


# --- リングの純粋関数 ----------------------------------------------------------------------

def test_ring_arithmetic():
    assert ring_used(16, 5, 5) == 0 and ring_free(16, 5, 5) == 15
    assert ring_used(16, 2, 14) == 4 and ring_free(16, 2, 14) == 11
    assert ring_used(16, 13, 14) == 15 and ring_free(16, 13, 14) == 0          # 満杯（1 バイト空ける）
    assert ring_spans(10, 4, 16) == [(10, 4)]
    assert ring_spans(14, 5, 16) == [(14, 2), (0, 3)]
    assert ring_spans(0, 0, 16) == []


# --- 開く ---------------------------------------------------------------------------------

def test_open_reads_channels(setup):
    _, _, rtt = setup
    cb = rtt.control_block
    assert cb.address == CB
    assert [(c.name, c.buffer_address, c.size, c.mode) for c in cb.up] == [
        ("Terminal", UP0, 256, RttMode.NO_BLOCK_SKIP), ("Data", UP1, 64, RttMode.NO_BLOCK_TRIM)]
    assert [(c.name, c.size, c.direction) for c in cb.down] == [("Terminal", 32, "down")]


def test_open_rejects_wrong_id_and_bad_descriptor():
    mem = FakeMemory()
    with pytest.raises(RttError):
        Rtt(mem, CB).open()
    build(mem, CB, up=[("x", UP0, 16, 0)], down=[])
    mem.poke(CB + 24 + 12, struct.pack("<I", 16))                                 # WrOff が大きさ以上
    with pytest.raises(RttError):
        Rtt(mem, CB).open()


def test_unused_channels_are_kept_but_skipped():
    mem = FakeMemory()
    build(mem, CB, up=[("t", UP0, 64, 0), (None, 0, 0, 0)], down=[(None, 0, 0, 0)])
    rtt = Rtt(mem, CB)
    cb = rtt.open()
    assert cb.up[1].size == 0 and cb.up[1].name is None
    Target(mem, CB).write(0, b"ok")
    assert rtt.poll() == {0: b"ok"}
    with pytest.raises(RttError):
        rtt.write(0, b"x")                                                         # 使われていない下り


# --- 上り（ターゲット → PC） --------------------------------------------------------------

def test_poll_reads_data_and_updates_read_offset_in_one_read_one_write(setup):
    mem, target, rtt = setup
    target.write(0, b"hello\n")
    assert rtt.poll() == {0: b"hello\n"}
    assert mem.u32(CB + 24 + 16) == 6                                              # RdOff を進めた
    assert [op[0] for op in mem.ops] == ["r", "w"]                                 # 推測読みで 1 回にまとまる
    assert mem.ops[0][1] == CB + 24                                                # 記述子の配列から読み始める
    mem.ops.clear()
    assert rtt.poll() == {}
    assert [op[0] for op in mem.ops] == ["r"]                                      # 何も無ければ読み 1 回だけ


def test_poll_multiple_channels(setup):
    _, target, rtt = setup
    target.write(0, b"log")
    target.write(1, bytes(range(10)))
    assert rtt.poll() == {0: b"log", 1: bytes(range(10))}


def test_wrap_around(setup):
    _, target, rtt = setup
    for _ in range(5):                                                             # 何周も回す
        target.write(1, bytes(range(40)))
        assert rtt.poll() == {1: bytes(range(40))}


def test_full_ring_and_modes(setup):
    _, target, rtt = setup
    assert target.write(1, bytes(70)) == 63                                        # TRIM: 入るだけ（64 - 1）
    assert rtt.poll() == {1: bytes(63)}
    assert target.write(0, bytes(255)) == 255                                      # 満杯ちょうど
    assert target.write(0, b"lost") == 0                                           # SKIP: 丸ごと捨てる
    assert rtt.poll() == {0: bytes(255)}
    mem2 = FakeMemory()
    build(mem2, CB, up=[("b", UP0, 8, RttMode.BLOCK_IF_FIFO_FULL)], down=[])
    t2, r2 = Target(mem2, CB), Rtt(mem2, CB)
    r2.open()
    assert t2.write(0, b"1234567") == 7 and t2.write(0, b"8") is None             # BLOCK: 読むまで待つ
    assert r2.poll() == {0: b"1234567"}
    assert t2.write(0, b"8") == 1 and r2.poll() == {0: b"8"}


def test_large_data_reads_rest_after_speculative_prefix(setup):
    mem, target, rtt = setup
    target.write(0, bytes(range(200)))
    assert rtt.poll() == {0: bytes(range(200))}
    reads = [op for op in mem.ops if op[0] == "r"]
    assert len(reads) == 2                                                         # 推測の 64 バイト + 残り
    assert reads[1][1] == UP0 + 64 and reads[1][2] == 200 - 64


def test_no_speculation_when_ring_precedes_descriptors():
    """リングが記述子より前にあると、1 回の読みでは「データを WrOff の後に読む」順序が守れない。"""
    mem = FakeMemory()
    cb = BASE + 0x1000
    build(mem, cb, up=[("t", BASE + 0x800, 128, 0)], down=[])
    rtt = Rtt(mem, cb)
    rtt.open()
    mem.ops.clear()
    Target(mem, cb).write(0, b"abc")
    assert rtt.poll() == {0: b"abc"}
    assert [(op[0], op[1]) for op in mem.ops] == [("r", cb + 24), ("r", BASE + 0x800), ("w", cb + 24 + 16)]


def test_no_speculation_when_ring_is_far_away():
    mem = FakeMemory()
    build(mem, CB, up=[("t", BASE + 0x4000, 128, 0)], down=[])
    rtt = Rtt(mem, CB, merge_gap=256)
    rtt.open()
    mem.ops.clear()
    Target(mem, CB).write(0, b"abc")
    assert rtt.poll() == {0: b"abc"}
    assert [op[0] for op in mem.ops] == ["r", "r", "w"] and mem.ops[0][2] == 24


def test_order_of_operations(setup):
    """WrOff を読んでからデータを読み、データを読んでから RdOff を書く。"""
    mem, target, rtt = setup
    rtt_slow = Rtt(mem, CB, speculate=0)
    rtt_slow.open()
    mem.ops.clear()
    target.write(0, b"xyz")
    rtt_slow.poll()
    assert [(op[0], op[1]) for op in mem.ops] == [("r", CB + 24), ("r", UP0), ("w", CB + 24 + 16)]


def test_read_single_channel(setup):
    _, target, rtt = setup
    target.write(0, b"a")
    target.write(1, b"b")
    assert rtt.read(1) == b"b"
    assert rtt.poll() == {0: b"a"}


def test_corrupt_write_offset_raises(setup):
    mem, _, rtt = setup
    mem.poke(CB + 24 + 12, struct.pack("<I", 999))
    with pytest.raises(RttError):
        rtt.poll()


def test_read_offset_changed_by_someone_else_is_followed(setup):
    mem, target, rtt = setup
    target.write(0, b"abcdef")
    mem.poke(CB + 24 + 16, struct.pack("<I", 4))                                   # 別の道具が 4 バイト読んだ
    assert rtt.poll() == {0: b"ef"}
    assert rtt.control_block.up[0].resyncs == 1


# --- 下り（PC → ターゲット） --------------------------------------------------------------

def test_write_down_channel(setup):
    mem, target, rtt = setup
    assert rtt.write(0, b"hi") == 2
    assert target.read(0) == b"hi"
    kinds = [(op[0], op[1]) for op in mem.ops]
    assert kinds == [("r", CB + 24 + 48 + 12), ("w", DOWN0), ("w", CB + 24 + 48 + 12)]   # 位置 → データ → WrOff


def test_write_down_respects_free_space_and_wraps(setup):
    _, target, rtt = setup
    assert rtt.write(0, bytes(range(40))) == 31                                    # 32 - 1
    assert rtt.write(0, b"more") == 0
    assert target.read(0) == bytes(range(31))
    assert rtt.write(0, b"wrapping!") == 9                                         # 折り返す
    assert target.read(0) == b"wrapping!"


def test_write_all_waits_for_target(setup):
    _, target, rtt = setup
    received = bytearray()

    def drain(_):
        received.extend(target.read(0))
    data = bytes(range(100))
    assert rtt.write_all(0, data, timeout_s=1.0, wait=drain) == 100
    received.extend(target.read(0))
    assert bytes(received) == data


def test_write_all_times_out(setup):
    _, _, rtt = setup
    assert rtt.write_all(0, bytes(100), timeout_s=0.01, wait=lambda _: None) == 31


# --- 探索 ---------------------------------------------------------------------------------

def test_find_control_block_across_chunk_boundary_and_skips_fake_id():
    mem = FakeMemory()
    mem.poke(BASE + 0x40, b"SEGGER RTT\0" + b"\xFF" * 13)                         # 文字列だけの偽物
    cb = BASE + 0x1000 - 5                                                         # かたまり（4 KB）の境目をまたぐ
    build(mem, cb, up=[("t", BASE + 0x2000, 64, 0)], down=[])
    assert find_control_block(mem, [(BASE, RAM)], chunk=0x1000) == cb


def test_find_control_block_not_found():
    with pytest.raises(ControlBlockNotFound):
        find_control_block(FakeMemory(), [(BASE, RAM)])


# --- 速さの工夫（U50） ----------------------------------------------------------------------

def test_coalesce_joins_near_spans_within_the_limit():
    from fprobe.rtt import coalesce
    assert coalesce([(100, 10), (0, 10), (120, 5)], gap=10) == [(0, 10), (100, 25)]
    assert coalesce([(0, 10), (30, 10)], gap=10) == [(0, 10), (30, 10)]
    assert coalesce([(0, 10), (10, 10)], gap=0, limit=15) == [(0, 10), (10, 10)]       # 上限を超えるならつながない
    assert coalesce([(0, 0), (5, 1)], gap=0) == [(5, 1)]


def wide(mem, **options) -> Rtt:
    rtt = Rtt(mem, CB, merge_gap=512, **options)
    rtt.open()
    mem.ops.clear()
    return rtt


def test_missing_data_of_several_channels_is_read_together(setup):
    mem, target, _ = setup
    rtt = wide(mem, speculate=0)
    target.write(0, bytes(range(200)))                                             # 0x200.. の 200 B
    target.write(1, bytes(range(50)))                                              # 0x400.. の 50 B
    assert rtt.poll() == {0: bytes(range(200)), 1: bytes(range(50))}
    reads = [op for op in mem.ops if op[0] == "r"]
    assert len(reads) == 2                                                         # 記述子、データを 1 回で
    assert reads[1][1] == UP0 and reads[1][1] + reads[1][2] == UP1 + 50     # 隙間ごと 1 回で


def test_speculation_follows_the_last_amount(setup):
    mem, target, _ = setup
    rtt = wide(mem)
    target.write(0, bytes(150))
    rtt.poll()
    mem.ops.clear()
    target.write(0, bytes(100))                                                    # 前回の量（150）まで推測で読む
    assert rtt.poll() == {0: bytes(100)}
    assert [op[0] for op in mem.ops] == ["r", "w"]
    rtt.poll()
    assert rtt.control_block.up[0].expect == 0                                     # 来なければ推測は最小に戻る


def test_a_known_write_offset_skips_the_descriptor_read(setup):
    mem, target, rtt = setup
    before = rtt.stats()
    target.write(0, b"abc")
    wr = mem.u32(CB + 24 + 12)
    target.write(0, b"def")                                                        # サンプラが見た後に書かれた分
    assert rtt.poll(wr_hint={0: wr}) == {0: b"abc"}
    assert [(op[0], op[1]) for op in mem.ops] == [("r", UP0), ("w", CB + 24 + 16)]
    assert rtt.poll() == {0: b"def"}                                               # 残りは次の poll で
    stats = rtt.stats()
    assert stats["polls"] == 2 and stats["hinted_polls"] == 1 and stats["up_bytes"] == 6
    assert stats["reads"] - before["reads"] == 2 and stats["writes"] - before["writes"] == 2
    assert rtt.poll(wr_hint={}) == {} and rtt.stats()["empty_polls"] == 1 and rtt.stats()["reads"] == stats["reads"]


def test_write_offset_addresses_are_given_for_the_sampler(setup):
    _, _, rtt = setup
    assert rtt.wroff_addresses() == {0: CB + 24 + 12, 1: CB + 48 + 12}
