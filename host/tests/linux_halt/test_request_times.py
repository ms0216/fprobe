"""停止要求の書き込みの FPGA の時刻（イベント記録）から、最初の書き込みから最後の書き込みまでを求める。"""
import pytest

from fprobe.events import ACK_OK, KIND_HOST, KIND_HOST_UNTRACKED, DapEvent
from fprobe.linux_halt.request_times import EventRequestTimes, NotTimed, write_span_ticks

DRCR = [0x80010090, 0x80012090, 0x80014090, 0x80016090]
WRITES = [(a, 1) for a in DRCR]
ACK_WAIT = 0b010


def ev(t, address, data=1, *, kind=KIND_HOST, ap=True, read=False, register=0xC, ack=ACK_OK):
    return DapEvent(time=t, kind=kind, ap=ap, read=read, register=register, ack=ack, parity_error=False,
                    address=address, data=data)


def batch(times):
    """TAR の書き込み + DRW の書き込みの組を、コアごとに（DRW の時刻 = times）。"""
    out = []
    for t, a in zip(times, DRCR):
        out += [ev(t - 5, 0x4, a, register=0x4), ev(t, a)]
    return out


def test_the_span_runs_from_the_first_to_the_last_accepted_halt_write():
    events = [ev(10, 0x80010088, 0, read=True, register=0xC)] + batch([100, 311, 522, 733])
    assert write_span_ticks(events, WRITES) == 633


def test_a_write_the_dp_answered_wait_is_not_the_write_and_its_retry_is():
    events = batch([100, 311, 522, 733])
    events.insert(3, ev(300, DRCR[1], ack=ACK_WAIT))                  # JTAG-DP WAIT: the request was not accepted
    assert write_span_ticks(events, WRITES) == 633


def test_reads_and_other_values_at_the_same_address_are_not_halt_writes():
    events = batch([100, 311, 522, 733]) + [ev(900, DRCR[3], read=True), ev(950, DRCR[3], data=6)]
    assert write_span_ticks(events, WRITES) == 633


def test_a_missing_write_says_which_address():
    events = batch([100, 311, 522, 733])
    del events[5]                                                     # the third core's DRW write
    with pytest.raises(NotTimed, match="0x80014090"):
        write_span_ticks(events, WRITES)


def test_a_write_whose_address_the_fpga_could_not_track_is_not_matched_by_order():
    events = batch([100, 311, 522, 733])
    events[3] = ev(311, 0xC, kind=KIND_HOST_UNTRACKED)
    with pytest.raises(NotTimed, match="track"):
        write_span_ticks(events, WRITES)


def test_two_accepted_writes_of_one_request_are_ambiguous():
    events = batch([100, 311, 522, 733]) + [ev(800, DRCR[0])]
    with pytest.raises(NotTimed, match="0x80010090"):
        write_span_ticks(events, WRITES)


def test_the_48_bit_timestamps_may_wrap_inside_the_batch():
    top = 1 << 48
    events = batch([top - 100, top - 50, 20, 70])
    events = [ev(e.time % top, e.address, e.data, register=e.register) for e in events]
    assert write_span_ticks(events, WRITES) == 170


class Log:
    def __init__(self, events=(), *, lost=0, running=True, capacity=1024):
        self.events, self.lost, self.recording, self.capacity = list(events), lost, running, capacity
        self.overwritten = 0

    def running(self):
        return self.recording

    def counts(self):
        return len(self.events), self.lost

    def read(self, last=None, end=None):
        end = len(self.events) if end is None else end
        n = end if last is None else min(last, end)
        out = self.events[end - n:end]
        self.records = len(out)  # EventLog.records と同じ（この偽物は 1 記録 1 イベント）
        return out


def test_the_span_in_ns_is_rounded_up_from_the_timebase_ticks():
    log = Log()
    times = EventRequestTimes(log, timebase_hz=150_000_000)
    mark = times.mark()
    log.events += batch([100, 311, 522, 733])
    span = times.span(mark, WRITES)
    assert (span.ticks, span.timebase_hz, span.ns) == (633, 150_000_000, 4220)      # 633 / 150 MHz = 4220.0 ns


def test_only_events_after_the_mark_count():
    log = Log(batch([1, 2, 3, 4]))                                    # an earlier stop's writes
    times = EventRequestTimes(log, timebase_hz=100_000_000)
    mark = times.mark()
    log.events += batch([100, 311, 522, 733])
    assert times.span(mark, WRITES).ticks == 633


@pytest.mark.parametrize("spoil, reason", [
    (lambda log: setattr(log, "lost", log.lost + 1), "lost"),
    (lambda log: setattr(log, "recording", False), "not recording"),
    (lambda log: setattr(log, "events", log.events[:1]), "restarted"),
    (lambda log: setattr(log, "capacity", 4), "overwrote"),
])
def test_an_event_log_that_lost_or_overwrote_events_in_the_window_is_not_used(spoil, reason):
    log = Log(batch([1, 2, 3, 4]))
    times = EventRequestTimes(log, timebase_hz=100_000_000)
    mark = times.mark()
    log.events += batch([100, 311, 522, 733])
    spoil(log)
    with pytest.raises(NotTimed, match=reason):
        times.span(mark, WRITES)


def test_events_overwritten_while_reading_are_not_used():
    log = Log()
    times = EventRequestTimes(log, timebase_hz=100_000_000)
    mark = times.mark()
    log.events += batch([100, 311, 522, 733])
    read = log.read

    def read_overwritten(last=None, end=None):
        out = read(last, end)
        log.overwritten = 1
        return out[1:]
    log.read = read_overwritten
    with pytest.raises(NotTimed, match="overwr"):
        times.span(mark, WRITES)


def test_a_build_without_an_event_log_says_so():
    times = EventRequestTimes(None, timebase_hz=100_000_000)
    with pytest.raises(NotTimed, match="no event log"):
        times.span(times.mark(), WRITES)


def test_open_starts_the_event_log_if_it_is_not_recording_and_takes_the_timebase_from_the_capabilities(monkeypatch):
    import fprobe.events
    started = []

    class FakeLog(Log):
        def __init__(self, dev):
            if not dev.has_log:
                raise RuntimeError("このデバイスにはイベント記録が無い")
            super().__init__()

        def ensure_started(self):
            started.append(True)
    monkeypatch.setattr(fprobe.events, "EventLog", FakeLog)
    from caps_fakes import FakeDeviceBase, caps_for
    dev = FakeDeviceBase()
    dev.caps, dev.has_log = caps_for(timebase_hz=150_000_000), True
    times = EventRequestTimes.open(dev)
    assert isinstance(times.log, FakeLog) and started == [True] and times.timebase_hz == 150_000_000
    dev.has_log = False
    times = EventRequestTimes.open(dev)
    assert times.log is None
    with pytest.raises(NotTimed, match="no event log"):
        times.span(times.mark(), WRITES)


def test_the_timebase_frequency_is_required():
    with pytest.raises(ValueError):
        EventRequestTimes(Log(), timebase_hz=0)


# --- コアが自分で止まった停止（event_stop_ticks）----------------------------------------------------------------------

from fprobe.linux_halt.request_times import NeedMoreEvents, event_stop_ticks  # noqa: E402

DSCR = {n: a - 0x8 for n, a in enumerate(DRCR)}
ARGS = dict(dscr=DSCR, drcr=dict(enumerate(DRCR)), hrq=1, rrq=2, halted=1, line_time=True, timebase_hz=100_000_000)


def read(t, address, value):
    """ポストされた DRW の読み（頼んだ転送）と、値を返す RDBUFF の読み。"""
    return [ev(t, address, 0, read=True), ev(t + 1, 0xC, value, ap=False, read=True)]


def test_a_stop_by_a_core_runs_from_its_last_running_read_to_the_last_halt_write():
    events = (read(10, DSCR[0], 0) + read(20, DSCR[1], 0) + read(30, DSCR[0], 1) + read(40, DSCR[1], 0)
              + [ev(50, DRCR[1])])
    span = event_stop_ticks(events, hits=[0], requested=[1], **ARGS)
    assert (span.assumed_ticks, span.lower_core, span.lower_from) == (40, 0, "read")   # 10 → 50
    assert span.measured_ticks is None                                 # 書き込みの後に core 1 の止まった読みが無い
    events += read(60, DSCR[1], 1)
    assert event_stop_ticks(events, hits=[0], requested=[1], **ARGS).measured_ticks == 51   # 10 → 61（値が返った転送）


def test_without_the_halt_write_in_the_events_read_more_is_needed():
    with pytest.raises(NeedMoreEvents, match="core 1"):
        event_stop_ticks(read(10, DSCR[0], 0) + read(30, DSCR[0], 1), hits=[0], requested=[1], **ARGS)


def test_an_untracked_drw_transfer_is_not_attributed_to_a_core():
    events = read(10, DSCR[0], 0) + [ev(20, 0xC, kind=KIND_HOST_UNTRACKED, read=True)] + read(30, DSCR[0], 1) \
        + [ev(50, DRCR[1])]
    with pytest.raises(NotTimed, match="track"):
        event_stop_ticks(events, hits=[0], requested=[1], **ARGS)
