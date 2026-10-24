"""fprobe.watch_buffer（U43）: ターゲット側のバッファ（配列 + 書いた数）の吸い出し。偽のメモリで模擬する。"""

import struct

import pytest
from value_kinds import ints, vint

from fprobe.dwarf.typesys import BaseType
from fprobe.watch_buffer import TargetRing

BUF, COUNT, N = 0x1000, 0x2000, 8
I16 = BaseType("short", 2, "signed")


class Target:
    """ターゲットのファーム: 要素を書いてから数を進める。"""

    def __init__(self, counter=True, width=4):
        self.mem = bytearray(0x3000)
        self.n = 0
        self.counter, self.width = counter, width
        self.on_read = None

    def put(self, value):
        at = BUF + 2 * (self.n % N)
        self.mem[at:at + 2] = struct.pack("<h", value)
        self.n += 1
        head = self.n % (1 << (8 * self.width)) if self.counter else self.n % N
        self.mem[COUNT:COUNT + self.width] = head.to_bytes(self.width, "little")

    def read(self, address, length):
        if self.on_read is not None and address == BUF:
            self.on_read()
        return bytes(self.mem[address:address + length])


def ring(target, **kw):
    return TargetRing(I16, BUF, N, COUNT, target.width, counter=target.counter, **kw)


def test_new_elements_are_read_once_in_order_with_their_numbers():
    t = Target()
    r = ring(t)
    t.put(-1)
    assert r.poll(t.read) == []                                                   # 最初は位置を合わせるだけ
    t.put(10)
    t.put(-20)
    assert r.poll(t.read) == [(0, vint(10)), (1, vint(-20))]
    assert r.poll(t.read) == []
    for v in range(6):                                                            # 折り返す
        t.put(v)
    assert r.poll(t.read) == [(2 + k, vint(k)) for k in range(6)] and r.lost == 0


def test_a_lap_before_reading_is_counted_as_lost():
    t = Target()
    r = ring(t)
    r.poll(t.read)
    for v in range(N + 3):
        t.put(v)
    got = r.poll(t.read)
    assert [v for _, v in got] == ints(*range(3, N + 3)) and r.lost == 3 and got[0][0] == 3


def test_elements_overwritten_while_reading_are_dropped():
    t = Target()
    r = ring(t)
    r.poll(t.read)
    for v in range(N):
        t.put(v)
    t.on_read = lambda: (t.put(100), t.put(101), setattr(t, "on_read", None))     # 読んでいる間に 2 つ書かれた
    got = r.poll(t.read)
    assert [s for s, _ in got] == list(range(2, N)) and r.lost == 2


def test_a_narrow_counter_wraps():
    t = Target(width=1)
    t.n = 254
    r = ring(t)
    t.put(1)
    r.poll(t.read)
    t.put(2)
    t.put(3)                                                                      # 255 → 0 → 1
    assert [v for _, v in r.poll(t.read)] == ints(2, 3)


def test_a_write_index_instead_of_a_counter():
    t = Target(counter=False)
    r = ring(t)
    t.put(5)
    r.poll(t.read)
    t.put(6)
    t.put(7)
    assert [v for _, v in r.poll(t.read)] == ints(6, 7)
    t.mem[COUNT:COUNT + 4] = (N + 1).to_bytes(4, "little")
    with pytest.raises(ValueError):
        r.poll(t.read)
