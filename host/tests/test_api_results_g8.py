"""API の応答の形（仕様の result）を、ほかの試験が呼ばないメソッドで確かめる（プローブ本体・クロストリガ・電圧）。
照合そのものは tests/api_result_check.py が呼び出しごとにする。ここは呼ぶだけと、その値の事実。"""

from types import SimpleNamespace

import pytest
from caps_fakes import caps_for
from probe_fakes import FakeDevice

from fprobe import generated as g
from fprobe.api.spec import load_spec
from fprobe.mcp_server import Probe
from fprobe.xtrig import Source


class _Device(FakeDevice):
    """本物の Capabilities を返す偽の機器（クロストリガ・補助マイコンあり）。"""

    def __init__(self):
        super().__init__()
        self.caps = caps_for(xtrig=True, trig_seq=True, aux=True, sampler=True, la_channels=10)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=_Device())
    return p


def test_csr_read_and_write(probe):
    assert probe.csr_write(0x100, [1, 2]) is None
    assert probe.csr_read(0x100, 2) == [1, 2]


def test_voltage_and_its_history(probe):
    dev = probe.session.device
    dev.csr[g.AUX_TARGET], dev.csr[g.AUX_SAMPLES] = 3300, 5
    reading = probe.target_voltage()
    assert reading["volts"] == pytest.approx(3.3) and reading["over_range"] is False
    history = probe.probe_voltage_history()
    assert history["stats"]["readings"] == 1 and history["readings"][0]["volts"] == pytest.approx(3.3)


def test_route_returns_the_source_names_of_the_circuit(probe):
    out = probe.xtrig_route(to_la=["LA_TRIGGER"], to_inject=["host"])
    assert out == {"to_la": ["la_trigger"], "to_inject": ["host"]}         # 渡し方の大文字に依らない
    route = probe.session.device.csr[g.XTRIG_ROUTE]
    assert route >> g.XTRIG_ROUTE_TO_LA[0] & 0x3F == int(Source.LA_TRIGGER)


def test_fire_match_and_sequence(probe):
    assert probe.xtrig_fire() is None
    assert probe.xtrig_set_match(0x20000000) is None
    out = probe.xtrig_set_sequence([{"when": ["pattern_a"]}, {"when": ["host"], "within_us": 10}],
                                   pattern_a={"rise": 8})
    assert out["stages"] == 2 and out["armed"] is True
    dev = probe.session.device
    dev.csr[g.TRIGSEQ_STATUS] = 1 << g.TRIGSEQ_STATUS_FIRED[0]               # 発火した: 最後の段の事象の範囲も返す
    status = probe.xtrig_sequence_status()
    assert status["fired"] is True and [w["event"] for w in status["fire_windows"]] == ["host"]


def test_sampler_trigger_set_status_clear(probe):
    dev = probe.session.device
    dev.caps = caps_for(xtrig=True, sampler=True, sampler_match=True)
    assert probe.sampler_trigger_set(address=0xE000EDF0, mask=1 << 17, value=1 << 17, halt=False, la=True)["la"] is True
    assert probe.sampler_trigger_status()["enabled"] is True
    assert probe.sampler_trigger_clear() == {"enabled": False}


def test_watched_target_state_is_target_status_with_the_run_count():
    """見張りの状態（session.status の target・通知 target.state）は target.status の応答に run を足した形: 仕様の
    WatchedTargetState は TargetStatus の欄をそのまま持つ（片方だけ変えるとずれる）。"""
    defs = load_spec().defs
    connected = defs["WatchedTargetState"]["anyOf"][1]
    status = defs["TargetStatus"]
    assert connected["properties"] == {**status["properties"], "run": {"type": "integer"}}
    assert set(connected["required"]) == {*status["required"], "run"}


def test_system_status_is_built_from_target_status_and_fpga_obs():
    """system.status の mcu は target.status の欄 + memory・connect、fpga は fpga_obs.status の欄 + 最後に分かった値
    （fpga_obs.read の項目 + fields）: 仕様の形も元のメソッドの形と同じ欄を持つ（片方だけ変えるとずれる）。"""
    spec = load_spec()
    result = spec.methods["system.status"].result["properties"]
    status = spec.defs["TargetStatus"]
    mcu = result["mcu"]["anyOf"][0]["properties"]
    assert {k: v for k, v in mcu.items() if k not in ("connected", "memory", "connect")} == status["properties"]
    obs = spec.methods["fpga_obs.status"].result["properties"]
    fpga = result["fpga"]["anyOf"][1]["properties"]
    assert {k: v for k, v in fpga.items() if k not in ("available", "latest", "gaps", "window", "latest_error")} == obs
    item = spec.methods["fpga_obs.read"].result["anyOf"][1]["properties"]["items"]["items"]
    latest = fpga["latest"]["items"]["properties"]
    assert {k: v for k, v in latest.items() if k != "age_us"} == item["properties"]      # age_us は system.status が足す
