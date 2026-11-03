"""1 周期に複数のサンプルの構成（fprobe_pynq_fast: 専用の入力を ISERDESE2 で sys の 1 周期に 6 サンプル）のホストの読み方。機器は使わない。

デバイスは内蔵バッファを語（sys の 1 周期 = N サンプル）で数え、窓 la_lanes_mem の ワード j × 語の数 + w に語 w のレーン 2j・2j + 1 を
置く。ホストはケイパビリティ（la_sample_hz / sys = N、LA_FINE）と la_cap.info（比・レーン・位相・遅れ）を見て、時間順のサンプルと
トリガの位置・細かい時刻を返す（仕様 docs/superpowers/specs/2026-11-01-la-resolution-design.md §3.5・§3.6）。"""

import struct

import pytest

from fprobe import generated as g
from fprobe.caps import LaChannel, parse
from fprobe.la import LogicAnalyzer
from caps_fakes import FakeDeviceBase, caps_for

N = 6
WORDS = 16
SYS = 100_000_000
RAW_TIME = 1_000_000
START, TRIGGER, LANE, LATENCY = 5, 9, 4, 27


def sample(w: int, k: int) -> int:
    """語 w のレーン k に置く値（ch 10〜15 に細かいサンプルの番号、ch 0〜7 に語の番号）。"""
    return ((w * N + k) & 0x3F) << 10 | w & 0xFF


# 本物の構成と同じ ch の並び: ピン 8 本・内部 2 本・専用の入力 6 本（ch 10〜15 = LA_FINE の細かい ch）
LAYOUT = (*(LaChannel(i, "pin", i) for i in range(8)), LaChannel(8, "internal", 0), LaChannel(9, "internal", 1),
          *(LaChannel(10 + i, "input", i) for i in range(6)))


class FakeDevice(FakeDeviceBase):
    def __init__(self, *, lanes_field=N):
        self.caps = caps_for("fprobe_pynq_fast", la_channels=16, la_sample_hz=SYS * N, sys_clock_hz=SYS,
                             la_depth=WORDS * N, la_fine=0xFC00, la_testgen_hz=300_000_000, la_testgen=True, la_layout=LAYOUT)
        self.info = (N << g.LA_CAP_INFO_RATIO[0] | LANE << g.LA_CAP_INFO_TRIGGER_PHASE[0]
                     | LATENCY << g.LA_CAP_INFO_LATENCY[0] | lanes_field << g.LA_CAP_INFO_LANES[0])
        self.writes = []

    def read(self, address, count=1, *, timeout_retries=0):
        if address == g.LA_START_INDEX:
            return [START, TRIGGER, RAW_TIME, 0]
        if g.LA_LANES_MEM_BASE <= address < g.LA_LANES_MEM_BASE + g.LA_LANES_MEM_SIZE:
            first = (address - g.LA_LANES_MEM_BASE) // 4
            j, w0 = divmod(first, WORDS)
            assert w0 == 0 and count == WORDS
            return [sample(w, 2 * j) | sample(w, 2 * j + 1) << 16 for w in range(WORDS)]
        raise AssertionError(f"読まない番地 0x{address:X}")

    def read32(self, address):
        assert address == g.LA_CAP_INFO
        return self.info

    def write32(self, address, value):
        self.writes.append((address, [value]))

    def write(self, address, words):
        self.writes.append((address, list(words)))


def test_samples_are_in_time_order_and_the_trigger_is_at_its_lane():
    dev = FakeDevice()
    la = LogicAnalyzer(dev)
    assert (la.lanes, la.depth) == (N, WORDS * N)
    cap = la.read(1 / (SYS * N))
    expected = [sample((START + i // N) % WORDS, i % N) for i in range(WORDS * N)]
    assert list(cap.samples) == expected
    assert cap.trigger == (TRIGGER - START) * N + LANE
    assert cap.samples[cap.trigger] == sample(TRIGGER, LANE)
    # ピンが変わった細かいサンプルの番号 = タイムベース × N + レーン − 遅れ。trigger_time はその sys の刻み（遅れは二重に引かない）
    assert cap.trigger_cap_cycles == RAW_TIME * N + LANE - LATENCY
    assert cap.trigger_time == (RAW_TIME * N + LANE - LATENCY) // N


def test_channel_resolution_is_declared_per_channel():
    cap = LogicAnalyzer(FakeDevice()).read(1 / (SYS * N))
    assert cap.channel_period_s(10) == pytest.approx(1 / (SYS * N))      # 専用の入力: 1.67 ns
    assert cap.channel_period_s(0) == pytest.approx(1 / SYS)             # 拡張ヘッダのピン: sys（10 ns）
    assert cap.channel_period_s(9) == pytest.approx(1 / SYS)             # 内部の信号


def test_a_device_whose_info_disagrees_is_not_read():
    with pytest.raises(RuntimeError, match="比が合わない"):
        LogicAnalyzer(FakeDevice(lanes_field=1)).read(1 / (SYS * N))


def test_pretrigger_is_written_in_words_and_rate_is_refused():
    dev = FakeDevice()
    la = LogicAnalyzer(dev)
    la.start(pretrigger=13, rise=1 << 10)
    rate = [w for a, w in dev.writes if a == g.LA_RATE][0]
    assert rate[:2] == [0, 3]                                            # 13 サンプル以上 = 3 語（18 サンプル）
    assert la._pretrigger == 18
    with pytest.raises(ValueError, match="間引かない"):
        la.start(rate_hz=1e6)
    with pytest.raises(ValueError, match="pretrigger"):
        la.start(pretrigger=WORDS * N - 1)                               # 16 語目は残せない（トリガの語が要る）


def test_testgen_clock_is_the_declared_one():
    assert LogicAnalyzer(FakeDevice()).testgen_clock_hz == 300_000_000   # 方形波は cap_clk（細かいサンプル 2 個）


def _tlv(kind, value):
    return struct.pack("<HH", kind, len(value)) + value + bytes(-len(value) % 4)


def test_capabilities_parse_the_new_declarations():
    body = (_tlv(g.CAPS_LA_TESTGEN_HZ, struct.pack("<I", 300_000_000)) + _tlv(g.CAPS_LA_FINE, struct.pack("<I", 0xFC00))
            + _tlv(g.CAPS_END, b""))
    data = struct.pack("<II", g.CAPS_MAGIC, 8 + len(body)) + body
    caps = parse(data)
    assert (caps.la_testgen_hz, caps.la_fine) == (300_000_000, 0xFC00)
    old = parse(struct.pack("<II", g.CAPS_MAGIC, 12) + _tlv(g.CAPS_END, b""))
    assert (old.la_testgen_hz, old.la_fine) == (0, None)                 # 申告の無い古いデバイス
