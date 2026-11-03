"""イベント記録を読む間に、書き手が読み手を追い越した分（リングが小さい構成で起きる）を捨てて数える。
docs/superpowers/specs/2026-10-20-bram-event-ring-design.md（レビューの指摘）。"""

from fprobe import generated as g
from fprobe.events import EVENT_BYTES, EventLog
from caps_fakes import FakeDeviceBase


def event(i: int) -> bytes:
    # 時刻 = 書いた番号、種類 4（UART のバイト）
    return (4 << 120 | i).to_bytes(EVENT_BYTES, "little")


class RingDevice(FakeDeviceBase):
    """容量 capacity のリング。read_stream の間に advance 件だけ新しいイベントが書かれる（遅いリンクで読む間に追い越される）。"""

    def __init__(self, capacity: int, written: int, advance: int):
        self.capacity, self.written, self.advance = capacity, written, advance
        self.first = self.count = 0

    def read32(self, address):
        assert address == g.EVENTS_CAPACITY
        return self.capacity

    def time(self):
        return 1 << 40                  # events_read がイベントの時刻を 64 ビットに延ばす基準（イベントの時刻より後）

    def read(self, address, count=1, *, timeout_retries=0):
        assert address == g.EVENTS_WRITTEN and count == 4
        return [self.written & 0xFFFFFFFF, 0, self.capacity, self.written >> 32]

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


class PathDevice(RingDevice):
    """読みの道の選び（select_stream_path）と命令の書き込みの順を記録する。"""

    def __init__(self, *a):
        super().__init__(*a)
        self.calls = []

    def select_stream_path(self, ps_read):
        self.calls.append(("path", ps_read))

    def write32(self, address, value):
        if address == g.LA_DEEP_COMMAND:
            self.calls.append(("command", value))


def test_the_event_read_selects_the_window_path_before_the_command():
    # イベントの読み出しは窓のフレームで届く。TCP の流れがつながっていると受け口は TCP の道が既定なので、命令の前に窓の道を
    # 選ばないと、届いたフレームを捨てて 0 バイトで止まる（2026-11-03 に実機で、--fpga-request-times の停止で起きた）
    dev = PathDevice(8, 4, 0)
    EventLog(dev).read()
    assert dev.calls == [("path", False), ("command", 1 << g.LA_DEEP_COMMAND_READ[0])]
