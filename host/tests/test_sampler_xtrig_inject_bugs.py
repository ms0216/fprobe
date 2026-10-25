"""検出専用: サンプラ（sampler.py）・クロストリガ（xtrig.py）・故障注入の台本（inject.py）・RTT の不具合の再現
（台帳 docs/reports/bugs/arm-a-jtag-sampler-xtrig.md）。どれも xfail(strict=True): 直ったら XPASS で失敗するので、
marker を外して通常の試験にする。"""

from types import SimpleNamespace

import pytest
from probe_fakes import FakeDevice as ProbeFakeDevice
from test_inject import FakeHost
from test_rtt import BASE, RAM, FakeMemory, build
from test_sampler import FakeDevice as SamplerDevice

from fprobe import generated as g
from fprobe.inject import InjectScript, ScriptError, run_script
from fprobe.rtt import Rtt
from fprobe.sampler import Sampler, plan_slots, swd_hz_for_rate
from fprobe.xtrig import Event, Pattern, Stage, TriggerSequencer, pulse_ticks

# --- sampler.py ---------------------------------------------------------------------------------------


def test_plan_slots_rejects_addresses_outside_32_bits():
    with pytest.raises(ValueError):
        plan_slots([(0xFFFF_FFFC, 8)])                    # 末尾が 0x1_0000_0000 に出る
    with pytest.raises(ValueError):
        plan_slots([(-4, 4)])


def test_swd_hz_for_rate_rejects_a_non_positive_rate():
    with pytest.raises(ValueError):
        swd_hz_for_rate([(0x2000_0000, 1)], 0, 100_000_000)


def test_sampler_ap_is_range_checked():
    dev = SamplerDevice()
    s = Sampler(dev)
    with pytest.raises(ValueError):
        s.configure([(0x2000_0000, 4)], 1e-4, ap=256)
    s.start()
    assert dev.csr[g.SAMPLER_CONTROL] >> 16 == 0, "ap の溢れがゆらぎの欄に入った"


# --- xtrig.py ------------------------------------------------------------------------------------------


def test_pattern_wider_than_16_bits_is_rejected():
    seq = TriggerSequencer(ProbeFakeDevice())
    with pytest.raises(ValueError):
        seq.configure([Stage(Event.PATTERN_A)], Pattern(mask=0x1_0000))


def test_non_positive_within_is_rejected():
    seq = TriggerSequencer(ProbeFakeDevice())
    with pytest.raises(ValueError):
        seq.configure([Stage(Event.PATTERN_A), Stage(Event.PATTERN_B, within_s=-1e-6)])


def test_a_stage_without_any_condition_is_rejected():
    seq = TriggerSequencer(ProbeFakeDevice())
    with pytest.raises(ValueError):
        seq.configure([Stage(Event(0))])


def test_pulse_ticks_rejects_a_negative_duration():
    with pytest.raises(ValueError):
        pulse_ticks(-1e-6, 100_000_000)


def test_xtrig_route_unknown_source_is_a_value_error():
    from fprobe.mcp_server import Probe
    p = Probe("unused")
    p._session = SimpleNamespace(device=ProbeFakeDevice())
    with pytest.raises(ValueError):
        p.xtrig_route(to_la=["laa_trigger"])


# --- inject.py -----------------------------------------------------------------------------------------

def _trial(**when):
    return {"id": "t", "when": when, "write": {"address": 0x20000000, "value": 1}, "timeout_s": 1, "observe_s": 0.1}


def test_sequence_stage_fields_are_validated_when_the_script_is_read():
    def parse(**when):
        return InjectScript.parse({"trials": [_trial(**when)]})
    with pytest.raises(ScriptError):
        parse(sequence={"stages": [{"when": ["pattern_a"], "count": 0}]})
    with pytest.raises(ScriptError):
        parse(sequence={"stages": [{"when": ["pattern_a"]}, {"when": ["pattern_b"], "within_us": -5}]})
    with pytest.raises(ScriptError):
        parse(sequence={"stages": [{"when": ["pattern_a"], "count": "many"}]})
    with pytest.raises(ScriptError):
        parse(dap_match={"address": 0x20000000, "on_write": "yes"})


class _FailsAfterInjection(FakeHost):
    """割り込み書き込みが起きた後の状態の読みで通信が切れる。"""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def inject_status(self):
        self.reads += 1
        if self.reads >= 2:                                   # 1 回目 = 仕掛ける前の回数、2 回目 = 成立待ち
            raise ConnectionError("lost")
        return super().inject_status()


def test_restore_runs_even_when_the_status_read_fails_after_the_injection():
    host = _FailsAfterInjection()
    data = {"trials": [{**_trial(host={"delay_s": 0}), "restore": {"write": [{"address": 0x20000000, "value": 0}]}}]}
    result = run_script(host, InjectScript.parse(data))
    assert result["trials"][0]["verdict"] == "error"
    assert ("write", 0x20000000, 0) in host.log, "注入が起きたのに restore の書き戻しが実行されていない"


# --- rtt.py --------------------------------------------------------------------------------------------

class _StrictMemory(FakeMemory):
    """範囲の外を読むと誤り（pyOCD は割り当てられた領域の外を読めない）。"""

    def read_memory(self, address, length):
        if address < BASE or address + length > BASE + RAM:
            raise IndexError(f"0x{address:08X}+{length} は範囲の外")
        return super().read_memory(address, length)


def test_open_survives_a_channel_name_near_the_end_of_the_region():
    mem = _StrictMemory()
    build(mem, BASE + 0x100, up=[("Terminal", BASE + 0x400, 256, 0)], down=[], names_at=BASE + RAM - 9)
    block = Rtt(mem, BASE + 0x100).open()
    assert block.up[0].name == "Terminal"
