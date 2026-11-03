"""多段トリガの状態（xtrig.sequence_status）: 機器のレジスタの事実（今の段・発火・仕掛けたか・段の数）と、各段の状態。
画面は stage_states をそのまま描く（今の段と比べて求め直さない。docs/reports/2026-10-23-ui-computations.md の b28）。"""

from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.mcp_server import Probe
from probe_fakes import FakeDevice
from caps_fakes import with_caps


@pytest.fixture
def device():
    return FakeDevice()


@pytest.fixture
def probe(device):
    p = Probe("unused")
    p._session = SimpleNamespace(device=device)
    return p


def put(device, *, stages, enable, stage=0, fired=False, time=0):
    device.write32(g.TRIGSEQ_CONTROL, stages << g.TRIGSEQ_CONTROL_STAGES[0] | int(enable) << g.TRIGSEQ_CONTROL_ENABLE[0])
    device.write32(g.TRIGSEQ_STATUS, stage << g.TRIGSEQ_STATUS_STAGE[0] | int(fired) << g.TRIGSEQ_STATUS_FIRED[0])
    device.write(g.TRIGSEQ_FIRE_TIME_LO, [time & 0xFFFF_FFFF, time >> 32])


def test_armed_and_waiting_in_the_middle_stage(probe, device):
    put(device, stages=3, enable=True, stage=1)
    out = probe.xtrig_sequence_status()
    del out["config"]                                       # 段の設定の読み戻しは test_xtrig_readback.py
    assert out == {"stage": 1, "fired": False, "time": 0, "armed": True, "stages": 3,
                   "stage_states": ["done", "current", "waiting"], "fire_windows": []}


def test_fired_marks_every_stage_done(probe, device):
    put(device, stages=2, enable=True, stage=1, fired=True, time=1 << 33 | 5)
    out = probe.xtrig_sequence_status()
    assert out["fired"] and out["time"] == 1 << 33 | 5 and out["stage_states"] == ["done", "done"]


def test_not_armed_is_idle(probe, device):
    put(device, stages=2, enable=False)
    out = probe.xtrig_sequence_status()
    assert out["armed"] is False and out["stage_states"] == ["idle", "idle"]


def test_not_configured_has_no_stages(probe, device):
    out = probe.xtrig_sequence_status()
    assert out["stages"] == 0 and out["stage_states"] == []


def test_fire_windows_list_every_event_of_the_last_stage():
    """時刻の正しさ S1b の A2: 発火の時刻から、最後の段を進めえた事象ごとの点の範囲（TLV TIMING の seq_*）。どれが進めたかは
    回路が記録しないので選ばない。パターンは使っている ch の種類ごと（ピンと内部の ch）。"""
    from fprobe.caps import TimingPoint
    from fprobe.xtrig import Event

    declared = {"seq_pin": TimingPoint("seq_pin", "pin_edge", "tick", 7, 1, ("input_routing",)),
                "seq_internal": TimingPoint("seq_internal", "internal_edge", "tick", 7, 0, ()),
                "seq_event": TimingPoint("seq_event", "source_pulse", "tick", 2, 0, ())}

    dev = FakeDevice()
    dev.caps = with_caps(dev.capabilities(), timing=declared, la_channels=10, la_sample_hz=100_000_000)
    dev.capabilities()                                    # 開いたときに読んだ（last_capabilities）のはこの構成
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    put(dev, stages=1, enable=True, fired=True, time=1000)
    dev.write(g.TRIGSEQ_STAGE0, [int(Event.PATTERN_A | Event.DAP_MATCH) | 1 << 16])
    dev.write(g.TRIGSEQ_PATTERN_A, [0b1_0000_0001, 0])                   # mask: ch 0（ピン）と ch 8（内部）
    got = {(w["event"], w.get("cause")): (w["t_lo"], w["t_hi"]) for w in p.xtrig_sequence_status()["fire_windows"]}
    assert got == {("pattern_a", "internal"): (993, 993), ("pattern_a", "pin"): (993, 994), ("dap_match", None): (998, 998)}
