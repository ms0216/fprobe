"""時刻の事実（fprobe.timing）の段階 S1b: ロジアナのトリガ時刻とクロストリガの間・多段トリガ・1 周期に複数のサンプルの起点。

仕様: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §11。値の意味（段数）は回路の申告（TLV TIMING。正は
gateware/fprobe_gw/timing.py）で、回路との一致は gateware の cocotb（la_capture_tb・la_deep_buffer_tb・la_cap_domain_tb・
la_lane_e2e_tb・trig_seq_tb・la_lane_inputs_tb）が確かめる。ここでは式だけを確かめる。
"""

import pytest

from fprobe import generated as g
from fprobe.caps import TimingPoint
from fprobe.recording_stream import anchor_stream
from fprobe.timing import (ext_trigger_window, la_trigger_facts, la_xtrig_source, seq_fire_window, stream_start_lanes)
from caps_fakes import FakeDeviceBase, caps_for, with_caps

HZ = 100_000_000


def tp(name, unit, delay, span, point="pin_edge"):
    return TimingPoint(name, point, unit, delay, span, ("input_routing",))


SYS = caps_for(la_sample_hz=HZ, la_delay_internal=6, la_delay_deep=7, la_phase_span=1,
               timing={p.source: p for p in (tp("la", "tick", 6, 1), tp("la_xtrig", "tick", 7, 1),
                                             tp("la_deep", "tick", 7, 1), tp("la_deep_xtrig", "tick", 7, 1),
                                             tp("la_deep_packed", "tick", 8, 1),
                                             tp("la_ext", "tick", 1, 0, "source_pulse"))})


def test_the_internal_trigger_is_the_pin_time_and_the_source_follows():
    f = la_trigger_facts(SYS, "internal", 1000)
    assert (f["pin_time"], f["pin_time_hi"], f["xtrig_source_time"]) == (994, 995, 1001)
    assert la_xtrig_source(SYS, False, f["pin_time"]) == 1001


def test_the_deep_trigger_is_its_own_source_and_packing_widens_the_pin_time():
    f = la_trigger_facts(SYS, "deep", 1000)
    assert (f["pin_time"], f["xtrig_source_time"]) == (993, 1000)
    # 詰め 1 語 4 サンプル（pack 2）、rate 1: トリガのサンプルは語の最後なら 1000 − 8、最初なら さらに 3 × 2 周期前
    packed = la_trigger_facts(SYS, "deep", 1000, pack=2, rate=1)
    assert (packed["pin_time"], packed["pin_time_hi"], packed["xtrig_source_time"]) == (1000 - 8 - 6, 1000 - 8 + 1, 1000)
    old = with_caps(SYS, timing={})
    unknown = la_trigger_facts(old, "deep", 1000, pack=2)
    assert unknown["pin_time"] is None and "pack_delay" in unknown["unknown"]


def test_the_capture_domain_trigger_uses_the_fine_cycle():
    cap = caps_for(la_sample_hz=3 * HZ, la_fine=None,
                   timing={"la_cap_xtrig": tp("la_cap_xtrig", "cap_cycle", 10, 3)})
    f = la_trigger_facts(cap, "internal", 1000, phase=2, latency=2)
    n = 3000
    assert (f["fine_sample"], f["pin_time"], f["pin_time_hi"]) == (n, 1000, 1001)
    assert f["xtrig_source_time"] == (n + 10) // 3 == la_xtrig_source(cap, False, 1000, n)
    with pytest.raises(ValueError):
        la_trigger_facts(cap, "internal", 1000)                        # latency が要る


def test_the_lane_trigger_source_is_one_cycle_of_n_fine_samples():
    lanes = caps_for(la_sample_hz=8 * HZ, la_fine=0xFF,
                     timing={"la_lanes_xtrig": tp("la_lanes_xtrig", "fine_sample", 45, 8)})
    # n = 1000 × 8 + 3 − 37、源 S: n は S × 8 − 45 以上 + 8 未満
    f = la_trigger_facts(lanes, "internal", 1000, phase=3, latency=37)
    n = f["fine_sample"]
    s = f["xtrig_source_time"]
    assert s * 8 - 45 <= n < s * 8 - 45 + 8


def test_an_external_trigger_window_comes_from_the_declaration():
    f = la_trigger_facts(SYS, "internal", 1000)
    assert ext_trigger_window(SYS, f) == {"t_lo": 993, "t_hi": 993, "from": "TIMING", "unknown": []}
    assert ext_trigger_window(SYS, f, rate=3)["t_lo"] == 990                      # 次のサンプルまでの待ち 0〜rate
    old = with_caps(SYS, timing={})
    assert ext_trigger_window(old, f)["unknown"] == ["ext_trigger_alignment"]   # 揃えていない古い回路
    cap = caps_for(la_sample_hz=3 * HZ, timing={"la_ext": tp("la_ext", "tick", 1, 0)})
    w = ext_trigger_window(cap, {"pin_time": 1000, "fine_sample": 3000}, rate=2)
    assert (w["t_lo"], w["t_hi"]) == (1000 - 1, 1000 - 1)                        # (S + 1) × 3 は 2998〜3000


def test_seq_fire_windows_per_cause():
    c = caps_for(la_sample_hz=8 * HZ, la_fine=0xFF,
                 timing={"seq_pin": tp("seq_pin", "tick", 4, 1), "seq_event": tp("seq_event", "tick", 2, 0, "source_pulse"),
                         "seq_input_lanes": tp("seq_input_lanes", "fine_sample", 45, 8)})
    assert (seq_fire_window(c, 100, "pin")["t_lo"], seq_fire_window(c, 100, "pin")["t_hi"]) == (96, 97)
    assert (seq_fire_window(c, 100, "event")["t_lo"], seq_fire_window(c, 100, "event")["t_hi"]) == (98, 98)
    w = seq_fire_window(c, 100, "input_lanes")
    assert (w["fine_lo"], w["fine_hi"]) == (800 - 45, 800 - 45 + 8)
    assert seq_fire_window(c, 100, "internal")["unknown"] == ["seq_timing"]       # 申告が無い
    with pytest.raises(ValueError):
        seq_fire_window(c, 100, "other")


def test_the_lane_stream_start_is_the_fine_edge():
    w = stream_start_lanes(1000, 8, 13)
    assert (w["fine_sample"], w["t_lo"], w["t_hi"]) == (8000 - 13, 998, 999)
    assert stream_start_lanes(1000, 8, 16)["t_lo"] == stream_start_lanes(1000, 8, 16)["t_hi"] == 998


class LaneStartDevice(FakeDeviceBase):
    """1 周期に複数のサンプルの深いバッファ: 起点 start_time を記録する回路。"""

    def __init__(self):
        self.t = 5000
        self.regs = {g.LA_DEEP_INFO: 1 << g.LA_DEEP_INFO_START_VALID[0], g.LA_DEEP_START_TIME_LO: 5003,
                     g.LA_DEEP_START_TIME_LO + 4: 0}

    def capabilities(self):
        return caps_for(la_sample_hz=8 * HZ, la_fine=0xFF,
                        timing={"la_deep_lanes": tp("la_deep_lanes", "fine_sample", 13, 1)})

    def time(self):
        self.t += 10
        return self.t

    def read32(self, address):
        return self.regs[address]

    def read(self, address, count=1, *, timeout_retries=0):
        return [self.regs[address + 4 * k] for k in range(count)]


def test_the_lane_stream_anchor_is_the_circuits_start_time():
    _, anchor = anchor_stream(LaneStartDevice(), lambda: object())
    n0 = 5003 * 8 - 13
    assert anchor.method == "circuit" and anchor.basis["fine_sample"] == n0
    assert (anchor.ticks, anchor.t_lo, anchor.t_hi) == (n0 // 8, n0 // 8, -(-n0 // 8))
    assert anchor.uncertainty == pytest.approx((n0 % 8) / 8)
