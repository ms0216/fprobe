"""fprobe.collector: デバイスの出来事（捕捉・イベント記録・時計）を記録へ取り込む。偽のデバイスで試験する。"""

import time
from array import array

from fprobe.collector import Collector, add_capture, add_events, unwrap48
from fprobe.events import KIND_AUX, KIND_HOST, KIND_UART_RX, DapEvent
from fprobe.la import Transitions
from fprobe.recording import Recorder, Recording

HZ = 100_000_000


def test_48_bit_event_times_are_extended_with_the_reference():
    ref = (5 << 48) | 1000                         # いまのタイムベース（上位が 5）
    assert unwrap48(900, ref) == (5 << 48) | 900
    assert unwrap48((1 << 48) - 10, ref) == (4 << 48) | ((1 << 48) - 10)     # 48 ビットが一周する前の出来事


def test_a_capture_is_placed_by_its_trigger_time(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    tr = Transitions(array("H", [0, 1]), array("Q", [0, 30]), 100, 30, 10_000, 1e-8, 1)   # トリガ = サンプル 30 = 時刻 10000
    add_capture(rec, tr)
    rec.flush()
    r = Recording.open(rec.directory)
    assert r.span() == (10_000 - 30, 10_000 + 70)
    assert r.waveform_view(9_970, 10_000, 1)[0] == "0" and r.waveform_view(10_000, 10_070, 1)[0] == "1"   # トリガで変わる


def ap_read(t, address, data):
    return DapEvent(t, KIND_HOST, True, True, 0xC, 0b001, False, address, data)


def test_events_become_dap_uart_and_aux_records_and_posted_reads_carry_over(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    ref = 1 << 40
    batch1 = [DapEvent(10, KIND_UART_RX, False, False, 0, 0, False, 0, 0x41),
              DapEvent(20, KIND_AUX, False, False, 0, 0, False, 0, (1 << 16) | 3300),
              ap_read(30, 0x2000_0000, 0)]                         # 値はまだ返っていない（ポスト）
    carry = add_events(rec, batch1, ref)
    batch2 = [ap_read(40, 0x2000_0004, 0x1234)]                    # 前の読みの値がここで返る
    add_events(rec, batch2, ref, carry=carry)
    rec.flush()
    got = [(e.t & 0xFFFF_FFFF_FFFF, e.stream, e.kind, e.a, e.b) for e in Recording.open(rec.directory).events()]
    assert got == [(10, "uart", "rx", 0, 0x41), (20, "aux", "mv", 1, 3300), (40, "dap", "ap_read", 0x2000_0000, 0x1234)]


class FakeDevice:
    """time() と、EventLog の代わりの読み出し（written を増やして返す）。"""

    def __init__(self):
        self.ticks = 1_000_000
        self.written = 0
        self.log: list[DapEvent] = []

    def time(self) -> int:
        self.ticks += 1000
        return self.ticks


class FakeEvents:
    capacity = 1024

    def __init__(self, dev):
        self.dev = dev

    def counts(self):
        return len(self.dev.log), 0

    def read(self, last=None, end=None):
        log = self.dev.log[:end]
        return log[-last:] if last else list(log)


def test_the_collector_adds_clock_points_and_new_events_while_running(tmp_path):
    dev = FakeDevice()
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    c = Collector(dev, rec, events=FakeEvents(dev), poll_s=0.02, clock_s=0.02)
    c.start()
    dev.log.append(DapEvent(5, KIND_UART_RX, False, False, 0, 0, False, 0, 0x42))
    time.sleep(0.2)
    dev.log.append(DapEvent(6, KIND_UART_RX, False, False, 0, 0, False, 0, 0x43))
    time.sleep(0.2)
    c.stop()
    r = Recording.open(rec.directory)
    assert [e.b for e in r.events(streams=["uart"])] == [0x42, 0x43]          # 同じ出来事を 2 度入れない
    assert len(r.events(streams=["clock"])) >= 3
    t, unc = rec.clock.to_ticks(time.monotonic())
    assert unc >= 0 and t > 1_000_000


class ArrivingEvents(FakeEvents):
    """読むたびに、その直前に新しい出来事が 1 件届く（数を読んでから中身を読むまでの間に書かれる）。read は本物の
    EventLog と同じく、end（省略時は読んだ時点の総数）で終わる last 件を返す。"""

    def __init__(self, dev):
        super().__init__(dev)
        self.next = 0x10

    def read(self, last=None, end=None):
        self.dev.log.append(DapEvent(7, KIND_UART_RX, False, False, 0, 0, False, 0, self.next))
        self.next += 1
        end = len(self.dev.log) if end is None else end
        return self.dev.log[end - last:end]


def test_events_arriving_during_a_read_are_taken_once_each(tmp_path):
    """数を読んでから中身を読むまでに届いた出来事で、読む範囲がずれない（2026-10-18 に実機で、記録中の SWO のバイトが
    欠けたり二重になったりした）。"""
    dev = FakeDevice()
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    events = ArrivingEvents(dev)
    c = Collector(dev, rec, events=events, poll_s=10, clock_s=10)
    dev.log.append(DapEvent(5, KIND_UART_RX, False, False, 0, 0, False, 0, 0x42))
    for _ in range(3):
        c.poll()
    rec.flush()
    got = [e.b for e in Recording.open(rec.directory).events(streams=["uart"])]
    assert got == [0x42, 0x10, 0x11]                                            # 最後に届いた 0x12 は次の回
