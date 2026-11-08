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


class TimedTokenDevice(TimedStreamDevice):
    """RLE のトークン形式を申告する偽物（ケイパビリティ la_rle_tokens）。"""

    def capabilities(self):
        from fprobe import la_tokens as T
        from fprobe.caps import TokenInfo
        return with_caps(super().capabilities(), la_rle_tokens=True, la_rle_token_info=TokenInfo(T.TOKEN_FORMAT_VERSION, False, False, 16))


@pytest.mark.parametrize("kind", ["hold", "clock_slow", "sparse"])
def test_token_stream_lands_in_the_recording_and_reports_the_format_facts(tmp_path, kind):
    # 画面の Record Pins（RLE の形式 = tokens）が使う道: 受け取りで変化点に復号して記録に入り、形式の事実が status に出る
    from fprobe import la_tokens as T
    from test_la_tokens import random_signal
    rng = random.Random(70)
    samples = random_signal(rng, 40000, kind)
    source = T.encode_tokens(samples)
    dev = TimedTokenDevice(source, rng=rng, trigger=False)
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=3600))
    s = LaRecordingStream(dev, rec, rle=True, chunk_s=5e-5, lock=threading.RLock(), rle_format="tokens", tick_us=1000)
    s.start()
    deadline = time.monotonic() + 5
    while dev.written * 16 < len(source) and time.monotonic() < deadline:
        time.sleep(0.005)
    status = s.stop()
    rec.close()
    assert not status["running"] and status["error"] is None and status["blocks"] >= 1
    got = source[:dev.written * 16]
    whole = T.decode_tokens(got)
    assert status["rle_format"] == "tokens" and status["wire_bytes"] == len(got) and status["pad_bytes"] == whole.pad_bytes
    assert status["changes"] == len(whole.values) and status["bytes_per_change"] == len(got) / len(whole.values)
    r = Recording.open(tmp_path / "r")
    expanded = _expand(r)
    assert expanded and expanded == samples[:len(expanded)] and len(expanded) >= whole.end_time
    start = [e for e in r.events(streams=["la"]) if e.kind == "stream_start"][0]
    assert start.body["rle"] is True and start.body["rle_format"] == "tokens"


def _run_token_stream(tmp_path, source, **kw):
    rng = random.Random(1)
    dev = TimedTokenDevice(source, rng=rng, trigger=False)
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=3600))
    s = LaRecordingStream(dev, rec, rle=True, chunk_s=1.0, lock=threading.RLock(), rle_format="tokens", tick_us=1000, **kw)
    s.start()
    deadline = time.monotonic() + 5
    while dev.written * 16 < len(source) and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.05)
    status = s.stop()
    rec.close()
    return Recording.open(tmp_path / "r"), status


def test_repeat_stream_is_cut_into_bounded_blocks_with_the_same_waveform(tmp_path, monkeypatch):
    # REPEAT は 1 トークンが大量の変化点になる。展開した変化点を 1 つのかたまりに溜めず、一定数ごとに閉じる（波形は同じ）
    from fprobe import la_tokens as T
    from test_la_tokens import clock
    monkeypatch.setattr("fprobe.recording_stream.PIECE_CHANGES", 300)
    monkeypatch.setattr("fprobe.recording_stream.BLOCK_CHANGES", 1000)
    samples = clock(20000, 1) + clock(3000, 5)                           # 毎サンプル変化（2 万変化）+ 周期 10
    source = T.encode_tokens(samples, repeat=True)
    assert T.count_repeats(source) > 0 and len(source) < 400
    r, status = _run_token_stream(tmp_path, source)
    assert status["error"] is None and status["blocks"] >= 15           # 1 秒のかたまりまで待たず、変化点の数で閉じた
    expanded = _expand(r)
    whole = T.decode_tokens(source)
    assert status["changes"] == len(whole.values)
    assert expanded and expanded == samples[:len(expanded)] and len(expanded) >= whole.end_time


def test_max_changes_stops_the_recording_with_the_facts_and_keeps_what_was_recorded(tmp_path, monkeypatch):
    from fprobe import la_tokens as T
    from test_la_tokens import clock
    monkeypatch.setattr("fprobe.recording_stream.PIECE_CHANGES", 300)
    monkeypatch.setattr("fprobe.recording_stream.BLOCK_CHANGES", 1000)
    samples = clock(20000, 1)
    source = T.encode_tokens(samples, repeat=True)
    r, status = _run_token_stream(tmp_path, source, max_changes=5000)
    assert "ChangeLimitExceeded" in status["error"] and "max_changes = 5000" in status["error"]
    assert status["changes"] == 5000
    expanded = _expand(r)
    assert len(expanded) >= 4999 and expanded == samples[:len(expanded)]
    with pytest.raises(ValueError):
        LaRecordingStream(TimedTokenDevice(source, rng=random.Random(1), trigger=False), None, rle=True, max_changes=0)


def test_records_stream_reports_its_format_and_does_not_count_changes_it_never_decoded(tmp_path):
    rng = random.Random(8)
    source = rle_source(rng, 3000)
    dev = TimedStreamDevice(source, rng=rng, trigger=False)
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=3600))
    s = LaRecordingStream(dev, rec, rle=True, chunk_s=5e-5, lock=threading.RLock())
    s.start()
    deadline = time.monotonic() + 5
    while dev.written * 16 < len(source) and time.monotonic() < deadline:
        time.sleep(0.005)
    status = s.stop()
    rec.close()
    assert status["rle_format"] == "records" and status["wire_bytes"] == dev.written * 16 and status["pad_bytes"] == 0
    assert status["changes"] is None and status["bytes_per_change"] is None      # 生の拍で保存する: 復号して数えていない


def test_token_stream_is_refused_with_the_reason_when_the_device_does_not_declare_it(tmp_path):
    rng = random.Random(9)
    dev = TimedStreamDevice(rle_source(rng, 100), rng=rng, trigger=False)       # la_rle_tokens を申告しない
    rec = SegmentedRecorder(tmp_path / "r", HZ, policy=Policy(segment_s=3600))
    s = LaRecordingStream(dev, rec, rle=True, chunk_s=1.0, lock=threading.RLock(), rle_format="tokens", tick_us=1000)
    with pytest.raises(RuntimeError, match="la_rle_tokens"):
        s.start()
    s = LaRecordingStream(TimedTokenDevice(b"", rng=rng, trigger=False), rec, rle=False, chunk_s=1.0, lock=threading.RLock(),
                          rle_format="tokens", tick_us=1000)
    with pytest.raises(ValueError, match="rle = True"):
        s.start()
    rec.close()


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
