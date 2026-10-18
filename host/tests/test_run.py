"""fprobe.run（`python -m fprobe run firmware.elf`）: 試験の結果の行の読み取り（Unity・Zephyr ztest・defmt-test・TAP・
利用者の正規表現）、JUnit XML、書き込み → 実行 → 出力 → 終わり方の判定と、故障・時間切れの時に残すもの。
機器は使わない（筋書きどおりに動く偽の Probe と、本物の Probe + 偽の CPU）。"""

import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_probe_observe import BKPT_AB, FakeCpu, connect
from test_probe_recording import FakeDevice

from fprobe import __main__ as cli
from fprobe import run as run_mod
from fprobe.mcp_server import Probe
from fprobe.run import (EXIT_ABNORMAL, EXIT_ERROR, EXIT_FAILED, EXIT_TIMEOUT, RunOptions, Runner, TestCase, TestLog,
                        junit_xml)

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
RTT_DEMO = FIRMWARE / "rtt_demo" / "rtt_demo.elf"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"


# --- 試験の結果の行 ------------------------------------------------------------------------------

def feed_lines(log: TestLog, text: str, source: str = "rtt") -> None:
    for i in range(0, len(text), 7):                          # 行の途中で切れて届く
        log.feed(source, text[i:i + 7])


def test_unity_results_and_summary():
    log = TestLog()
    feed_lines(log, "boot\r\ntest_main.c:12:test_add:PASS\ntest_main.c:20:test_sub:FAIL: Expected 2 Was 3\n"
                    "test_main.c:30:test_mul:IGNORE: later\n\n-----------------------\n3 Tests 1 Failures 1 Ignored \n")
    assert not log.finished
    feed_lines(log, "FAIL\n")
    assert log.finished and log.verdict == "failed"
    assert [(c.name, c.status) for c in log.cases] == [("test_add", "passed"), ("test_sub", "failed"),
                                                      ("test_mul", "skipped")]
    sub = log.cases[1]
    assert sub.message == "Expected 2 Was 3" and sub.file == "test_main.c" and sub.line == 20 and sub.suite == "test_main"


def test_unity_ok_summary_passes():
    log = TestLog()
    feed_lines(log, "t.c:1:a:PASS\n-----------------------\n1 Tests 0 Failures 0 Ignored\nOK\n")
    assert log.finished and log.verdict == "passed"


def test_zephyr_ztest_results():
    log = TestLog()
    feed_lines(log, "Running TESTSUITE math\n===\nSTART - test_add\n PASS - test_add in 0.001 seconds\n"
                    " FAIL - test_div in 0.002 seconds\n SKIP - test_pow in 0.000 seconds\n"
                    "PROJECT EXECUTION FAILED\n")
    assert [(c.suite, c.name, c.status, c.time_s) for c in log.cases] == [
        ("math", "test_add", "passed", 0.001), ("math", "test_div", "failed", 0.002), ("math", "test_pow", "skipped", 0.0)]
    assert log.finished and log.verdict == "failed"


def test_defmt_test_running_lines_and_all_passed():
    log = TestLog()
    feed_lines(log, "(1/2) running `add`...\n(2/2) running `sub`...\n")
    assert [(c.name, c.status) for c in log.cases] == [("add", "passed"), ("sub", "running")]
    feed_lines(log, "all tests passed!\n")
    assert log.finished and log.verdict == "passed" and log.cases[1].status == "passed"


def test_a_crash_fails_the_running_test():
    log = TestLog()
    feed_lines(log, "(1/2) running `add`...\npanicked at 'boom'\n")
    log.abort("HardFault（UNDEFINSTR）")
    assert log.cases[0].status == "error" and "HardFault" in log.cases[0].message


def test_tap_results_and_plan():
    log = TestLog()
    feed_lines(log, "1..3\nok 1 - add\nnot ok 2 - sub\n# diag\nok 3 - mul # SKIP no hw\n")
    assert [(c.name, c.status) for c in log.cases] == [("add", "passed"), ("sub", "failed"), ("mul", "skipped")]
    assert log.cases[2].message == "no hw"
    assert log.finished and log.verdict == "failed"


def test_user_patterns_finish_the_run():
    ok = TestLog(pass_pattern=r"ALL DONE", fail_pattern=r"ASSERT")
    feed_lines(ok, "x\nALL DONE\n")
    assert ok.finished and ok.verdict == "passed"
    bad = TestLog(pass_pattern=r"ALL DONE", fail_pattern=r"ASSERT")
    feed_lines(bad, "ASSERT failed at x.c:3\n")
    assert bad.finished and bad.verdict == "failed" and "ASSERT failed" in bad.failure


def test_sources_keep_their_own_partial_lines():
    log = TestLog()
    log.feed("rtt", "t.c:1:a:PA")
    log.feed("semihosting", "t.c:2:b:PASS\n")
    log.feed("rtt", "SS\n")
    assert [c.name for c in log.cases] == ["b", "a"]


# --- JUnit XML -----------------------------------------------------------------------------------

def test_junit_xml_lists_cases_failures_and_output():
    cases = [TestCase("add", "passed", suite="math", time_s=0.5), TestCase("sub", "failed", "Expected 2", suite="math"),
             TestCase("mul", "skipped", "later", suite="math"), TestCase("div", "error", "HardFault", suite="math")]
    root = ET.fromstring(junit_xml("app", cases, output="line1\nline2\n", duration_s=1.25))
    suite = root.find("testsuite")
    assert suite.get("name") == "app" and suite.get("tests") == "4" and suite.get("failures") == "1"
    assert suite.get("errors") == "1" and suite.get("skipped") == "1" and suite.get("time") == "1.250"
    by_name = {c.get("name"): c for c in suite.findall("testcase")}
    assert by_name["add"].get("classname") == "math" and by_name["add"].get("time") == "0.500"
    assert by_name["sub"].find("failure").get("message") == "Expected 2"
    assert by_name["mul"].find("skipped").get("message") == "later"
    assert by_name["div"].find("error").get("message") == "HardFault"
    assert suite.find("system-out").text == "line1\nline2\n"


def test_junit_xml_escapes_and_strips_control_characters():
    text = junit_xml("a<b>", [TestCase("x&y", "failed", "bad \x01 char <here>")], output="\x1b[31mred\x1b[0m")
    root = ET.fromstring(text)
    assert root.find("testsuite/testcase").get("name") == "x&y"
    assert "\x01" not in text and "\x1b" not in text


# --- 実行（偽の Probe） ------------------------------------------------------------------------------

class ScriptedProbe:
    """Runner が使う Probe の道具だけの偽物。steps は resume の後の、ループの 1 周ごとの出来事:
    {"rtt": 文字列, "semihosting": 文字列, "halt": True, "exit": 終了コード, "fault": True, "bkpt": True}。"""

    def __init__(self, steps=(), rtt_ready_after: int = 0, memory_map_ram=True):
        self.steps = list(steps)
        self.calls: list[tuple] = []
        self.state = "HALTED"
        self.pc = 0x20000100
        self.exit = None
        self.faulted = False
        self.bkpt = False
        self.rtt_attempts = 0
        self.rtt_ready_after = rtt_ready_after
        self.rtt_on = False
        self.logs = {0: "", "semihosting": ""}
        self.closed = False

    def _call(self, *args):
        self.calls.append(args)

    def names(self):
        return [c[0] for c in self.calls]

    # 接続・書き込み
    def target_connect(self, target="cortex_m", frequency_hz=4_000_000, connect_mode="halt", jtag_chain=None):
        self._call("connect", target, frequency_hz, connect_mode)
        return {"state": "HALTED"}

    def program_ram(self, path):
        self._call("program_ram", path)
        return {"reason": "user"}

    def flash(self, path, address=None):
        self._call("flash", path)
        self.state = "RUNNING"
        return {"seconds": 0.1}

    def elf_load(self, path):
        self._call("elf_load", path)
        return {}

    def target_reset(self, halt=False):
        self._call("reset", halt)
        self.state = "HALTED" if halt else "RUNNING"
        return {"state": self.state}

    def write_expression(self, expression, value, frame=0):
        self._call("write_expression", expression, value)
        return {"value": value}

    def target_memory_map(self):
        return [{"start": 0x20000000, "length": 0x18000, "is_ram": True}]

    # 実行
    def target_resume(self):
        self._call("resume")
        self.state = "RUNNING"
        return {"state": "RUNNING"}

    def target_halt(self):
        self._call("halt")
        self.state = "HALTED"
        return {"state": "HALTED", "pc": self.pc}

    def _step(self):
        if self.state != "RUNNING" or not self.steps:
            return
        step = self.steps.pop(0)
        for key in (0, "semihosting"):
            name = "rtt" if key == 0 else key
            if name in step:
                self.logs[key] += step[name]
        if step.get("halt") or "exit" in step or step.get("fault") or step.get("bkpt"):
            self.state = "HALTED"
            self.exit = {"code": step["exit"], "reason": 0x20026} if "exit" in step else None
            self.faulted = bool(step.get("fault"))
            self.bkpt = bool(step.get("bkpt"))

    def target_status(self):
        self._step()
        out = {"state": self.state}
        if self.state == "HALTED":
            out.update(pc=self.pc, where="main", function="main")
        return out

    def semihosting_status(self):
        return {"exit": self.exit, "requests": 0}

    def read_memory(self, address, length):
        return (b"\x00\xbe" if self.bkpt else b"\x00\xbf").hex()[:2 * length]

    def target_fault(self):
        if self.faulted:
            return {"faulted": True, "causes": [{"bit": "UNDEFINSTR"}], "exception": {"name": "HardFault"},
                    "frame": {"pc": {"address": 0x20000180, "where": "boom+4 (app.c:9)"}}}
        return {"faulted": False, "causes": []}

    def backtrace(self, variables=False, max_frames=64, max_elements=64):
        return [{"index": 0, "pc": self.pc, "function": "boom", "where": "boom+4"}]

    # 出力
    def rtt_start(self, address=None, ranges=None, text_channels=None):
        self.rtt_attempts += 1
        if self.rtt_attempts <= self.rtt_ready_after:
            raise RuntimeError("RTT の制御ブロックが無い")
        self.rtt_on = True
        return {"address": 0x20000000, "up": [{"index": 0}]}

    def rtt_read(self, channel=0, since=None, max_bytes=65536, encoding="text", timeout_s=0.0):
        data, self.logs[channel] = self.logs[channel], ""
        if channel == 0 and not self.rtt_on:
            self.logs[channel] = data
            data = ""
        return {"channel": channel, "data": data}

    def swo_start(self, baud=2_000_000):
        self._call("swo_start", baud)

    def swo_text(self, port=0, timeout_s=0.5):
        return {"port": port, "lines": [], "pending": ""}

    # 残すもの
    def snapshot_save(self, name="", path=None, **kw):
        self._call("snapshot_save", path)
        Path(path).write_bytes(b"snap")
        return {"path": path}

    def snapshot_export_core(self, path=None, out=None):
        self._call("export_core", path, out)
        Path(out).write_bytes(b"core")
        return {"path": out}

    def close(self):
        self.closed = True


def options(tmp_path, **kw) -> RunOptions:
    base = dict(elf=RTT_DEMO, out_dir=tmp_path / "out", timeout_s=2.0, poll_s=0.001, load="ram")
    base.update(kw)
    return RunOptions(**base)


def run(probe, tmp_path, **kw):
    echoed = []
    result = Runner(probe, options(tmp_path, **kw), echo=echoed.append).run()
    return result, "".join(echoed)


def test_semihosting_exit_zero_with_passing_tests(tmp_path):
    probe = ScriptedProbe([{"semihosting": "t.c:1:a:PASS\n"}, {"semihosting": "t.c:2:b:PASS\n", "exit": 0}])
    result, echoed = run(probe, tmp_path, junit=tmp_path / "junit.xml")
    assert result.exit_code == 0 and result.outcome == "exit" and result.target_exit_code == 0
    assert echoed == "t.c:1:a:PASS\nt.c:2:b:PASS\n"
    assert probe.names()[:2] == ["connect", "program_ram"] and "resume" in probe.names()
    suite = ET.parse(tmp_path / "junit.xml").getroot().find("testsuite")
    assert suite.get("tests") == "2" and suite.get("failures") == "0"
    saved = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert saved["exit_code"] == 0 and saved["outcome"] == "exit" and len(saved["cases"]) == 2
    assert (tmp_path / "out" / "output.log").read_text(encoding="utf-8") == echoed
    assert "snapshot_save" not in probe.names()                                   # 正常に終わったら残さない


def test_the_target_exit_code_is_passed_through(tmp_path):
    result, _ = run(ScriptedProbe([{"exit": 3}]), tmp_path)
    assert result.exit_code == 3 and result.target_exit_code == 3
    result, _ = run(ScriptedProbe([{"exit": 256}]), tmp_path)
    assert result.exit_code == EXIT_FAILED                                        # 下位 8 ビットが 0 でも失敗


def test_failing_test_lines_fail_even_with_exit_zero(tmp_path):
    result, _ = run(ScriptedProbe([{"rtt": "t.c:1:a:FAIL: no\n", "exit": 0}]), tmp_path)
    assert result.exit_code == EXIT_FAILED and "a" in result.reason


def test_a_fault_leaves_the_fault_backtrace_snapshot_and_core(tmp_path):
    probe = ScriptedProbe([{"rtt": "(1/1) running `boom`...\n"}, {"fault": True}])
    result, _ = run(probe, tmp_path, junit=tmp_path / "j.xml")
    out = tmp_path / "out"
    assert result.exit_code == EXIT_ABNORMAL and result.outcome == "fault" and "UNDEFINSTR" in result.reason
    assert json.loads((out / "fault.json").read_text(encoding="utf-8"))["faulted"]
    assert json.loads((out / "backtrace.json").read_text(encoding="utf-8"))[0]["function"] == "boom"
    assert (out / "snapshot.fsnap").read_bytes() == b"snap" and (out / "snapshot.core").is_file()
    assert set(result.artifacts) >= {"fault", "backtrace", "snapshot", "core"}
    case = ET.parse(tmp_path / "j.xml").getroot().find("testsuite/testcase")
    assert case.get("name") == "boom" and case.find("error") is not None


def test_a_timeout_halts_and_keeps_the_state(tmp_path):
    probe = ScriptedProbe([{"rtt": "tick\n"}] * 100000)
    result, _ = run(probe, tmp_path, timeout_s=0.05)
    assert result.exit_code == EXIT_TIMEOUT and result.outcome == "timeout"
    assert "halt" in probe.names() and "snapshot_save" in probe.names()
    case = ET.fromstring(junit_xml("x", result.junit_cases(), "")).find("testsuite/testcase")
    assert case.find("error") is not None                                       # 試験の行が無ければ 1 件として


def test_finishing_by_the_test_summary_does_not_wait_for_a_halt(tmp_path):
    probe = ScriptedProbe([{"rtt": "t.c:1:a:PASS\n-----------------------\n1 Tests 0 Failures 0 Ignored\nOK\n"}]
                          + [{"rtt": ""}] * 100000)
    result, _ = run(probe, tmp_path, timeout_s=5)
    assert result.exit_code == 0 and result.outcome == "tests" and result.duration_s < 5


def test_a_breakpoint_instruction_ends_the_run_like_probe_rs(tmp_path):
    probe = ScriptedProbe([{"rtt": "(1/1) running `add`...\nall tests passed!\n"}, {"bkpt": True}])
    probe.steps[0]["rtt"] = "(1/1) running `add`...\n"
    probe.steps.insert(1, {"rtt": "all tests passed!\n"})
    result, _ = run(probe, tmp_path, stop_on_summary=False)
    assert result.exit_code == 0 and result.outcome == "bkpt"


def test_an_unexpected_halt_is_abnormal(tmp_path):
    result, _ = run(ScriptedProbe([{"halt": True}]), tmp_path)
    assert result.exit_code == EXIT_ABNORMAL and result.outcome == "halted"


def test_rtt_is_attached_once_the_firmware_initialised_it(tmp_path):
    probe = ScriptedProbe([{"rtt": "hello\n"}, {"rtt": ""}, {"rtt": ""}, {"exit": 0}], rtt_ready_after=2)
    result, echoed = run(probe, tmp_path, rtt_retry_s=0)
    assert probe.rtt_attempts == 3 and echoed == "hello\n"


def test_flash_load_resets_and_variables_are_set_before_running(tmp_path):
    probe = ScriptedProbe([{"exit": 0}])
    result, _ = run(probe, tmp_path, load="flash", set_values={"semihosting_enabled": "1"})
    names = probe.names()
    assert names.index("flash") < names.index("reset") < names.index("write_expression") < names.index("resume")
    assert ("reset", True) in probe.calls and ("write_expression", "semihosting_enabled", 1) in probe.calls


def test_swo_is_started_when_asked(tmp_path):
    probe = ScriptedProbe([{"exit": 0}])
    run(probe, tmp_path, swo_baud=2_000_000)
    assert ("swo_start", 2_000_000) in probe.calls


def test_auto_load_picks_ram_for_an_sram_image():
    assert run_mod.choose_load(RTT_DEMO, [(0x20000000, 0x18000)]) == "ram"
    assert run_mod.choose_load(RTT_DEMO, []) == "ram"                              # Cortex-M の SRAM の番地
    assert run_mod.choose_load(RTT_DEMO, [(0x10000000, 0x1000)]) == "flash"


def test_a_probe_error_is_reported_with_its_own_code(tmp_path):
    class Broken(ScriptedProbe):
        def target_connect(self, *a, **k):
            raise RuntimeError("プローブが無い")
    result, _ = run(Broken(), tmp_path)
    assert result.exit_code == EXIT_ERROR and "プローブが無い" in result.reason


# --- 本物の Probe と偽の CPU ---------------------------------------------------------------------------

def test_with_a_real_probe_semihosting_output_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    cpu = FakeCpu(stops=[(0x20000200, 0x04, 0x20000400), (0x20000210, 0x20, 0x20000500)], image=RTT_DEMO)
    cpu.write_memory_block8(0x20000200, BKPT_AB)
    cpu.write_memory_block8(0x20000210, BKPT_AB)
    cpu.write_memory_block8(0x20000400, b"t.c:1:a:PASS\n\0")
    cpu.write_memory_block8(0x20000500, struct.pack("<II", 0x20026, 3))
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=FakeDevice())
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    echoed = []
    try:
        result = Runner(probe, options(tmp_path, load="none", reset=False, connect=False, rtt=False),
                        echo=echoed.append).run()
    finally:
        probe._stop_observing()
    assert result.exit_code == 3 and "".join(echoed) == "t.c:1:a:PASS\n" and result.cases[0].status == "passed"


# --- コマンド ---------------------------------------------------------------------------------------

def test_cli_run_builds_options_and_exits_with_the_code(tmp_path, monkeypatch):
    seen = {}

    def fake_run(opts, probe_args, echo=None):
        seen["opts"], seen["probe"] = opts, probe_args
        return SimpleNamespace(exit_code=7)
    monkeypatch.setattr(run_mod, "run_firmware", fake_run)
    with pytest.raises(SystemExit) as e:
        cli.main(["--serial", "S1", "run", str(RTT_DEMO), "--target", "stm32f401xe", "--timeout", "9",
                  "--load", "ram", "--junit", str(tmp_path / "j.xml"), "--set", "semihosting_enabled=1",
                  "--rtt", "--swo", "2000000", "--pass", "DONE", "--fail", "BAD", "--out", str(tmp_path / "o")])
    assert e.value.code == 7
    o = seen["opts"]
    assert o.elf == RTT_DEMO and o.target == "stm32f401xe" and o.timeout_s == 9 and o.load == "ram"
    assert o.set_values == {"semihosting_enabled": "1"} and o.rtt is True and o.swo_baud == 2_000_000
    assert o.pass_pattern == "DONE" and o.fail_pattern == "BAD" and o.junit == tmp_path / "j.xml"
    assert seen["probe"] == {"serial": "S1", "port": None, "eth": None}
