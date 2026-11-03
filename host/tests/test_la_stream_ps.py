"""ロジアナのストリームを PS が DDR のリングを直接読む形（ケイパビリティ la_stream_ps）で始める判断（偽のデバイス。
ハードウェア不要）。仕様: docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md §3.5"""

import random
import time

import pytest

from fprobe import generated as g
from fprobe.la import DeepAnalyzer
from test_la_stream import raw_source
from test_la_stream_events import FakeMixedDevice, framed
from caps_fakes import FakeDeviceBase, with_caps

PS_READ = 1 << g.LA_DEEP_CONTROL_PS_READ[0]


class Dev(FakeMixedDevice):
    """la_stream_ps の有無を選べる偽のデバイス（流れそのものは窓経由の形のまま。確かめるのは開始の書き込み）。"""

    def __init__(self, *a, has_ps, **kw):
        super().__init__(*a, **kw)
        self.has_ps = has_ps

    def capabilities(self):
        caps = super().capabilities()                                 # has_ps = None: TLV の旗が無い古い機器（既定の意味）
        return caps if self.has_ps is None else with_caps(caps, la_stream_ps=self.has_ps)


def start_control(has_ps, **stream_kw):
    rng = random.Random(1)
    data, ends = framed(rng, raw_source(rng, 100), 0, 5)
    dev = Dev(data, ends, rng=rng, has_ps=has_ps)
    stream = DeepAnalyzer(dev).stream(rle=False, **stream_kw)
    list(stream.chunks(0.02))
    return next(v for v in dev.control_writes if v & 1)               # run を立てる書き込み（その前の 0 は停止）


def test_a_device_that_can_be_read_by_the_ps_starts_the_stream_in_that_mode():
    value = start_control(True)
    assert value & PS_READ and value & 1 and value >> g.LA_DEEP_CONTROL_STREAM[0] & 1     # run・stream と同じ 1 回の書き


def test_a_device_without_the_capability_keeps_the_stream_through_the_window():
    assert not start_control(False) & PS_READ
    assert not start_control(None) & PS_READ                         # ケイパビリティの項目が無い古い機器


def test_the_caller_can_choose_the_mode_explicitly():
    assert not start_control(True, ps_read=False) & PS_READ
    assert start_control(True, ps_read=True) & PS_READ


def test_asking_for_ps_read_on_a_device_that_cannot_do_it_is_an_error():
    rng = random.Random(1)
    data, ends = framed(rng, raw_source(rng, 100), 0, 5)
    dev = Dev(data, ends, rng=rng, has_ps=False)
    with pytest.raises(RuntimeError, match="la_stream_ps"):
        DeepAnalyzer(dev).stream(rle=False, ps_read=True)
    assert dev.control_writes == []                                  # 何も書かずに断る


class StuckDevice(FakeDeviceBase):
    """止めても「読み出し中」が下がらず、データも届かない機器（PS 直読みを提供できない中継の先の PL 等）。"""

    def __init__(self):
        self.control_writes = []

    def read32(self, address):
        if address == g.LA_DEEP_STATUS:
            return 1 << g.LA_DEEP_STATUS_READING[0]
        return 0                                                      # stream_beats など

    def read(self, address, count, *, timeout_retries=0):
        return [self.read32(address + 4 * i) for i in range(count)]

    def write32(self, address, value):
        if address == g.LA_DEEP_CONTROL:
            self.control_writes.append(value)

    def read_stream_some(self, size, timeout_ms):
        raise AssertionError("届く量が 0 のときは読まない")


def test_a_stream_that_never_drains_after_the_stop_times_out_instead_of_hanging():
    from fprobe.la import LaStream
    dev = StuckDevice()
    stream = LaStream(dev, False, 1e-8, 16)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="出し切らない"):
        list(stream.chunks(0.0, timeout_ms=300))
    assert 0.25 <= time.monotonic() - started < 3.0
    assert dev.control_writes and dev.control_writes[-1] == 0          # 止める書き込みは済んでいる
