"""fprobe.la の前段の追加の条件（U21）: グリッチフィルタとパルス幅のトリガのレジスタ。機器は使わない。"""

from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.la import Front, Pulse

CLOCK = 100_000_000


def caps(**features):
    return SimpleNamespace(sys_clock_hz=CLOCK, la_channels=16, **{"la_filter": True, "la_pulse": True, **features})


def fields(words):
    flt, pulse, low, high = words
    get = lambda w, f: w >> f[0] & ((1 << f[1]) - 1)
    return {"channels": get(flt, g.LA_FILTER_CHANNELS), "cycles": get(flt, g.LA_FILTER_CYCLES),
            "mode": get(pulse, g.LA_PULSE_MODE), "level": get(pulse, g.LA_PULSE_LEVEL),
            "channel": get(pulse, g.LA_PULSE_CHANNEL), "min": low, "max": high}


def test_nothing_requested_turns_both_off():
    front = Front.of(caps())
    assert fields(front.words) == {"channels": 0, "cycles": 0, "mode": 0, "level": 1, "channel": 0, "min": 0,
                                   "max": 0xFFFFFFFF}
    assert front.info == {}


def test_filter_in_nanoseconds_and_its_delay():
    front = Front.of(caps(), filter_channels=0b101, filter_ns=50)
    f = fields(front.words)
    assert (f["channels"], f["cycles"]) == (0b101, 5)
    assert front.info["filter"] == {"channels": [0, 2], "min_width_ns": 50.0, "delay_ns": 40.0}
    assert fields(Front.of(caps(), filter_channels=1, filter_ns=1).words)["cycles"] == 2   # 最短でも 2 周期
    with pytest.raises(ValueError, match="ns まで"):
        Front.of(caps(), filter_channels=1, filter_ns=3000)
    with pytest.raises(ValueError, match="ch 0"):
        Front.of(caps(), filter_channels=1 << 8, filter_ns=50)


def test_pulse_width_range_longer_shorter_and_timeout():
    f = fields(Front.of(caps(), pulse={"channel": 3, "min_ns": 200, "max_ns": 400}).words)
    assert (f["mode"], f["level"], f["channel"], f["min"], f["max"]) == (1, 1, 3, 20, 40)
    f = fields(Front.of(caps(), pulse=Pulse(channel=1, level=0, min_ns=1000)).words)
    assert (f["mode"], f["level"], f["min"], f["max"]) == (1, 0, 100, 0xFFFFFFFF)       # より長い
    f = fields(Front.of(caps(), pulse=Pulse(channel=1, max_ns=100)).words)
    assert (f["min"], f["max"]) == (0, 10)                                               # より短い
    front = Front.of(caps(), pulse=Pulse(channel=2, min_ns=1e6, timeout=True))
    assert fields(front.words)["mode"] == 2 and fields(front.words)["min"] == 100_000
    assert front.info["pulse"] == {"channel": 2, "level": 1, "timeout": True, "min_ns": 1e6, "max_ns": None}
    with pytest.raises(ValueError):
        Front.of(caps(), pulse=Pulse(channel=1, min_ns=500, max_ns=100))
    with pytest.raises(ValueError):
        Front.of(caps(), pulse=Pulse(channel=16))
    with pytest.raises(ValueError):
        Front.of(caps(), pulse=Pulse(channel=1, timeout=True))                           # 0 周期の timeout


def test_missing_features():
    old = SimpleNamespace(sys_clock_hz=CLOCK, la_channels=16)          # U21 の前の構成: レジスタが無いので書かない
    assert Front.of(old) is None
    with pytest.raises(RuntimeError, match="la_filter"):
        Front.of(old, filter_channels=1, filter_ns=50)
    with pytest.raises(RuntimeError, match="la_pulse"):
        Front.of(caps(la_pulse=False), pulse={"channel": 0})


def test_analyzer_writes_the_front_after_the_trigger_and_before_starting():
    from fprobe.la import LogicAnalyzer

    class Dev:
        def __init__(self):
            self.writes = []

        def capabilities(self):
            return SimpleNamespace(la_channels=16, la_depth=64, la_sample_hz=CLOCK, mem_bytes=0, sys_clock_hz=CLOCK,
                                   la_filter=True, la_pulse=True)

        def write32(self, address, value):
            self.writes.append((address, [value]))

        def write(self, address, words):
            self.writes.append((address, list(words)))

    dev = Dev()
    la = LogicAnalyzer(dev)
    la.start(pretrigger=8, filter_channels=1, filter_ns=30, pulse={"channel": 0, "min_ns": 100})
    addresses = [a for a, _ in dev.writes]
    assert addresses == [g.LA_CONTROL, g.LA_RATE, g.LA_FILTER, g.LA_CONTROL]
    assert dev.writes[-1] == (g.LA_CONTROL, [1])
    assert set(la.front_info) == {"filter", "pulse"}


def layout_with_inputs(count=6):
    from fprobe.caps import LaChannel
    return (*(LaChannel(i, "pin", i) for i in range(8)), LaChannel(8, "internal", 0), LaChannel(9, "internal", 1),
            *(LaChannel(10 + i, "input", i) for i in range(count)),
            *(LaChannel(10 + i, "none", 0) for i in range(count, 6)))


def test_filter_reaches_the_dedicated_inputs_declared_by_the_device():
    """U22: フィルタは申告（la_layout）の外の信号の ch（ピンと専用の入力）にかけられる。内部の信号・無い ch は不可。"""
    front = Front.of(caps(la_layout=layout_with_inputs()), filter_channels=1 << 10 | 1 << 15 | 1, filter_ns=50)
    assert fields(front.words)["channels"] == 1 << 10 | 1 << 15 | 1
    assert front.info["filter"]["channels"] == [0, 10, 15]
    for bad in (1 << 8, 1 << 9):                                       # 内部の信号
        with pytest.raises(ValueError, match="ch 0"):
            Front.of(caps(la_layout=layout_with_inputs()), filter_channels=bad, filter_ns=50)
    with pytest.raises(ValueError, match="専用の入力"):                 # 無い ch（入力 2 本の構成の ch 12）
        Front.of(caps(la_layout=layout_with_inputs(2)), filter_channels=1 << 12, filter_ns=50)
    with pytest.raises(ValueError):                                    # 申告の無い古いデバイス: ch 10 は無い
        Front.of(caps(), filter_channels=1 << 10, filter_ns=50)


def test_triggers_on_channels_the_device_does_not_have_are_rejected():
    """U22: 専用の入力の無い構成で ch 10〜15 にトリガを掛けると、成り立たない条件を黙って待たずに誤りにする。"""
    from fprobe.la import check_trigger_channels
    with_inputs = caps(la_layout=layout_with_inputs(), design="fprobe_la")
    check_trigger_channels(with_inputs, mask=1 << 15, rise=1 << 10 | 1 << 8)
    without = caps(la_layout=layout_with_inputs(0), design="fprobe")
    check_trigger_channels(without, mask=0x3FF, rise=1 << 9)
    with pytest.raises(ValueError, match=r"rise の ch \[10, 12\].*fprobe_la"):
        check_trigger_channels(without, rise=1 << 10 | 1 << 12)
    check_trigger_channels(caps(), mask=1 << 15)                       # 申告を持たない偽物は調べない


def test_channel_names_follow_the_declared_layout():
    from fprobe.la import DEFAULT_NAMES, channel_names, external_channels
    names = channel_names(caps(la_layout=layout_with_inputs(2)))
    assert names == {**{i: f"ch{i}" for i in range(8)}, 8: "probe_drives_swdio", 9: "dap_busy", 10: "in0", 11: "in1"}
    assert external_channels(caps(la_layout=layout_with_inputs(2))) == [*range(8), 10, 11]
    assert channel_names(caps()) == {**{i: f"ch{i}" for i in range(8)}, **DEFAULT_NAMES}   # 古いデバイス
    assert DEFAULT_NAMES == {8: "probe_drives_swdio", 9: "dap_busy"}
