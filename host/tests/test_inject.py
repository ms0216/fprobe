"""fprobe.inject: 故障注入の台本の検証と、試行（仕掛け・成立待ち・判定・戻し）の進め方（偽の機器）。"""

import pytest

from fprobe.inject import InjectScript, ScriptError, run_script
from fprobe.recording import Recorder, Recording
from fprobe.xtrig import InjectStatus

HZ = 1_000_000


class FakeHost:
    """InjectHost の偽物: メモリ（番地 → 語）、割り込み書き込み（fire か「条件の成立」で書く）、記録。"""

    def __init__(self, recording=None, triggers=True, result="ok"):
        self.memory = {0x20000000: 0b1010, 0x20000004: 0}
        self.symbols = {"g_mode": 0x20000004}
        self.inject = None
        self.count = 0
        self.time = 5000
        self.armed = None
        self.triggers = triggers
        self.result = result
        self.log = []
        self.notes = []
        self.rec = recording
        self.state_value = "running"
        self.clock = 0.0

    # 番地・メモリ
    def resolve(self, address):
        return self.symbols[address] if isinstance(address, str) else address

    def read_memory(self, address, size):
        return self.memory.get(address, 0) & ((1 << 8 * size) - 1)

    def write_memory(self, address, value, size):
        self.log.append(("write", address, value))
        self.memory[address] = value

    # 割り込み書き込み
    def set_inject(self, address, value, ap):
        self.inject = (address, value, ap)

    def arm(self, when):
        self.armed = when
        self.log.append(("arm", next(iter(when))))
        if self.triggers and "host" not in when:
            self._fire()

    def fire(self):
        self.log.append(("fire",))
        self._fire()

    def _fire(self):
        if self.armed is None:
            return
        self.count += 1
        self.time += 100
        if self.result == "ok":
            self.memory[self.inject[0]] = self.inject[1]

    def disarm(self):
        self.armed = None
        self.log.append(("disarm",))

    def restore_default(self):
        self.log.append(("default",))

    def timebase_hz(self):
        return HZ

    def inject_status(self):
        return InjectStatus(False, self.result, 1, self.count, self.time)

    # ターゲット
    def state(self):
        return self.state_value

    def evaluate(self, expression):
        return {"g_mode": self.memory[0x20000004]}[expression]

    def reset(self, kind):
        self.log.append(("reset", kind))

    def resume(self):
        self.log.append(("resume",))

    # 記録・時間
    def recording(self):
        return self.rec

    def note(self, stream, kind, t, t_end, body):
        self.notes.append((stream, kind, t, t_end, body))

    def sleep(self, seconds):
        self.clock += seconds

    def monotonic(self):
        return self.clock


def _script(**trial):
    base = {"id": "t1", "when": {"host": {"delay_s": 0.01}}, "write": {"address": "g_mode", "value": 7},
            "expect": [{"expression": "g_mode", "equals": 7}]}
    return InjectScript.parse({"name": "s", "requirements": {"REQ-30": "壊れても止まらない"},
                               "trials": [{**base, **trial}]})


def test_scripts_are_validated():
    with pytest.raises(ScriptError, match="when"):
        _script(when={"host": {}, "pattern": {"rise": 1}})
    with pytest.raises(ScriptError, match="when"):
        _script(when={"sometime": {}})
    with pytest.raises(ScriptError, match="pattern"):
        _script(when={"pattern": {"rising": 8}})
    with pytest.raises(ScriptError, match="dap_match"):
        _script(when={"dap_match": {"mask": 1}})
    with pytest.raises(ScriptError, match="stages"):
        _script(when={"sequence": {"stages": [{"when": ["soon"]}]}})
    _script(when={"sequence": {"stages": [{"when": ["pattern_a"]}, {"when": ["pattern_b"], "within_us": 10}],
                               "pattern_a": {"rise": 8}, "pattern_b": {"fall": 32}}})
    with pytest.raises(ScriptError, match="write"):
        _script(write={"address": 0x20000000})                     # value も flip も無い
    with pytest.raises(ScriptError, match="expect"):
        _script(expect=[{"guess": 1}])
    with pytest.raises(ScriptError, match="規則"):
        _script(expect=[{"rules": [{"id": "x", "kind": "never"}]}])
    with pytest.raises(ScriptError, match="重複"):
        InjectScript.parse({"trials": [{"id": "a", "when": {"host": {}}, "write": {"address": 0, "value": 0}}] * 2})


def test_a_host_triggered_trial_writes_judges_and_records_at_the_hardware_time():
    host = FakeHost()
    result = run_script(host, _script(requirement="REQ-30"))
    trial = result["trials"][0]
    assert result["verdict"] == "pass" and trial["verdict"] == "pass" and trial["injected"]
    assert trial["address"] == 0x20000004 and trial["value"] == 7 and trial["t"] == 5100
    assert host.log.index(("arm", "host")) < host.log.index(("fire",)) < host.log.index(("disarm",))
    assert host.log[-1] == ("default",)                              # 壊す書き込みを残さない
    (stream, kind, t, t_end, body), = host.notes
    assert (stream, kind, t) == ("inject", "trial", 5100) and body["verdict"] == "pass" and t_end > t


def test_a_condition_that_never_holds_is_not_triggered_and_still_disarmed():
    host = FakeHost(triggers=False)
    result = run_script(host, _script(when={"pattern": {"rise": 8}}, timeout_s=0.05))
    assert result["trials"][0]["verdict"] == "not_triggered" and ("disarm",) in host.log
    assert result["verdict"] == "not_triggered"


def test_flip_reads_the_word_first_and_keeps_the_previous_value():
    host = FakeHost()
    result = run_script(host, _script(write={"address": 0x20000000, "flip": 1},
                                      expect=[{"memory": {"address": 0x20000000, "size": 4}, "equals": 0b1011}]))
    trial = result["trials"][0]
    assert trial["previous"] == 0b1010 and trial["value"] == 0b1011 and trial["verdict"] == "pass"


def test_failed_checks_fail_the_trial_with_reasons():
    host = FakeHost()
    host.state_value = "halted"
    result = run_script(host, _script(expect=[{"state": "running"}, {"expression": "g_mode", "max": 3}]))
    checks = result["trials"][0]["checks"]
    assert result["trials"][0]["verdict"] == "fail" and [c["ok"] for c in checks] == [False, False]
    assert checks[0]["value"] == "halted"


def test_an_inject_error_is_reported():
    host = FakeHost(result="swd_error")
    assert run_script(host, _script())["trials"][0]["verdict"] == "inject_error"


def test_restore_writes_resets_and_resumes_after_the_judgement():
    host = FakeHost()
    run_script(host, _script(restore={"write": [{"address": "g_mode", "value": 0}], "reset": "system",
                                      "resume": True}))
    assert host.memory[0x20000004] == 0
    tail = host.log[host.log.index(("disarm",)):]
    assert tail == [("disarm",), ("write", 0x20000004, 0), ("reset", "system"), ("resume",), ("default",)]


def test_rules_are_applied_to_the_observation_window_of_the_recording(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ)
    rec.add("swo", "exception", 5150, a=3)                             # 注入（5100）の後に故障
    rec.add("swo", "exception", 100, a=3)                              # 注入の前は数えない
    rec.close()
    host = FakeHost(recording=Recording.open(tmp_path / "r"))
    result = run_script(host, _script(observe_s=0.001, expect=[
        {"rules": [{"id": "no-fault", "kind": "never", "event": {"stream": "swo", "kind": "exception"}}]}]))
    check = result["trials"][0]["checks"][0]
    assert not check["ok"] and check["value"]["rules"][0]["violations"][0]["t"] == 5150


def test_rules_without_a_recording_are_an_error_not_a_pass():
    host = FakeHost()
    result = run_script(host, _script(expect=[{"rules": [{"id": "n", "kind": "never", "event": {"stream": "swo"}}]}]))
    check = result["trials"][0]["checks"][0]
    assert not check["ok"] and "記録" in check["error"]


def test_repeat_runs_the_trial_several_times():
    result = run_script(FakeHost(), _script(repeat=3))
    assert [t["repeat"] for t in result["trials"]] == [0, 1, 2] and result["counts"]["pass"] == 3


def test_unaligned_addresses_are_refused_before_arming():
    host = FakeHost()
    result = run_script(host, _script(write={"address": 0x20000002, "value": 1}))
    assert result["trials"][0]["verdict"] == "error" and not any(x[0] == "arm" for x in host.log)
