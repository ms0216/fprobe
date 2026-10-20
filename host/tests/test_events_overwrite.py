"""イベント記録を読む間に、書き手が読み手を追い越した分（リングが小さい構成で起きる）を捨てて数える。
docs/superpowers/specs/2026-10-20-bram-event-ring-design.md（レビューの指摘）。"""

from fprobe import generated as g
from fprobe.events import EVENT_BYTES, EventLog


def event(i: int) -> bytes:
    # 時刻 = 書いた番号、種類 4（UART のバイト）
    return (4 << 120 | i).to_bytes(EVENT_BYTES, "little")


class RingDevice:
    """容量 capacity のリング。read_stream の間に advance 件だけ新しいイベントが書かれる（遅いリンクで読む間に追い越される）。"""

    def __init__(self, capacity: int, written: int, advance: int):
        self.capacity, self.written, self.advance = capacity, written, advance
        self.first = self.count = 0

    def read32(self, address):
        assert address == g.EVENTS_CAPACITY
        return self.capacity

    def read(self, address, n):
        assert address == g.EVENTS_WRITTEN and n == 2
        return [self.written, 0]

    def write32(self, address, value):
        pass

    def write(self, address, words):
        if address == g.LA_DEEP_READ_FIRST:
            self.first, self.count = words

    def read_stream(self, size, timeout_ms=2000):
        # 読み始めの時点のリング: 位置 p には「p ≡ 番号 (mod capacity) の最新の番号」が入っている。読む間に書かれた分も反映する
        self.written += self.advance
        out = b""
        for k in range(self.count):
            pos = (self.first + k) % self.capacity
            newest = max(i for i in range(self.written) if i % self.capacity == pos)
            out += event(newest)
        return out


def test_events_overwritten_while_reading_are_dropped_and_counted():
    dev = RingDevice(capacity=8, written=8, advance=5)
    log = EventLog(dev)
    events = log.read()                                   # 番号 0〜7 を読むつもりが、0〜4 は 8〜12 に上書きされた
    assert [e.time for e in events] == [5, 6, 7]
    assert log.overwritten == 5


def test_the_cursor_counts_overwritten_events_as_lost():
    dev = RingDevice(capacity=8, written=0, advance=0)
    log = EventLog(dev)
    cursor = log.cursor()
    dev.written, dev.advance = 6, 4                       # 6 件の後、読む間に 4 件
    events = cursor.take()
    assert [e.time for e in events] == [2, 3, 4, 5]       # 0・1 は 8・9 に上書きされた
    assert cursor.lost == 2


def test_nothing_is_dropped_when_the_writer_stays_behind():
    dev = RingDevice(capacity=1024, written=100, advance=3)
    log = EventLog(dev)
    assert [e.time for e in log.read()] == list(range(100)) and log.overwritten == 0
