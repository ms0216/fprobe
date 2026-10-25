"""ロジアナのホスト側（la / la_wave / la_vcd / probe_la）の不具合の再現（検出専用。strict の xfail: 直ると XPASS で失敗して気づく）。
台帳: docs/reports/bugs/la-host.md

手法: 試験の中に素朴な別実装（RLE の復号・変化点の表・波形の縮め・VCD の読み戻し・深いバッファのリング）を書き、
乱数の入力（ランダムな遷移列、RLE の繰り返し長 1〜65536 と境界、pretrigger / post_beats の境界、チャンネル数 1〜16、
サンプル周波数の分周 1〜7、画面幅 1〜10000 画素、ストリームの任意のバイト位置での分割）を製品に当てて突き合わせた。
numpy の有る経路と無い経路（la_wave._np = None）の両方を確かめた。以下の「通る試験」は不具合なしの確認で、
xfail は見つけた不具合の再現。
"""

import random
import re
import threading
from array import array
from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe import la_wave
from fprobe.caps import Capabilities
from fprobe.la import (Capture, DeepAnalyzer, LaStream, LogicAnalyzer, SegmentSplitter, StreamChunk, Transitions,
                       VcdWriter, Waveform, decode_beats, decode_transitions, trigger_sample, write_vcd)

SEEDS = range(12)


@pytest.fixture(params=["numpy", "python"])
def numpy_mode(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(la_wave, "_np", None)
        monkeypatch.setattr("fprobe.la._np", None)
    return request.param


# --- 素朴な基準実装 -------------------------------------------------------------------------------------

def ref_records(data):
    """RLE の拍 → [(値, 繰り返し数)]（レコード = 32 ビット、下位 16 = 値、上位 16 = 繰り返し − 1）。"""
    return [(int.from_bytes(data[i:i + 2], "little"), int.from_bytes(data[i + 2:i + 4], "little") + 1)
            for i in range(0, len(data), 4)]


def ref_expand(data):
    out = []
    for value, count in ref_records(data):
        out += [value] * count
    return out


def ref_runs(samples):
    values, starts = [], []
    for i, v in enumerate(samples):
        if not values or values[-1] != v:
            values.append(v)
            starts.append(i)
    return values, starts


def pack_rle(records):
    """[(値, 個数 1〜65536)] → 拍（4 レコード単位。足りない分は個数 1 のレコードで埋める）。"""
    records = list(records)
    while len(records) % 4:
        records.append((records[-1][0] ^ 1, 1))
    return b"".join(((n - 1) << 16 | v).to_bytes(4, "little") for v, n in records)


def random_records(rng, n):
    out = []
    for _ in range(n):
        n_rep = rng.choice([1, 2, 3, 255, 256, 65535, 65536, rng.randint(1, 65536)] if rng.random() < 0.3
                           else [1, 1, 2, 5, 17])
        out.append((rng.choice([0, 1, 0xFFFF, rng.randrange(1 << 16)]), n_rep))
    return out


def random_samples(rng, n, channels):
    mask, out, v = (1 << channels) - 1, [], rng.randrange(1 << channels)
    while len(out) < n:
        out += [v] * rng.choice([1, 1, 2, 3, 10, 100])
        v = v ^ (1 << rng.randrange(channels)) if rng.random() < 0.6 else rng.randrange(mask + 1)
    return out[:n]


def make_cap(samples, channels, trigger=0, period=1e-8, trigger_time=0):
    return Capture(samples, trigger, trigger_time, period, channels)


# --- (a) 復号・変化点・波形の往復 -----------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_rle_decode_matches_reference(seed, numpy_mode):
    rng = random.Random(seed)
    data = pack_rle(random_records(rng, rng.randint(1, 60)))
    expected = ref_expand(data)
    assert list(decode_beats(data, True)) == expected
    values, starts, length = decode_transitions(data, True)
    ev, es = ref_runs(expected)
    assert (list(values), list(starts), length) == (ev, es, len(expected))


@pytest.mark.parametrize("seed", SEEDS)
def test_raw_decode_matches_reference(seed, numpy_mode):
    rng = random.Random(seed)
    samples = random_samples(rng, 8 * rng.randint(1, 40), 16)
    data = array("H", samples).tobytes()
    assert list(decode_beats(data, False)) == samples
    values, starts, length = decode_transitions(data, False)
    assert (list(values), list(starts), length) == (*ref_runs(samples), len(samples))


def test_empty_data_decodes_to_nothing(numpy_mode):
    for rle in (False, True):
        assert len(decode_beats(b"", rle)) == 0
        values, starts, length = decode_transitions(b"", rle)
        assert (len(values), len(starts), length) == (0, 0, 0)


def test_decode_rejects_partial_beat():
    with pytest.raises(ValueError):
        decode_beats(bytes(15), False)


@pytest.mark.parametrize("seed", SEEDS)
def test_trigger_sample_matches_reference(seed):
    rng = random.Random(seed)
    records = random_records(rng, rng.randint(4, 40))
    data = pack_rle(records)
    for beat in range(len(data) // 16):
        for slot in range(4):
            assert trigger_sample(data, True, beat, slot) == sum(n for _, n in records_padded(data)[:beat * 4 + slot])
    raw = array("H", random_samples(rng, 8 * 9, 16)).tobytes()
    assert all(trigger_sample(raw, False, b, s) == b * 8 + s for b in range(9) for s in range(8))


def records_padded(data):
    return ref_records(data)


@pytest.mark.parametrize("seed", SEEDS)
def test_capture_transitions_waveform_roundtrip(seed, numpy_mode):
    rng = random.Random(seed)
    channels = rng.randint(1, 16)
    samples = random_samples(rng, rng.randint(1, 600), channels)
    cap = make_cap(samples, channels, trigger=rng.randrange(len(samples)))
    tr = Transitions.from_capture(cap)
    assert (list(tr.values), list(tr.starts)) == tuple(ref_runs(samples)) and tr.length == len(samples)
    wave = Waveform(tr)
    for ch in range(channels):
        bits = [s >> ch & 1 for s in samples]
        assert cap.channel(ch) == bits
        assert [tr.level_at(ch, i) for i in range(len(samples))] == bits
        assert [wave.level_at(ch, i) for i in range(len(samples))] == bits
        assert tr.edges(ch) == [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]
        assert list(wave.edges_between(ch, -1, len(samples))) == tr.edges(ch)
        lo, hi = sorted((rng.randrange(len(samples) + 1), rng.randrange(len(samples) + 1)))
        want = [e for e in tr.edges(ch) if lo < e < hi]
        assert list(wave.edges_between(ch, lo, hi)) == want and wave.count_between(ch, lo, hi) == len(want)


# --- VCD の読み戻し（別実装の読み手） -------------------------------------------------------------------

def parse_vcd(path):
    """VCD → (名前→識別子, {識別子: [(時刻 ns, 値)]})。"""
    names, series, now = {}, {}, None
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith("$var"):
            _, _, _, ident, name, _ = line.split()
            names[name] = ident
            series[ident] = []
        elif line.startswith("#"):
            now = int(line[1:])
        elif line and line[0] in "01" and now is not None:
            series[line[1:]].append((now, int(line[0])))
    return names, series


def vcd_value(series, ns):
    value = None
    for t, v in series:
        if t <= ns:
            value = v
    return value


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("as_transitions", [False, True])
def test_vcd_roundtrip_values_and_trigger_marker(seed, as_transitions, tmp_path, numpy_mode):
    rng = random.Random(seed)
    channels, divider = rng.randint(1, 16), rng.randint(1, 7)
    samples = random_samples(rng, rng.randint(2, 300), channels)
    trig = rng.randrange(len(samples))
    cap = make_cap(samples, channels, trigger=trig, period=divider / 100e6)
    write_vcd(Transitions.from_capture(cap) if as_transitions else cap, tmp_path / "w.vcd")
    names, series = parse_vcd(tmp_path / "w.vcd")
    for ch in range(channels):
        ident = [i for n, i in names.items() if n.startswith("ch") and n == f"ch{ch}" or n in
                 ("probe_drives_swdio", "dap_busy") and ch == {"probe_drives_swdio": 8, "dap_busy": 9}[n]][0]
        for i, s in enumerate(samples):
            assert vcd_value(series[ident], i * divider * 10) == s >> ch & 1, (ch, i)
    marks = series[names["trigger"]]
    assert vcd_value(marks, trig * divider * 10) == 1                                  # トリガのサンプルで 1
    assert all(v == 0 for t, v in marks if t != trig * divider * 10)                   # ほかでは 0


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("rle", [False, True])
def test_stream_chunks_join_to_whole(seed, rle, tmp_path, numpy_mode):
    """任意のバイト位置で分けた受け取り（LaStream._take）が、全体を一度に復号した結果・write_vcd と一致する。"""
    rng = random.Random(seed)
    if rle:
        data = pack_rle(random_records(rng, rng.randint(4, 80)))
        expected = ref_expand(data)
    else:
        expected = random_samples(rng, 8 * rng.randint(1, 60), 16)
        data = array("H", expected).tobytes()
    stream = LaStream(None, rle, 1e-8, 16)
    cuts = sorted(rng.sample(range(1, len(data)), min(len(data) - 1, rng.randint(0, 8)))) if len(data) > 1 else []
    pieces = [data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)])]
    chunks = [c for c in (stream._take(p) for p in pieces) if c is not None]
    assert stream._pending == b""
    joined = []
    for c in chunks:
        assert c.first == len(joined) and c.length == len(c.samples())
        joined += list(c.samples())
    assert joined == expected
    values, starts, previous = [], [], None
    for c in chunks:
        v, s = c.transitions()
        values += list(v)
        starts += list(s)
    assert (values, starts) == ref_runs(expected)
    with VcdWriter(tmp_path / "s.vcd", 16, 1e-8) as vcd:
        for c in chunks:
            vcd.write(*c.transitions())
    write_vcd(Transitions(array("H", values), array("Q", starts), len(expected), 0, 0, 1e-8, 16), tmp_path / "w.vcd")
    sn, ss = parse_vcd(tmp_path / "s.vcd")
    wn, ws = parse_vcd(tmp_path / "w.vcd")
    for ch in range(16):
        assert ss[sn[wn_name(ch, sn)]] == ws[wn[wn_name(ch, wn)]], ch


def wn_name(ch, names):
    return next(n for n in names if n == f"ch{ch}" or (ch, n) in ((8, "probe_drives_swdio"), (9, "dap_busy")))


@pytest.mark.parametrize("seed", SEEDS)
def test_stream_with_headers_splits_at_any_byte(seed):
    """見出し付きのストリーム（R1）を任意のバイト位置で分けても、ロジアナの拍がつながる。"""
    rng = random.Random(seed)
    rle_beats, wire, seq = [], b"", 0
    for _ in range(rng.randint(1, 6)):
        n = rng.randint(1, 5)
        body = bytes(rng.randrange(256) for _ in range(16 * n))
        head = g.STREAM_SEG_LA | n << 16 | seq << 32 | g.STREAM_SEG_MAGIC << 64
        wire += head.to_bytes(16, "little") + body
        rle_beats.append(body)
        seq += n
    stream = LaStream(None, False, 1e-8, 16, events=(0, 100))
    cuts = sorted(rng.sample(range(1, len(wire)), min(len(wire) - 1, 7)))
    got = b"".join(c.data for c in (stream._take(wire[a:b]) for a, b in zip([0, *cuts], [*cuts, len(wire)])) if c)
    assert got == b"".join(rle_beats)


# --- 深いバッファ（リング）の読み出しの別実装 ------------------------------------------------------------

class RingDevice:
    """深いバッファの機器の模型: 拍 k をリングの k % R に置き、トリガの拍 tb・スロット slot、トリガ後 post 拍を埋めて止まった状態。"""

    def __init__(self, samples, ring, tb, slot, post, rle_data=None, time=1_000_000, **caps):
        self.caps = Capabilities(la_channels=16, la_sample_hz=100_000_000, sys_clock_hz=100_000_000, mem_bytes=1 << 20,
                                 **caps)
        self.ring, self.tb, self.slot, self.post, self.time = ring, tb, slot, post, time
        self.data = rle_data if rle_data is not None else array("H", samples).tobytes()
        self.regs = {g.LA_DEEP_RING_BEATS: ring, g.MEM_STATUS: 1 << g.MEM_STATUS_READY[0],
                     g.LA_DEEP_STATUS: slot << g.LA_DEEP_STATUS_TRIGGER_SLOT[0] | 1 << g.LA_DEEP_STATUS_DONE[0]}
        self.pending = b""

    def capabilities(self):
        return self.caps

    def read32(self, a):
        if a == g.LA_DEEP_POST_BEATS and a not in self.regs:
            return self.post
        return self.regs.get(a, 0)

    def read(self, a, count=1):
        if a == g.LA_DEEP_TRIGGER_BEAT:
            return [self.tb % self.ring, min(self.tb + 1 + self.post, self.ring), self.time & 0xFFFFFFFF, self.time >> 32]
        return [0] * count

    def write32(self, a, v):
        self.regs[a] = v

    def write(self, a, words):
        for i, w in enumerate(words):
            self.regs[a + 4 * i] = w
        if a == g.LA_DEEP_READ_FIRST:
            self.first, self.count = words

    def read_stream(self, n, timeout_ms=0):
        beats = [(self.first + j) % self.ring for j in range(self.count)]
        return b"".join(self.data[b * 16:(b + 1) * 16] for b in beats)[:n]


@pytest.mark.parametrize("seed", SEEDS)
def test_deep_raw_window_and_trigger_position(seed):
    """LogicAnalyzer（la_depth = 0 の深いバッファ）: pretrigger・窓の境界で、トリガのサンプルが pretrigger の位置に来る。"""
    rng = random.Random(seed)
    depth = LogicAnalyzer.WINDOW
    pre = rng.choice([0, 1, 7, 8, 9, 100, 4096, depth - 1, rng.randrange(depth)])
    ring = 2048
    post = -(-(depth - pre) // 8)
    tb = rng.randrange(-(-pre // 8) + 1, ring - post - 1)
    slot = rng.randrange(8)
    samples = random_samples(rng, ring * 8, 16)
    dev = RingDevice(samples, ring, tb, slot, post, la_depth=0)
    la = LogicAnalyzer(dev)
    period = la.start(pretrigger=pre)
    cap = la.read(period)
    t = tb * 8 + slot
    assert cap.trigger == pre
    assert list(cap.samples) == samples[t - pre:t - pre + depth]
    assert cap.trigger_time == dev.time                               # 遅れの申告が無ければ直さない


@pytest.mark.parametrize("seed", SEEDS)
def test_deep_read_before_after_beats(seed):
    rng = random.Random(seed)
    ring, rle = 512, bool(seed % 2)
    if rle:
        records = random_records(rng, ring * 4)
        data = pack_rle(records)
        expected = ref_expand(data)
        bounds = [0]
        for _, n in ref_records(data):
            bounds.append(bounds[-1] + n)
    else:
        expected = random_samples(rng, ring * 8, 16)
        data = array("H", expected).tobytes()
    post = rng.randint(0, 40)
    tb = rng.randint(post + 1, ring - post - 1)
    slot = rng.randrange(4 if rle else 8)
    dev = RingDevice(None, ring, tb, slot, post, rle_data=data)
    deep = DeepAnalyzer(dev)
    deep._rle = rle
    before, after = rng.choice([0, 1, 5, 100, tb, None]), rng.choice([0, 1, 3, post, None])
    cap = deep.read(before, after)
    nb = tb if before is None else min(before, tb)
    na = post if after is None else min(after, post)
    if rle:
        first, last = bounds[(tb - nb) * 4], bounds[(tb + 1 + na) * 4]
        trig = bounds[tb * 4 + slot] - first
    else:
        first, last = (tb - nb) * 8, (tb + 1 + na) * 8
        trig = tb * 8 + slot - first
    assert list(cap.samples) == expected[first:last] and cap.trigger == trig
    tr = deep.read_transitions(before, after)
    assert (list(tr.values), list(tr.starts), tr.length, tr.trigger) == (*ref_runs(expected[first:last]), last - first, trig)


# --- (c) 時刻の換算 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("divider", range(1, 8))
def test_add_capture_places_the_trigger_sample_at_the_trigger_time(divider):
    from fprobe.collector import add_capture
    placed = []
    rec = SimpleNamespace(timebase_hz=100_000_000, add_waveform=lambda tr, start_ticks: placed.append(start_ticks))
    tr = Transitions(array("H", [0]), array("Q", [0]), 10_000_000, 5_000_000, 123_456_789, divider / 1e8, 16)
    add_capture(rec, tr)
    assert placed[0] + tr.trigger * divider == tr.trigger_time                 # 1 サンプルのずれがない


# --- (e) 画面幅への縮め -------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_view_never_loses_a_change(seed, numpy_mode):
    """縮めた波形: 画素の先頭の値が正しく、区間内に変化があれば 'x'。どの変化点も 'x' の画素に入る（消えない）。"""
    rng = random.Random(seed)
    channels = rng.randint(1, 16)
    samples = random_samples(rng, rng.choice([1, 2, 50, 700, 5000]), channels)
    wave = Waveform(Transitions.from_capture(make_cap(samples, channels)))
    for _ in range(25):
        width = rng.choice([1, 2, 3, 7, 100, 1000, 10000, rng.randint(1, 10000)])
        a = rng.randrange(len(samples))
        b = rng.randint(a + 1, len(samples))
        rows = wave.view(a, b, width)
        assert len(rows) == channels and all(len(r) == width for r in rows)
        span = b - a
        for ch in range(channels):
            bits = [s >> ch & 1 for s in samples]
            flagged, starts = set(), set()
            for i, c in enumerate(rows[ch]):
                s = a + i * span // width
                e = max(s + 1, a - (-(i + 1) * span // width))
                varies = len(set(bits[s:e])) > 1
                assert c == ("x" if varies else str(bits[s])), (ch, i, s, e)
                starts.add(s)
                if c == "x":
                    flagged.update(range(s + 1, e))
            for t in range(a + 1, b):
                if bits[t] != bits[t - 1]:
                    assert t in flagged or t in starts, ("消えた変化", ch, t)    # 画素の先頭の変化は隣の画素と値が違う


def test_view_one_sample_pulse_is_visible_at_every_width(numpy_mode):
    n = 3000
    for p in (0, 1, 1499, 2999):
        samples = [0] * n
        samples[p] = 1
        wave = Waveform(Transitions.from_capture(make_cap(samples, 1)))
        for width in (1, 2, 5, 31, 97, 1000, 2999, 3000, 10000):
            row = wave.view(0, n, width)[0]
            assert set(row) - {"0"}, (p, width)                       # 脈が消えて全部 '0' にならない


def test_view_clips_and_rejects_empty():
    wave = Waveform(Transitions.from_capture(make_cap([0, 1, 0, 1], 1)))
    assert wave.view(-10, 100, 4) == wave.view(0, 4, 4)
    with pytest.raises(ValueError):
        wave.view(4, 9, 4)


# --- 見つけた不具合 -------------------------------------------------------------------------------------

class _Collector:
    def __init__(self):
        self.paused = False

    def pause_events(self, flag):
        self.paused = flag


def _tools(dev, collector):
    from fprobe.probe_la import LogicAnalyzerTools

    class T(LogicAnalyzerTools):
        pass
    t = T()
    t._init_la()
    t._lock = threading.RLock()
    t.session = SimpleNamespace(device=dev)
    t._collector = collector
    t._la = None
    t._la_idle = lambda: None
    return t


def test_failed_deep_arm_does_not_leave_event_recording_paused():
    """BUG-LA-01（修正済み）: 仕掛けに失敗したら、止めたイベント記録の読みを必ず戻し、半端な DeepAnalyzer を残さない。"""
    dev = RingDevice(None, 512, 10, 0, 4, rle_data=bytes(512 * 16))
    collector = _Collector()
    tools = _tools(dev, collector)
    with pytest.raises(ValueError):
        tools.la_deep_arm(after_beats=4096)                                      # リング（512）を超える post_beats は断られる
    assert not collector.paused
    assert tools._deep is None


@pytest.mark.xfail(strict=True, reason="BUG-LA-02: la_arm の既定の pretrigger 1024 が小さいバッファで ValueError")
def test_la_arm_default_pretrigger_works_on_a_small_buffer():
    class Dev(RingDevice):
        pass
    dev = Dev(None, 512, 10, 0, 4, rle_data=bytes(512 * 16), la_depth=512)
    tools = _tools(dev, None)
    tools.la_arm()                                                               # 既定の pretrigger = 1024 > 深さ 512


@pytest.mark.xfail(strict=True, reason="BUG-LA-03: rate_hz = 0 が ZeroDivisionError、負は黙って最高速")
def test_rate_hz_zero_or_negative_is_rejected_with_a_message():
    dev = RingDevice(None, 512, 10, 0, 4, rle_data=bytes(512 * 16), la_depth=64)
    la = LogicAnalyzer(dev)
    for bad in (0, -1e6):
        with pytest.raises(ValueError):
            la.start(rate_hz=bad)


@pytest.mark.xfail(strict=True, reason="BUG-LA-04: la_deep_collect の要約が切り捨て・省いた ch を言わない")
def test_deep_summary_reports_what_it_omits_like_the_internal_one():
    from fprobe.probe_la import _channel_summary, _transition_summary
    samples = [0, 1] * 40 + [0] * 10
    cap = make_cap(samples, 2)
    a, b = _channel_summary(cap), _transition_summary(Transitions.from_capture(cap))
    assert set(a["channels"]) == set(b) and a["constant_low"] == [1]
    assert "first_edges_truncated" in b[0]                                      # 先頭 16 個で切ったことが分かる
