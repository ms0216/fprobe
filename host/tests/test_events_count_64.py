"""書いたイベントの総数は 64 ビット（written_hi:written）。2**32 をまたいでも戻らず、EventCursor が始め直しと取り違えない。"""
from fprobe import generated as g
from fprobe.events import EventCursor, EventLog
from caps_fakes import FakeDeviceBase


class Dev(FakeDeviceBase):
    def __init__(self, written):
        self.written = written
        self.reads = []

    def read(self, address, count=1, *, timeout_retries=0):
        assert address == g.EVENTS_WRITTEN and count == 4        # written に続けて written_hi まで 1 回の連続読み出し
        self.reads.append(address)
        return [self.written & 0xFFFFFFFF, 3, 64, self.written >> 32]

    def read32(self, address):
        assert address == g.EVENTS_CAPACITY
        return 64


def test_counts_join_the_high_and_low_words():
    assert EventLog(Dev((5 << 32) | 7)).counts() == ((5 << 32) | 7, 3)
    assert EventLog(Dev((1 << 32) - 1)).counts()[0] == (1 << 32) - 1


def test_cursor_goes_on_across_2_pow_32():
    dev = Dev((1 << 32) - 2)
    log = EventLog(dev)
    log.read = lambda n, end=None: [object()] * n
    cursor = EventCursor(log)
    dev.written = (1 << 32) + 3                              # 下位は 3 に戻るが、総数は増え続ける
    events = cursor.take()
    assert len(events) == 5 and cursor.restarts == 0 and cursor.lost == 0
