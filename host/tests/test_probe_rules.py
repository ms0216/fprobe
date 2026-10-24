"""Probe の道具 rules_* / inject_run（U25）: 記録への規則の検査・状態遷移図・証跡、偽の機器での故障注入。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.mcp_server import Probe
from fprobe.recording import Recorder
from fprobe.xtrig import DHCSR, HALT, Source
from probe_fakes import HZ, DictMemory, FakeDevice, FakeTarget

ISR = {"stream": "testpoint", "kind": "enter", "body": {"function": "isr"}}
ISR_END = {"stream": "testpoint", "kind": "exit", "body": {"function": "isr"}}
LATENCY = {"name": "lat", "requirements": {"REQ-1": "50 µs 以内"},
           "rules": [{"id": "lat", "kind": "interval", "start": ISR, "end": ISR_END, "max_us": 50,
                      "requirement": "REQ-1"}]}


class XtrigDevice(FakeDevice):
    """クロストリガの割り込み書き込みを真似る: 源 host が割り込み書き込みへつながっていれば、fire で target に書く。"""

    def __init__(self, target):
        super().__init__()
        self.target = target

    def write32(self, address, value):
        super().write32(address, value)
        route = self.csr.get(g.XTRIG_ROUTE, 0) >> g.XTRIG_ROUTE_TO_INJECT[0] & 0xF
        if address == g.XTRIG_COMMAND and value & 1 and route & Source.HOST:
            status = self.csr.get(g.XTRIG_INJECT_STATUS, 0)
            count = (status >> 16) + 1
            self.csr[g.XTRIG_INJECT_STATUS] = count << 16
            self.csr[g.XTRIG_INJECT_TIME_LO] = 7_000_000
            self.target.write_memory(self.csr[g.XTRIG_INJECT_ADDR], self.csr[g.XTRIG_INJECT_VALUE])


def _recording(home: Path, name: str, rows) -> None:
    rec = Recorder(home / "recordings" / name, timebase_hz=HZ, meta={"name": name})
    rec.add_many(rows)
    rec.close()


def tp(kind, t):
    return ("testpoint", kind, t, 0, 0, {"function": "isr"})


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    if p._recorder is not None:
        p.recording_stop()


def test_rules_check_marks_violations_and_compares_with_a_baseline(probe, tmp_path):
    _recording(tmp_path, "slow", [tp("enter", 0), tp("exit", 100 * 100)])          # 100 µs（HZ = 100 MHz）
    _recording(tmp_path, "good", [tp("enter", 0), tp("exit", 10 * 100)])
    probe.recording_open("slow")
    result = probe.rules_check(rules=LATENCY, baseline="good")
    assert result["verdict"] == "fail" and result["marked"] == 1
    assert result["rules"][0]["baseline"]["delta"]["max_us"] == 90
    assert probe.recording_find({"stream": "rule"})["count"] == 1
    with pytest.raises(ValueError):
        probe.rules_check(rules=LATENCY, path="x.yaml")                              # どちらか一方
    with pytest.raises(KeyError):
        probe.rules_check(rules=LATENCY, baseline="none")


def test_rules_last_keeps_the_last_check_with_when_and_who(probe, tmp_path):
    """最後の rules_check を、応答のまま・時刻・呼び出し元とともに読める（画面と AI が同じものを見る）"""
    assert probe.rules_last() == {"rules": None, "inject": None}
    _recording(tmp_path, "slow", [tp("enter", 0), tp("exit", 100 * 100)])
    probe.recording_open("slow")
    ai = {"id": 3, "name": "claude", "role": "ai"}
    result = probe.rules_check(rules=LATENCY, caller=ai)
    last = probe.rules_last()
    assert last["rules"]["result"] == result and last["rules"]["caller"] == ai and last["rules"]["at"] > 0
    assert last["inject"] is None


def test_rules_load_from_a_yaml_file_and_bad_rules_are_errors(probe, tmp_path):
    _recording(tmp_path, "r", [tp("enter", 0), tp("exit", 100)])
    probe.recording_open("r")
    path = tmp_path / "rules.yaml"
    path.write_text("name: y\nrules:\n  - id: never-exit\n    kind: never\n    event: {stream: testpoint, kind: exit}\n",
                    encoding="utf-8")
    result = probe.rules_check(path=str(path), mark=False)
    assert result["rules"][0]["verdict"] == "fail" and result["ruleset"]["source"] == str(path)
    assert "marked" not in result
    with pytest.raises(ValueError, match="kind"):
        probe.rules_check(rules={"rules": [{"id": "x", "kind": "maybe"}]})


def test_rules_states_returns_and_writes_the_dot(probe, tmp_path):
    _recording(tmp_path, "s", [("sample", "word", t, 0x20000000, v) for t, v in ((0, 0), (100, 1), (300, 0))])
    probe.recording_open("s")
    out = tmp_path / "s.dot"
    sm = probe.rules_states(event={"stream": "sample"}, field="b", labels={"0": "idle", "1": "run"},
                            dot_path=str(out))
    assert sm["states"]["run"]["dwell_us"]["max"] == 2 and out.read_text(encoding="utf-8") == sm["dot"]


def test_rules_report_writes_the_evidence(probe, tmp_path):
    with pytest.raises(ValueError):
        probe.rules_report(path=str(tmp_path / "e.md"))                               # まだ何も検査していない
    _recording(tmp_path, "r", [tp("enter", 0), tp("exit", 100)])
    probe.recording_open("r")
    probe.rules_check(rules=LATENCY)
    out = probe.rules_report(path=str(tmp_path / "e.md"))
    text = (tmp_path / "e.md").read_text(encoding="utf-8")
    assert out["verdict"] == "pass" and "REQ-1" in text and out["requirements"] == {"REQ-1": "pass"}


def _connect(probe, memory):
    target = FakeTarget(memory, {"pc": 0x08000100}, halted=False)
    probe._session = SimpleNamespace(device=XtrigDevice(target))
    probe._debug = SimpleNamespace(target=target, close=lambda: None)
    return probe._session.device


def test_inject_run_writes_through_the_cross_trigger_and_restores_the_default(probe):
    memory = DictMemory({0x20000010: 5})
    dev = _connect(probe, memory)
    dev.csr[g.XTRIG_ROUTE] = int(Source.LA_TRIGGER) << g.XTRIG_ROUTE_TO_LA[0]   # ロジアナへのつなぎは保つ
    script = {"name": "s", "trials": [{"id": "t", "requirement": "REQ-9", "when": {"host": {"delay_s": 0}},
                                       "write": {"address": 0x20000010, "flip": 0x100}, "timeout_s": 2.0, "observe_s": 0,
                                       "expect": [{"memory": {"address": 0x20000010}, "equals": 0x105},
                                                  {"state": "running"}]}]}
    result = probe.inject_run(script=script)
    trial = result["trials"][0]
    assert result["verdict"] == "pass" and trial["t"] == 7_000_000 and memory.u32(0x20000010) == 0x105
    assert dev.csr[g.XTRIG_ROUTE] == int(Source.LA_TRIGGER) << g.XTRIG_ROUTE_TO_LA[0]
    assert [dev.csr[g.XTRIG_INJECT_ADDR], dev.csr[g.XTRIG_INJECT_VALUE]] == [DHCSR, HALT]
    report = probe.rules_report(path=str(Path(probe._recordings_dir()).parent / "e.json"), format="json")
    assert report["requirements"] == {"REQ-9": "pass"}


def test_inject_run_arms_the_sequencer_for_a_pin_pattern_and_times_out(probe):
    dev = _connect(probe, DictMemory())
    writes = []
    original = dev.write32

    def spy(address, value):
        writes.append((address, value))
        original(address, value)
    dev.write32 = spy
    result = probe.inject_run(script={"trials": [{"id": "p", "when": {"pattern": {"rise": 8}}, "timeout_s": 0.02, "observe_s": 0,
                                                  "write": {"address": 0x20000000, "value": 1}}]})
    assert result["trials"][0]["verdict"] == "not_triggered"
    routes = [v >> g.XTRIG_ROUTE_TO_INJECT[0] & 0xF for a, v in writes if a == g.XTRIG_ROUTE]
    assert routes == [int(Source.SEQUENCE), 0]                                       # 仕掛けて、切った
    assert dev.csr[g.TRIGSEQ_PATTERN_A + 4] == 8                                    # パターン A の立ち上がり
    assert dev.csr[g.TRIGSEQ_CONTROL] & 1 == 0                                      # 多段トリガも止めた


def test_inject_run_resolves_variables_through_the_elf(probe):
    _connect(probe, DictMemory())
    with pytest.raises(ValueError, match="変数名"):
        probe._inject_host().resolve("g_mode")                                       # ELF を読んでいない
    probe._symbols = SimpleNamespace(variable=lambda name: SimpleNamespace(address=0x20000040))
    assert probe._inject_host().resolve("g_mode") == 0x20000040
    assert probe._inject_host().resolve("0x20000044") == 0x20000044
