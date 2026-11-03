"""深いバッファを 1 周期に複数のサンプルで垂れ流す構成（fprobe_pynq_deep_fast、800 MS/s）のホスト側: サンプルの間隔と ch の詰めの
レジスタ、RAW の量の上限で始める前に断る（RLE は断らない）、1 ch の詰めの解読、リングと帯域の使用量、細かいサンプルの時刻の式
（偽のデバイス。ハードウェア不要。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）。"""

import random
from array import array

import pytest

from fprobe import generated as g
from fprobe.la import DeepAnalyzer, lane_divisor, raw_stream_bytes_per_s
from test_la_stream import FakeStreamDevice
from caps_fakes import caps_for

LANES = 8


class FastDev(FakeStreamDevice):
    """1 周期に 8 サンプルの深いバッファ。la_deep の数え（la_written・ring_unread・ring_peak）・info・起点の時刻を返す。"""

    def __init__(self, *a, limit=50_000_000, usage=True, info_lanes=LANES, **kw):
        super().__init__(*a, **kw)
        self.limit, self.has_usage, self.info_lanes = limit, usage, info_lanes
        self.rates: list[list[int]] = []
        self.start_time = 1234

    def capabilities(self):
        return caps_for("fprobe_pynq_deep_fast", mem_bytes=32 << 20, la_channels=16, la_sample_hz=800_000_000,
                        sys_clock_hz=100_000_000, la_fine=0xFCFF, la_pack=True, la_filter=False, la_pulse=False,
                        la_stream_bytes_per_s=self.limit, la_stream_usage=self.has_usage)

    def write(self, address, words):
        if address == g.LA_RATE:
            self.rates.append(list(words))

    def read(self, address, count=1, *, timeout_retries=0):
        if address == g.LA_DEEP_START_TIME_LO and count == 2:
            return [self.start_time, 0]
        return super().read(address, count)

    def read32(self, address):
        if self.has_usage and address in (g.LA_DEEP_LA_WRITTEN, g.LA_DEEP_RING_UNREAD, g.LA_DEEP_RING_PEAK):
            return {g.LA_DEEP_LA_WRITTEN: self.written, g.LA_DEEP_RING_UNREAD: self.written // 2,
                    g.LA_DEEP_RING_PEAK: self.written}[address]
        if address == g.LA_DEEP_INFO:
            return (self.info_lanes << g.LA_DEEP_INFO_LANES[0] | 13 << g.LA_DEEP_INFO_LATENCY[0]
                    | 1 << g.LA_DEEP_INFO_START_VALID[0])
        return super().read32(address)


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)


def start_value(dev):
    return [v for v in dev.control_writes if v & 1][-1]


def test_lane_divisor_takes_only_the_intervals_the_circuit_can_make():
    assert lane_divisor(800_000_000, 8, None) == (1, 0, 0)
    assert lane_divisor(800_000_000, 8, 400e6) == (2, 1, 0)
    assert lane_divisor(800_000_000, 8, 200e6) == (4, 2, 0)
    assert lane_divisor(800_000_000, 8, 100e6) == (8, 3, 0)
    assert lane_divisor(800_000_000, 8, 25e6) == (32, 3, 3)
    for bad in (800e6 / 3, 800e6 / 6, 800e6 / 12, 7e6, 2e9):
        with pytest.raises(ValueError):
            lane_divisor(800_000_000, 8, bad)


@pytest.mark.parametrize("rate, step, la_rate", [(None, 0, 0), (400e6, 1, 0), (100e6, 3, 0), (50e6, 3, 1)])
def test_stream_writes_the_step_and_the_word_rate(rate, step, la_rate):
    dev = FastDev(bytes(16 * 100), rng=random.Random(1), limit=0)          # 量の上限は別の試験で
    s = DeepAnalyzer(dev).stream(rate_hz=rate, pack_channels=1)
    v = start_value(dev)
    assert v >> g.LA_DEEP_CONTROL_STEP[0] & 3 == step and v >> g.LA_DEEP_CONTROL_PACK[0] & 7 == 4
    assert dev.rates[-1][0] == la_rate
    assert s.sample_period_s == pytest.approx((1 if rate is None else 800e6 / rate) / 800e6)


def test_raw_over_the_declared_rate_is_refused_before_starting_and_rle_is_not():
    dev = FastDev(bytes(16 * 100), rng=random.Random(1))
    an = DeepAnalyzer(dev)
    with pytest.raises(ValueError, match="100.0 MB/s"):            # 1 ch × 800 MS/s = 100 MB/s > 50 MB/s
        an.stream(pack_channels=1)
    assert not dev.control_writes or not any(v & 1 for v in dev.control_writes)    # 始めていない
    an.stream(pack_channels=1, rate_hz=400e6)                     # 1 ch × 400 MS/s = 50 MB/s（上限ちょうど）は流せる
    an.stream(pack_channels=2, rate_hz=200e6)
    with pytest.raises(ValueError):
        an.stream(rate_hz=100e6)                                  # 16 ch × 100 MS/s = 200 MB/s
    an.stream(rle=True)                                           # RLE は量が信号で決まるので断らない
    an.stream(pack_channels=1, max_bytes_per_s=120e6)             # 使う側が上限を明示すれば、そちらを使う
    with pytest.raises(ValueError, match="指定の max_bytes_per_s"):
        an.stream(pack_channels=1, rate_hz=400e6, max_bytes_per_s=10e6)
    assert raw_stream_bytes_per_s(1, 800e6) == 100e6 and raw_stream_bytes_per_s(16, 25e6) == 50e6


def test_without_a_declared_rate_raw_is_not_refused():
    dev = FastDev(bytes(16 * 100), rng=random.Random(1), limit=0)
    DeepAnalyzer(dev).stream(pack_channels=1)


def test_one_channel_pack_expands_sixteen_samples_a_word():
    rng = random.Random(3)
    bits = []
    while len(bits) < 16 * 8 * 3000:
        bits += [len(bits) // 7 % 2] * rng.randint(1, 9)
    bits = bits[:16 * 8 * 3000]
    words = array("H", (sum(bits[i + j] << j for j in range(16)) for i in range(0, len(bits), 16)))
    dev = FastDev(words.tobytes(), rng=rng, limit=0)
    stream = DeepAnalyzer(dev).stream(pack_channels=1)
    got = array("H")
    for chunk in stream.chunks(0.1):
        got.extend(chunk.samples())
    assert list(got) == bits[:len(got)] and len(got) > 1000
    assert stream.stats().samples == len(got)


def test_trigger_capture_and_the_wrong_lanes_are_refused():
    dev = FastDev(bytes(16 * 100), rng=random.Random(1))
    with pytest.raises(RuntimeError, match="垂れ流しだけ"):
        DeepAnalyzer(dev).start()
    with pytest.raises(ValueError, match="rate_hz"):
        DeepAnalyzer(dev).stream(rle=True, rate_hz=400e6)
    with pytest.raises(RuntimeError, match="合わない"):
        DeepAnalyzer(FastDev(bytes(16 * 100), rng=random.Random(1), info_lanes=6)).stream(rle=True)


def test_usage_reports_the_ring_and_the_rates_as_facts():
    dev = FastDev(bytes(16 * 4000), rng=random.Random(1), limit=0)
    stream = DeepAnalyzer(dev).stream(pack_channels=1)
    first = stream.usage()
    assert first["ring_beats"] == dev.ring and first["unknown"] == []
    for _ in stream.chunks(0.05):
        pass
    last = stream.usage()
    assert last["encoded_bytes"] == dev.written * 16 and last["ring_unread_beats"] == dev.written // 2
    assert last["ring_peak_beats"] == dev.written and last["received_bytes"] == stream.stats().bytes
    assert last["encoded_bytes_per_s"] is not None and len(stream.usage_history) == 2
    # 数えの無い古い回路: 欄は None、unknown に名前
    old = FastDev(bytes(16 * 100), rng=random.Random(1), limit=0, usage=False)
    u = DeepAnalyzer(old).stream(pack_channels=1).usage()
    assert u["ring_unread_beats"] is None and "ring_unread_beats" in u["unknown"]


def test_sample_time_follows_the_encoder_formula():
    dev = FastDev(bytes(16 * 100), rng=random.Random(1), limit=0)
    s = DeepAnalyzer(dev).stream(pack_channels=1, rate_hz=200e6)          # 4 細かいサンプルごと
    t = s.sample_time(10)
    assert t["edge"] == 1234 * 8 + 10 * 4 - 13 and t["edge_lo"] == t["edge"] - 3 and t["edge_hz"] == 800_000_000


def test_first_channel_selects_the_packed_channels_and_is_checked():
    from test_la_stream_pack import PackDev
    dev = FastDev(bytes(16 * 100), rng=random.Random(1), limit=0)
    s = DeepAnalyzer(dev).stream(pack_channels=1, first_channel=10)          # PMOD JB の 1 番 = ch 10 だけ
    assert start_value(dev) >> g.LA_DEEP_CONTROL_FIRST[0] & 0xF == 10 and s.first_channel == 10
    for kw in ({"first_channel": 10}, {"pack_channels": 8, "first_channel": 9}, {"pack_channels": 2, "first_channel": 15}):
        with pytest.raises(ValueError, match="first_channel"):
            DeepAnalyzer(dev).stream(**kw)
    with pytest.raises(ValueError, match="first_channel"):                  # 1 周期 1 サンプルの構成は 0 だけ
        DeepAnalyzer(PackDev(bytes(16 * 100), rng=random.Random(1))).stream(pack_channels=2, first_channel=2)
