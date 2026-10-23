"""fprobe.collector: デバイスの出来事（捕捉・イベント記録・時計）を記録へ取り込む。偽のデバイスで試験する。"""

import time
from array import array

from fprobe.collector import Collector, add_capture, add_events, unwrap48
from fprobe.events import KIND_AUX, KIND_HOST, KIND_INJECT_TRANSFERS, KIND_SYNC, KIND_UART_RX, DapEvent
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



def test_injected_writes_are_recorded_as_inject_with_the_transfer_count(tmp_path):
    # R20: 割り込み書き込みは "inject"（ホストの ap_write と区別）、本体に転送数。ホストのポストの読みの連鎖は切らない
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    ref = 1 << 40
    carry = add_events(rec, [ap_read(30, 0x2000_0000, 0),
                             DapEvent(35, KIND_INJECT_TRANSFERS, True, False, 0xC, 0b001, False, 0xE000EDF0, 0xA05F0003,
                                      transfers=7)], ref)
    add_events(rec, [ap_read(40, 0x2000_0004, 0x1234)], ref, carry=carry)
    rec.flush()
    got = [(e.t & 0xFFFF_FFFF_FFFF, e.stream, e.kind, e.a, e.b, e.body) for e in Recording.open(rec.directory).events()]
    assert got == [(35, "dap", "inject", 0xE000EDF0, 0xA05F0003, {"ack": 1, "transfers": 7}),
                   (40, "dap", "ap_read", 0x2000_0000, 0x1234, {"ack": 1})]


def test_sync_pairs_become_sync_rows_with_the_64_bit_parent_time(tmp_path):
    """種類 10: 時刻 = 子の時刻（48 ビット → 参照で 64 ビット）、address = 親の時刻の下位 32、data = 上位 32。"""
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    ref = (3 << 48) | 100                                      # 子の今の時刻: 48 ビットが一周した直後
    parent = (0x8123_4567 << 32) | 0x89AB_CDEF                 # 最上位ビットも立つ親の時刻
    add_events(rec, [DapEvent((1 << 48) - 5, KIND_SYNC, False, False, 0, 0, False, parent & 0xFFFF_FFFF, parent >> 32),
                     DapEvent(50, KIND_SYNC, False, False, 0, 0, False, 7, 0)], ref)
    rec.flush()
    got = [(e.t, e.stream, e.kind, e.a, e.b, e.body) for e in Recording.open(rec.directory).events()]
    assert got == [((2 << 48) | ((1 << 48) - 5), "sync", "pair", 0x89AB_CDEF, 0x8123_4567, {"parent": parent}),
                   ((3 << 48) | 50, "sync", "pair", 7, 0, {"parent": 7})]


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


def test_fpga_observation_events_become_fpga_rows(tmp_path):
    # 記録中に種類 8・9 が来ても止まらない（KeyError だった）。値・溢れの切れ目・組の切り替えを stream fpga に。
    # 値の時刻は印が来てから決まる（観測の回路 v2）。印 1 は鎖の時刻 5 = fprobe 3005、傾きは申告の比 1
    from fprobe.events import KIND_OBS_RECORD, KIND_OBS_SYNC
    from fprobe.fpga_obs import ObsDecoder

    def rec8(kind, payload, t):
        return DapEvent(t, KIND_OBS_RECORD, False, False, 0, 0, False, 0, kind << 30 | payload)

    def sync9(seq, t):
        return DapEvent(t, KIND_OBS_SYNC, False, False, 0, 0, False, 0, seq)

    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    ref = 1 << 40
    obs = ObsDecoder(nominal_ratio=1.0)
    add_events(rec, [rec8(3, 0, 3000), rec8(0, 0x11, 3000), sync9(1, 3005), rec8(1, 5 << 16 | 1 << 15 | 1 << 7, 3100)],
               ref, obs=obs)
    add_events(rec, [rec8(2, 7, 4000), rec8(3, 1, 4000), rec8(0, 0x22, 4000), sync9(2, 4010),
                     rec8(1, 10 << 16 | 1 << 15 | 2 << 7, 4100),
                     DapEvent(5000, KIND_UART_RX, False, False, 0, 0, False, 0, 0x41)], ref, obs=obs)
    rec.flush()
    got = [(e.t & 0xFFFF_FFFF_FFFF, e.stream, e.kind, e.a, e.b) for e in Recording.open(rec.directory).events()]
    assert got == [(3000, "fpga", "group", 0, 0), (3000, "fpga", "value", 0, 0x11),
                   (4000, "fpga", "gap", 7, 0), (4000, "fpga", "group", 1, 0), (4000, "fpga", "value", 1, 0x22),
                   (5000, "uart", "rx", 0, 0x41)]

def test_fpga_rows_without_a_sync_keep_their_received_time(tmp_path):
    # R17: 印の来ないまま終わった鎖の組・値も捨てない。別の種類（*_untimed）で、時刻は「受けた時」（起きた時刻の上限）
    from fprobe.events import KIND_OBS_RECORD, KIND_OBS_SYNC
    from fprobe.fpga_obs import ObsDecoder

    def rec8(kind, payload, t):
        return DapEvent(t, KIND_OBS_RECORD, False, False, 0, 0, False, 0, kind << 30 | payload)

    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    ref = 1 << 40
    obs = ObsDecoder(nominal_ratio=1.0)
    add_events(rec, [rec8(3, 0, 1000), rec8(0, 0x09, 1000),                       # 鎖 A（印が来ない）
                     rec8(3, 0, 2000), rec8(2, 4, 2000), rec8(0, 0x0A, 2000),     # 組 → 溢れ 4 → 鎖 B
                     DapEvent(2005, KIND_OBS_SYNC, False, False, 0, 0, False, 0, 1),
                     rec8(1, 5 << 16 | 1 << 15 | 1 << 7, 2100)], ref, obs=obs)
    rec.flush()
    got = [(e.t & 0xFFFF_FFFF_FFFF, e.kind, e.a, e.b) for e in Recording.open(rec.directory).events(streams=["fpga"])]
    assert (1000, "group_untimed", 0, 0) in got and (1000, "value_untimed", 0, 0x09) in got
    assert [g[1:] for g in got if not g[1].endswith("_untimed")] == [("gap", 4, 0), ("value", 0, 0x0A)]


def test_stopping_flushes_the_values_of_an_unsynced_chain(tmp_path):
    from fprobe.collector import flush_obs
    from fprobe.events import KIND_OBS_RECORD
    from fprobe.fpga_obs import ObsDecoder
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    obs = ObsDecoder(nominal_ratio=1.0)
    add_events(rec, [DapEvent(700, KIND_OBS_RECORD, False, False, 0, 0, False, 0, 3 << 30),
                     DapEvent(700, KIND_OBS_RECORD, False, False, 0, 0, False, 0, 0x33)], 1 << 40, obs=obs)
    flush_obs(rec, obs, 1 << 40)                                  # 鎖は終わっていないが、記録は止める
    rec.flush()
    got = [(e.t & 0xFFFF_FFFF_FFFF, e.kind, e.b) for e in Recording.open(rec.directory).events(streams=["fpga"])]
    assert got == [(700, "group_untimed", 0), (700, "value_untimed", 0x33)]


class FailingOnceEvents(FakeEvents):
    """1 回だけ読み出しが途中で止まる（2026-10-21 の実機: ストリームが 42432/44992 バイトで止まり、取り込みのスレッドが
    例外で終わって、記録は「記録中」のまま何も入らなくなった）。"""

    def __init__(self, dev):
        super().__init__(dev)
        self.fail = True

    def read(self, last=None, end=None):
        if self.fail:
            self.fail = False
            raise TimeoutError("ストリームが 42432/44992 バイトで止まった")
        return super().read(last, end)


def test_a_failed_read_does_not_stop_the_recording(tmp_path):
    dev = FakeDevice()
    dev.read_stream_some = lambda n, timeout_ms=0: (_ for _ in ()).throw(TimeoutError())   # 残りは無い
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    c = Collector(dev, rec, events=FailingOnceEvents(dev), poll_s=0.02, clock_s=10)
    c.start()
    dev.log.append(DapEvent(5, KIND_UART_RX, False, False, 0, 0, False, 0, 0x42))   # これを読むところで失敗
    time.sleep(0.15)
    dev.log.append(DapEvent(6, KIND_UART_RX, False, False, 0, 0, False, 0, 0x43))
    time.sleep(0.15)
    c.stop()
    r = Recording.open(rec.directory)
    assert [e.b for e in r.events(streams=["uart"])] == [0x43]                # 失敗の後も取り込み続ける
    assert c.errors == 1 and "42432" in c.last_error and c.lost == 1          # 読めなかった分は欠落として数える
    assert [e.kind for e in r.events(streams=["annotation"])] == ["events_error"]


def test_the_collector_survives_failures_outside_the_event_read_and_stop_still_returns(tmp_path):
    # 時計の点・flush・立て直しの中の機器の読みが失敗しても取り込みのスレッドは止まらない。stop の最後の取り込みが
    # 失敗しても例外を出さない（出すと recording_stop が記録を閉じられず「記録中」のまま残る）
    dev = FakeDevice()
    calls = {"n": 0}
    real_time = dev.time

    def flaky_time():
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise TimeoutError("UDP が届かない")
        return real_time()
    dev.time = flaky_time
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    events = FailingOnceEvents(dev)
    c = Collector(dev, rec, events=events, poll_s=0.01, clock_s=0.01)
    c.start()
    dev.log.append(DapEvent(5, KIND_UART_RX, False, False, 0, 0, False, 0, 0x42))
    time.sleep(0.2)
    dev.log.append(DapEvent(6, KIND_UART_RX, False, False, 0, 0, False, 0, 0x43))
    time.sleep(0.2)
    assert c._thread.is_alive()
    events.read = lambda last=None, end=None: (_ for _ in ()).throw(TimeoutError("切れた"))
    dev.log.append(DapEvent(7, KIND_UART_RX, False, False, 0, 0, False, 0, 0x44))
    c.stop()                                                                     # 例外を出さない
    assert c.errors >= 1


def test_stopping_keeps_the_sync_counts_in_the_recording_meta(tmp_path):
    # 同期の CRC 異常で捨てた組の数は CSR にしか無い: 記録を止めるときに meta に事実として残す（モジュール化 M3）
    from types import SimpleNamespace

    from fprobe import generated as g

    class SyncDevice(FakeDevice):
        def capabilities(self):
            return SimpleNamespace(sync=True)

        def read32(self, address):
            assert address == g.SYNC_STATUS
            return 3 << 16 | 120                                      # crc_errors 3・pairs 120

    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    c = Collector(SyncDevice(), rec, events=FakeEvents(FakeDevice()), poll_s=0.02, clock_s=0.02)
    c.start()
    c.stop()
    rec.close()
    assert Recording.open(rec.directory).meta["sync"] == {"pairs_mod_65536": 120, "crc_errors": 3}


def test_devices_without_sync_leave_no_sync_meta(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    c = Collector(FakeDevice(), rec, events=FakeEvents(FakeDevice()), poll_s=0.02, clock_s=0.02)
    c.start()
    c.stop()
    rec.close()
    assert "sync" not in Recording.open(rec.directory).meta


def test_untimed_fpga_rows_are_unknown_in_the_timeline_and_counted_by_correlate(tmp_path):
    from fprobe.timeline_fpga import expand
    from fprobe.fpga_signals import FpgaSignals
    from fprobe.recording import Event
    events = [Event(100, "fpga", "value", 0, 5, None), Event(200, "fpga", "value_untimed", 0, 9, None),
              Event(300, "fpga", "value", 0, 6, None)]
    out = expand(events, FpgaSignals.from_dict(None))
    assert any(e.t == 200 and (e.body or {}).get("unknown") for e in out)     # 時刻の決まらない記録の所で値は不明
