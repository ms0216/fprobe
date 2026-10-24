"""クロストリガの設定の読み戻し（機器の CSR の事実）: 番地一致・割り込み書き込みの番地と値（xtrig.status）、多段トリガの
段とピンの条件（xtrig.sequence_status の config）、ロジアナのトリガの条件と状態（la.trigger_status）。
画面は操作の記録から「今の設定」を推さず、これを描く（docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.3）。"""

from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.mcp_server import Probe
from fprobe.xtrig import CrossTrigger, Event, Pattern, Stage, TriggerSequencer
from probe_fakes import HZ, FakeDevice


class LaDevice(FakeDevice):
    def __init__(self, **caps):
        super().__init__()
        self.caps = dict(la_channels=10, la_depth=8192, la_sample_hz=100_000_000, mem_bytes=0, la_filter=True,
                         la_pulse=True)
        self.caps.update(caps)

    def capabilities(self):
        return SimpleNamespace(**{**vars(super().capabilities()), **self.caps})


def probe_of(device):
    p = Probe("unused")
    p._session = SimpleNamespace(device=device)
    return p


def test_xtrig_status_reads_back_the_match_and_inject_settings():
    dev = FakeDevice()
    xt = CrossTrigger(dev)
    xt.set_match(0x2000_0010, address_mask=0xFFFF_FFFC, data=5, data_mask=0xFF, on_write=False, on_read=True)
    xt.set_inject(0x4000_0000, 0x1234, ap=1)
    out = probe_of(dev).xtrig_status()
    assert out["match"] == {"count": 0, "time": 0, "address": 0x2000_0010, "address_mask": 0xFFFF_FFFC, "data": 5,
                            "data_mask": 0xFF, "on_write": False, "on_read": True}
    assert out["inject"]["ap"] == 1 and out["inject"]["address"] == 0x4000_0000 and out["inject"]["value"] == 0x1234
    assert out["inject"]["count"] == 0 and out["inject"]["result"] == "ok"


def test_sequence_status_reads_back_the_stages_and_patterns():
    dev = FakeDevice()
    seq = TriggerSequencer(dev)
    seq.configure([Stage(Event.PATTERN_A), Stage(Event.PATTERN_B | Event.DAP_MATCH, count=3, within_s=10e-6)],
                  Pattern(mask=1, value=1, rise=8), Pattern(fall=32))
    out = probe_of(dev).xtrig_sequence_status()
    assert out["config"] == {
        "stages": [{"when": ["pattern_a"], "count": 1, "within_us": None},
                   {"when": ["pattern_b", "dap_match"], "count": 3, "within_us": pytest.approx(10.0)}],
        "pattern_a": {"mask": 1, "value": 1, "rise": 8, "fall": 0},
        "pattern_b": {"mask": 0, "value": 0, "rise": 0, "fall": 32},
    }
    assert out["stages"] == 2


def test_sequence_status_without_stages_has_empty_config():
    out = probe_of(FakeDevice()).xtrig_sequence_status()
    assert out["config"]["stages"] == []


def put_la(dev, *, mask=0, value=0, rise=0, fall=0, rate=3, pretrigger=1024, status=0, time=0):
    dev.write(g.LA_RATE, [rate, pretrigger, mask, value, rise, fall])
    dev.write32(g.LA_STATUS, status)
    dev.write(g.LA_TRIGGER_TIME_LO, [time & 0xFFFF_FFFF, time >> 32])
    dev.write32(g.LA_PULSE_MAX, 0xFFFF_FFFF)


def test_la_trigger_status_reads_the_pattern_and_state():
    dev = LaDevice()
    put_la(dev, mask=0b11, value=0b01, rise=0b100, status=0b011, time=1 << 32 | 7)
    out = probe_of(dev).la_trigger_status()
    assert out["mask"] == 3 and out["value"] == 1 and out["rise"] == 4 and out["fall"] == 0
    assert out["external_only"] is False
    assert out["rate_hz"] == pytest.approx(25e6) and out["pretrigger"] == 1024
    assert out["internal"] == {"capturing": True, "triggered": True, "done": False, "trigger_time": 1 << 32 | 7}
    assert out["filter"] == {"channels": 0, "cycles": 0, "min_width_ns": None}
    assert out["pulse"] == {"mode": "none"}
    assert "deep" not in out


def test_la_trigger_status_external_only_is_the_never_matching_pattern():
    """external_only は「ch0 が 1 かつ ch0 が立ち下がる」（決して成り立たない）で書く: 読んだその形をそのまま示す。"""
    dev = LaDevice()
    put_la(dev, mask=1, value=1, fall=1)
    assert probe_of(dev).la_trigger_status()["external_only"] is True


def test_la_trigger_status_no_trigger_time_before_trigger():
    dev = LaDevice()
    put_la(dev, status=0b001, time=99)
    assert probe_of(dev).la_trigger_status()["internal"]["trigger_time"] is None


def test_la_trigger_status_filter_and_pulse():
    dev = LaDevice()
    put_la(dev)
    dev.write32(g.LA_FILTER, 0b10 << g.LA_FILTER_CHANNELS[0] | 5 << g.LA_FILTER_CYCLES[0])
    dev.write(g.LA_PULSE, [1 << g.LA_PULSE_MODE[0] | 0 << g.LA_PULSE_LEVEL[0] | 3 << g.LA_PULSE_CHANNEL[0], 10,
                           0xFFFF_FFFF])
    out = probe_of(dev).la_trigger_status()
    assert out["filter"] == {"channels": 2, "cycles": 5, "min_width_ns": pytest.approx(5 / HZ * 1e9)}
    assert out["pulse"] == {"mode": "width", "level": 0, "channel": 3, "min_ns": pytest.approx(10 / HZ * 1e9),
                            "max_ns": None}


def test_la_trigger_status_deep_buffer_state():
    dev = LaDevice(la_depth=0, mem_bytes=1 << 27, la_filter=False, la_pulse=False)
    put_la(dev)
    dev.write32(g.LA_DEEP_STATUS, 0b110)
    dev.write(g.LA_DEEP_TRIGGER_TIME_LO, [42, 0])
    out = probe_of(dev).la_trigger_status()
    assert "internal" not in out and "filter" not in out and "pulse" not in out
    assert out["deep"] == {"capturing": False, "triggered": True, "done": True, "trigger_time": 42}


def test_la_trigger_status_without_la():
    with pytest.raises(RuntimeError):
        probe_of(LaDevice(la_channels=0)).la_trigger_status()
