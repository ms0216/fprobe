"""fprobe.recording_stream: ロジアナの流し続けを記録へ（偽のデバイス。ハードウェア不要）。"""

import random
import threading
import time

import pytest

from fprobe import generated as g
from fprobe.la import decode_beats
from fprobe.recording import Recording
from fprobe.recording_long import Policy, SegmentedRecorder
from fprobe.recording_stream import LaRecordingStream, LockedDevice, anchor_stream
from test_la_stream import FakeStreamDevice, raw_source, rle_source
from caps_fakes import FakeDeviceBase, with_caps

HZ = 100_000_000


class TimedStreamDevice(FakeStreamDevice):
    """タイムベースと、深いバッファのトリガ時刻（条件なしのトリガ = 最初のサンプル）を足した偽物。"""

    def __init__(self, *args, trigger: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.now = 5_000_000
        self.trigger = trigger
        self.trigger_time = 0

    def time(self):
        self.now += 1000                                  # 読むたびに 10 µs 進む
        return self.now

    def write32(self, address, value):
        if address == g.LA_DEEP_CONTROL and value & 1 and not self.running:
            self.trigger_time = self.now + 300
        super().write32(address, value)

    def read32(self, address):
        if address == g.LA_DEEP_STATUS:
            return super().read32(address) | int(self.trigger) << g.LA_DEEP_STATUS_TRIGGERED[0]
        return super().read32(address)

    def read(self, address, count, *, timeout_retries=0):
        if address == g.LA_DEEP_TRIGGER_TIME_LO and count == 2:
            return [self.trigger_time & 0xFFFF_FFFF, self.trigger_time >> 32]
        return super().read(address, count)


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)


def _expand(recording: Recording) -> list[int]:
    out = []
    for _, _, tr in recording.waveforms():
        for i, (v, s) in enumerate(zip(tr.values, tr.starts)):
            nxt = tr.starts[i + 1] if i + 1 < len(tr.starts) else tr.length
            out.extend([v] * (nxt - s))
    return out


@pytest.mark.parametrize("python", [False, True], ids=["numpy", "python"])
@pytest.mark.parametrize("rle,trigger", [(False, True), (True, False)])
def test_stream_lands_in_blocks_with_the_right_time(tmp_path, monkeypatch, rle, trigger, python):
    if python:                                               # numpy の無い道（変化点を array で並べる）でも同じ記録
        from fprobe import la, la_wave, recording_stream
        for module in (la, la_wave, recording_stream):
            monkeypatch.setattr(module, "_np", None)
    rng = random.Random(3 + rle)
    source = (rle_source if rle else raw_source)(rng, 6000)
    dev = TimedStreamDevice(source, rng=rng, trigger=trigger)
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=3600))
    lock = threading.RLock()
    s = LaRecordingStream(dev, rec, rle=rle, chunk_s=5e-5, lock=lock)
    anchor = s.start()
    deadline = time.monotonic() + 5
    while dev.written * 16 < len(source) and time.monotonic() < deadline:
        time.sleep(0.005)
    status = s.stop()
    rec.close()
    assert not status["running"] and status["error"] is None and status["blocks"] > 3
    # サンプル 0 の時刻は回路が記録しない: 前後の読みの範囲（トリガがあればその時刻が上の端）だけを出し、中点に置く
    # （偽物は LA_DELAY を申告しないので段数 0）
    if trigger:
        assert anchor.method == "trigger" and anchor.t_hi == dev.trigger_time and anchor.t_lo == dev.trigger_time - 300
    else:
        assert anchor.method == "host" and anchor.t_hi - anchor.t_lo == 1000 and anchor.uncertainty == 500
    assert anchor.ticks == (anchor.t_lo + anchor.t_hi) // 2
    r = Recording.open(tmp_path / "r")
    assert _expand(r) == list(decode_beats(source[:dev.written * 16], rle))
    chunks = list(r.waveforms())
    assert chunks[0][0] == anchor.ticks
    for (t_a, tps, tr), (t_b, _, _) in zip(chunks, chunks[1:]):
        assert t_b == t_a + round(tr.length * tps)                   # かたまりは隙間なくつながる
    kinds = [e.kind for e in r.events(streams=["la"])]
    assert kinds == ["stream_start", "stream_stop"]


class TimedPackDevice(TimedStreamDevice):
    """チャンネルを詰めて流せる（ケイパビリティ la_pack）偽物。"""

    def capabilities(self):
        return with_caps(super().capabilities(), la_pack=True)


@pytest.mark.parametrize("channels, pack", [(8, 1), (4, 2)])
def test_packed_stream_lands_in_the_recording_as_every_sample_of_the_selected_channels(tmp_path, channels, pack):
    # 画面の Start Recording が使う道: 詰めて流しても、記録には展開した全サンプルが入り、流れの開始の記録に詰めたチャンネル数が残る
    from test_la_stream_pack import packed_source
    rng = random.Random(50 + pack)
    source, samples = packed_source(rng, 4000, pack)
    dev = TimedPackDevice(source, rng=rng, trigger=True)
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=3600))
    s = LaRecordingStream(dev, rec, rle=False, chunk_s=5e-5, lock=threading.RLock(), pack_channels=channels)
    s.start()
    deadline = time.monotonic() + 5
    while dev.written * 16 < len(source) and time.monotonic() < deadline:
        time.sleep(0.005)
    status = s.stop()
    rec.close()
    assert not status["running"] and status["error"] is None and status["blocks"] > 1
    r = Recording.open(tmp_path / "r")
    got = _expand(r)
    assert got and got == samples[:len(got)]
    start = [e for e in r.events(streams=["la"]) if e.kind == "stream_start"][0]
    assert start.body["channels"] == channels and start.body["pack_channels"] == channels


def test_locked_device_takes_the_lock_per_call():
    seen = []

    class Dev(FakeDeviceBase):
        value = 7

        def read32(self, address):
            seen.append(lock._is_owned())
            return address

    lock = threading.RLock()
    d = LockedDevice(Dev(), lock)
    assert d.read32(5) == 5 and seen == [True] and d.value == 7


def test_anchor_falls_back_to_host_when_trigger_time_is_outside(tmp_path):
    dev = TimedStreamDevice(raw_source(random.Random(1), 10), rng=random.Random(1), trigger=True)

    class Late:
        sample_period_s = 1e-8

    def start():
        dev.trigger_time = 10 ** 12                                  # 前後の読みの外: 信じない
        return Late()

    _, anchor = anchor_stream(dev, start, HZ)
    assert anchor.method == "host"
