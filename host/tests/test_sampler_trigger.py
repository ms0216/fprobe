"""サンプラの値の一致のハードのクロストリガ（R21。源 bit5 sampler_match）。機器は使わない。
仕様 docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md"""

from types import SimpleNamespace

import pytest
from probe_fakes import FakeDevice

from fprobe import generated as g
from fprobe.mcp_server import Probe
from fprobe.xtrig import Source

DHCSR, S_HALT = 0xE000EDF0, 1 << 17


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    dev = FakeDevice()
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "sampler": True, "xtrig": True, "sampler_match": True})
    p._session = SimpleNamespace(device=dev)
    return p


def test_set_writes_the_condition_and_only_its_route_bit(probe):
    dev = probe.session.device
    dev.csr[g.XTRIG_ROUTE] = int(Source.HOST) << g.XTRIG_ROUTE_TO_LA[0]             # 先にある経路は残す
    out = probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=False, la=True)
    assert out == {"address": DHCSR, "mask": S_HALT, "value": S_HALT, "halt": False, "la": True}
    assert dev.csr[g.SAMPLER_MATCH_ADDR] == DHCSR
    assert dev.csr[g.SAMPLER_MATCH_MASK] == S_HALT and dev.csr[g.SAMPLER_MATCH_VALUE] == S_HALT
    assert dev.csr[g.SAMPLER_MATCH_CONTROL] & 1
    route = dev.csr[g.XTRIG_ROUTE]
    assert route >> g.XTRIG_ROUTE_TO_LA[0] & 0x3F == int(Source.SAMPLER_MATCH | Source.HOST)
    assert route >> g.XTRIG_ROUTE_TO_INJECT[0] & 0x3F == 0


def test_status_is_the_register_facts(probe):
    dev = probe.session.device
    probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=True, la=True)
    dev.csr[g.SAMPLER_MATCH_CONTROL] = 1 | 1 << 8 | 1 << 9
    dev.csr[g.SAMPLER_MATCH_SEEN], dev.csr[g.SAMPLER_MATCH_COUNT] = 40, 2
    dev.csr[g.SAMPLER_MATCH_TIME_LO], dev.csr[g.SAMPLER_MATCH_TIME_HI] = 5, 1
    dev.csr[g.SAMPLER_MATCH_PREV_LO], dev.csr[g.SAMPLER_MATCH_PREV_HI] = 3, 1
    dev.ticks = 1 << 40                                    # いまのタイムベース（回路の 48 ビットの時刻はこれより前）
    st = probe.sampler_trigger_status()
    assert st == {"enabled": True, "address": DHCSR, "mask": S_HALT, "value": S_HALT, "halt": True, "la": True,
                  "matched": True, "seen_any": True, "seen": 40, "count": 2,
                  "time": (1 << 32) + 5, "prev_time": (1 << 32) + 3}


def test_clear_disables_and_removes_only_its_route_bit(probe):
    dev = probe.session.device
    dev.csr[g.XTRIG_ROUTE] = int(Source.HOST) << g.XTRIG_ROUTE_TO_INJECT[0]
    probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=True, la=False)
    probe.sampler_trigger_clear()
    assert dev.csr[g.SAMPLER_MATCH_CONTROL] & 1 == 0
    assert dev.csr[g.XTRIG_ROUTE] == int(Source.HOST) << g.XTRIG_ROUTE_TO_INJECT[0]
    assert probe.sampler_trigger_status()["enabled"] is False


def test_set_needs_an_action_and_the_capability(probe):
    with pytest.raises(ValueError):
        probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=False, la=False)
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "sampler_match": False})
    with pytest.raises(RuntimeError, match="sampler_match"):
        probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=True, la=False)


def test_source_name_is_accepted_by_xtrig_route(probe):
    probe.xtrig_route(to_la=["sampler_match"])
    assert probe.session.device.csr[g.XTRIG_ROUTE] >> g.XTRIG_ROUTE_TO_LA[0] & 0x3F == int(Source.SAMPLER_MATCH)


def test_halt_needs_the_injector(probe):
    # 故障注入（割り込み書き込み）の無い構成では、halt の経路を立てても MCU は止まらない: 断る（la だけなら受け付ける）
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "inject": False})
    with pytest.raises(RuntimeError, match="inject"):
        probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=True, la=False)
    assert dev.csr.get(g.SAMPLER_MATCH_CONTROL, 0) & 1 == 0
    assert probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=False, la=True)["la"]


def test_xtrig_status_reports_the_route_from_the_register(probe):
    # 横断レビュー: 画面の経路の表は機器のレジスタの事実から始める（ほかの道具が書いた 1 ビットを黙って消さない）
    probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=True, la=False)
    probe.session.device.csr[g.XTRIG_ROUTE] |= int(Source.HOST) << g.XTRIG_ROUTE_TO_LA[0]
    st = probe.xtrig_status()
    assert st["route"] == {"to_la": ["host"], "to_inject": ["sampler_match"]}


def test_routing_a_source_the_design_lacks_is_refused(probe):
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "sampler_match": False, "obs_match": False})
    with pytest.raises(RuntimeError, match="sampler_match"):
        probe.xtrig_route(to_la=["sampler_match"])
    with pytest.raises(RuntimeError, match="obs_match"):
        probe.xtrig_route(to_inject=["obs_match"])


def test_status_times_follow_the_timebase_across_the_48_bit_wrap(probe):
    """回路の時刻は 48 ビット。タイムベース（64 ビット）が折り返しを過ぎても、LA のトリガ時刻などと比べられる時刻で返す"""
    dev = probe.session.device
    wrap = 1 << 48
    probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=True, la=False)
    dev.csr[g.SAMPLER_MATCH_CONTROL] = 1 | 1 << 8 | 1 << 9
    dev.csr[g.SAMPLER_MATCH_SEEN], dev.csr[g.SAMPLER_MATCH_COUNT] = 40, 1
    dev.csr[g.SAMPLER_MATCH_TIME_LO], dev.csr[g.SAMPLER_MATCH_TIME_HI] = 50 & 0xFFFFFFFF, 0           # 折り返しの後
    dev.csr[g.SAMPLER_MATCH_PREV_LO], dev.csr[g.SAMPLER_MATCH_PREV_HI] = (wrap - 50) & 0xFFFFFFFF, (wrap - 50) >> 32   # 前
    dev.ticks = 3 * wrap + 1000
    st = probe.sampler_trigger_status()
    assert st["time"] == 3 * wrap + 50 and st["prev_time"] == 2 * wrap + (wrap - 50)
    assert st["prev_time"] < st["time"]
