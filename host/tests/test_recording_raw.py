"""記録の波形を生の拍で保存する（記録の形式の版 3。docs/superpowers/specs/2026-11-03-recording-raw-waveform-design.md）。

同じ流れ（RAW・RLE・詰め・欠け・ch ごとのずれ）を、変化点で保存する道（形式 1。変更前の道）と生の拍で保存する道（形式 2）で記録に
入れ、読みの答え（変化点・見え方・VCD・CSV・JSON・出来事・縮図の事実）が同じになることを確かめる。流れは偽のストリームの
チャンクを決まった順で渡す（スレッドも時計も使わない）。
"""

import random
import sqlite3
from array import array
from types import SimpleNamespace

import pytest

from fprobe import la, la_wave
from fprobe.la import StreamChunk, StreamGap
from fprobe.recording import Recorder, Recording
from fprobe.recording_long import Policy, SegmentedRecorder
from fprobe.recording_stream import LaRecordingStream
from fprobe.recording_table import FORMAT_VERSION, RecordingVersionError
from caps_fakes import caps_for
from recording_answers import answers

HZ = 100_000_000


def beats(rng, rle: bool, pack: int, n: int) -> bytes:
    if rle:
        return array("I", [rng.choice([0, 0, rng.randrange(60)]) << 16 | rng.choice([1, 1, 2, rng.randrange(1 << 16)])
                           for _ in range(n * 4)]).tobytes()
    width = 16 >> pack
    words = array("H")
    for _ in range(n * 8):
        w = 0
        for j in range(1 << pack):
            w |= (rng.choice([3, 3, 3, rng.randrange(1 << width)]) & ((1 << width) - 1)) << (width * j)
        words.append(w)
    return words.tobytes()


def make_chunks(rng, rle: bool, pack: int, count: int, gaps: bool) -> list[StreamChunk]:
    """LaStream が返すのと同じ約束のチャンク（first・previous・count が続く。欠けの後は previous = None、gap を持つ）。"""
    out, first, previous = [], 0, None
    for _ in range(count):
        gap = None
        if gaps and out and rng.random() < 0.25:
            skip = rng.randint(1, 300)
            gap = StreamGap(first, skip, 0, 0)
            first, previous = first + skip, None
        data = beats(rng, rle, pack, rng.randint(1, 12))
        n, last = la_wave._samples_of(data, rle, pack)
        out.append(StreamChunk(data, rle, first, previous, pack=pack, gap=gap, count=n))
        first, previous = first + n, last
    if gaps and rng.random() < 0.3:                           # 欠けで終わる流れ（中身の無いチャンク）
        skip = rng.randint(1, 50)
        out.append(StreamChunk(b"", rle, first + skip, None, pack=pack, gap=StreamGap(first, skip, 0, 0), count=0))
    return out


class FakeStream:
    """LaRecordingStream._run が使う分だけの偽のストリーム（決まったチャンクを順に返す）。"""

    def __init__(self, chunks, rle, pack, channels, period_s):
        self._chunks = chunks
        self.rle, self.pack, self.channels, self.sample_period_s = rle, pack, channels, period_s
        self.carries_events = False

    def chunks(self, _duration):
        yield from self._chunks

    def stats(self):
        return SimpleNamespace(samples=sum(max(c.count, 0) for c in self._chunks), overrun=False, gaps=(), gap_samples=0,
                               unplaced_beats=0, link_buffer_dict=None)


def run_stream(rec, chunks, rle, pack, channels, period_s, chunk_s, skew, ticks=1000):
    # 機器はケイパビリティだけを読まれる（本物の Capabilities。§B で recording_stream が getattr で読まなくなった）
    s = LaRecordingStream(SimpleNamespace(capabilities=caps_for), rec, rle=rle, chunk_s=chunk_s)
    s.stream = FakeStream(chunks, rle, pack, channels, period_s)
    s.anchor = SimpleNamespace(ticks=ticks, uncertainty=0, method="circuit", t_lo=ticks, t_hi=ticks, basis=None)
    s._skew = skew
    s._run()
    assert s.error is None
    return s.blocks


def wave_rows(directory):
    db = sqlite3.connect(directory / "index.sqlite")
    try:
        return db.execute("SELECT t0, t1, ticks_per_sample, channels, length, first, last, toggles FROM wave ORDER BY t0").fetchall()
    finally:
        db.close()


CASES = [(seed, rle, pack) for seed in range(6) for rle, pack in ((False, 0), (True, 0), (False, 1), (False, 2), (False, 3), (False, 4))]


@pytest.mark.parametrize("segmented", [False, True], ids=["plain", "segmented"])
@pytest.mark.parametrize("seed,rle,pack", CASES)
def test_raw_and_transitions_recordings_answer_the_same(tmp_path, monkeypatch, segmented, seed, rle, pack):
    rng = random.Random(seed * 10 + pack + rle)
    gaps = seed % 2 == 1
    chunks = make_chunks(rng, rle, pack, rng.randint(2, 10), gaps)
    channels = 16 >> pack if not rle else 16
    skew = tuple(rng.choice([0.0, 1.5, None, -0.5]) for _ in range(channels)) if seed % 3 == 0 else ()
    period_s = rng.choice([1 / HZ, 2 / HZ])
    chunk_s = rng.choice([40, 150, 1000]) * period_s                     # かたまりの長さ（サンプル数）をいろいろに

    def record(name):
        directory = tmp_path / name
        rec = (SegmentedRecorder(directory, HZ, policy=Policy(segment_s=3600)) if segmented else Recorder(directory, HZ))
        blocks = run_stream(rec, chunks, rle, pack, channels, period_s, chunk_s, skew)
        rec.close()
        return directory, blocks

    new_dir, new_blocks = record("new")
    assert any(f.read_bytes().startswith(b"FPRAW2 ") for f in new_dir.rglob("*.fpraw"))   # 生の拍で入った
    with monkeypatch.context() as m:
        m.setattr(Recorder, "waveform_format", property(lambda self: 1))
        old_dir, old_blocks = record("old")
    assert not any(b'"format"' in f.read_bytes().split(b"\n", 1)[0] for f in old_dir.rglob("*.bin"))
    assert new_blocks == old_blocks
    assert answers(new_dir, tmp_path / "o_new") == answers(old_dir, tmp_path / "o_old")
    a, b = Recording.open(new_dir), Recording.open(old_dir)
    try:
        assert a.events() == b.events()
        span = a.span()
        skews = lambda r: [r.chunk_skew(c) for c, _kept in r.wave_chunks(span[0], span[1] + 1)]    # noqa: E731
        assert skews(a) == skews(b)                                          # 見出しだけを読む口（correlate が使う）
        if segmented:
            assert wave_rows(new_dir) == wave_rows(old_dir)                # 縮図の事実（first・last・toggles）も同じ
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("python", [False, True], ids=["numpy", "python"])
def test_numpy_free_reader_gives_the_same_transitions(tmp_path, monkeypatch, python):
    rng = random.Random(77)
    chunks = make_chunks(rng, False, 0, 6, True)
    rec = Recorder(tmp_path / "r", HZ)
    run_stream(rec, chunks, False, 0, 16, 1 / HZ, 300 / HZ, ())
    rec.close()
    expected = answers(tmp_path / "r", tmp_path / "o1")
    if python:
        for module in (la, la_wave):
            monkeypatch.setattr(module, "_np", None)
    assert answers(tmp_path / "r", tmp_path / "o2") == expected


def test_a_new_recording_is_format_version_3_and_an_old_one_is_not_changed(tmp_path):
    rec = Recorder(tmp_path / "new", HZ)
    assert rec.waveform_format == 2
    rec.close()
    assert sqlite3.connect(tmp_path / "new" / "recording.sqlite").execute("PRAGMA user_version").fetchone()[0] == FORMAT_VERSION == 3
    # 古い記録（user_version 2）に書き足す: 版は変えず、波形は変化点の形式で書く。生の拍は書けない
    old = tmp_path / "old"
    Recorder(old, HZ).close()
    db = sqlite3.connect(old / "recording.sqlite")
    db.execute("PRAGMA user_version = 2")
    db.close()
    rec = Recorder(old, HZ)
    assert rec.waveform_format == 1
    chunks = make_chunks(random.Random(1), False, 0, 4, False)
    run_stream(rec, chunks, False, 0, 16, 1 / HZ, 200 / HZ, ())
    with pytest.raises(RuntimeError, match="古い形式"):
        rec.add_raw_waveform([b"\0" * 16], rle=False, pack=0, channels=16, period_s=1 / HZ, previous=None, start_ticks=0, length=8)
    rec.close()
    assert sqlite3.connect(old / "recording.sqlite").execute("PRAGMA user_version").fetchone()[0] == 2
    assert not any(b'"format"' in f.read_bytes().split(b"\n", 1)[0] for f in (old / "waveform").iterdir())


def test_a_newer_format_version_is_refused_not_misread(tmp_path):
    Recorder(tmp_path / "r", HZ).close()
    db = sqlite3.connect(tmp_path / "r" / "recording.sqlite")
    db.execute("PRAGMA user_version = 4")
    db.close()
    with pytest.raises(RecordingVersionError, match="4"):
        Recording.open(tmp_path / "r")


def test_a_recording_may_mix_both_chunk_formats(tmp_path):
    from fprobe.la import Transitions
    rec = Recorder(tmp_path / "m", HZ)
    rec.add_waveform(Transitions(array("H", [0, 1]), array("Q", [0, 5]), 10, 0, 0, 1 / HZ, 1), 0)
    data = array("H", [1] * 8 + [0] * 8).tobytes()                       # 2 拍。直前の値は 1
    rec.add_raw_waveform([data], rle=False, pack=0, channels=1, period_s=1 / HZ, previous=1, start_ticks=100, length=16)
    rec.close()
    r = Recording.open(tmp_path / "m")
    got = [(t0, list(tr.values), list(tr.starts), tr.length) for t0, _, tr in r.waveforms()]
    assert got == [(0, [0, 1], [0, 5], 10), (100, [1, 0], [0, 8], 16)]   # 頭は続いている値（previous = 1）で始まる
    r.close()


# --- 古い読み手が新しい記録を黙って読み違えない ---------------------------------------------------------------

@pytest.mark.parametrize("skewed", [False, True])
def test_an_old_reader_stops_on_a_raw_chunk_instead_of_misreading_it(tmp_path, skewed):
    import legacy_reader as old
    rng = random.Random(5)
    skew = (0.0, 1.5) + (None,) * 14 if skewed else ()
    rec = Recorder(tmp_path / "r", HZ)
    run_stream(rec, make_chunks(rng, False, 0, 5, False), False, 0, 16, 1 / HZ, 200 / HZ, skew)
    rec.close()
    files = sorted((tmp_path / "r" / "waveform").iterdir())
    assert files and all(f.suffix == ".fpraw" for f in files)           # 古い形式の名前（.bin）ではない
    for f in files:
        for read in (old.old_transitions, old.old_transitions_np, old.old_chunk_skew):   # 見出しだけを読む口も含めて
            with pytest.raises(ValueError):                              # json.JSONDecodeError: 見出しが JSON として読めない
                read(f)


def test_the_vendored_old_reader_is_faithful_on_a_legacy_recording(tmp_path):
    # 写した古い読み手が、古い記録（固定試料）を新しい読み手と同じに読める = 写しが正しい
    import shutil
    from pathlib import Path
    import legacy_reader as old
    shutil.copytree(Path(__file__).parent / "data" / "recording_legacy" / "plain", tmp_path / "p")
    r = Recording.open(tmp_path / "p")
    try:
        for c in r._chunks():
            f = tmp_path / "p" / "waveform" / c.path
            a, b = old.old_transitions(f), r._transitions(c)
            assert (list(a.values), list(a.starts), a.length, a.channel_skew) == (list(b.values), list(b.starts), b.length, b.channel_skew)
            assert old.old_chunk_skew(f) == r.chunk_skew(c)
    finally:
        r.close()
