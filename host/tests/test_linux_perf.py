"""Linux の perf（R26 段階 4。仕様 §3.3 の linux.perf.*）: 数え上げ（stat）と標本（record）を事実のまま返す。偽の ssh で試す。"""

import json

import pytest
from test_probe_linux import probe  # noqa: F401  （fixture）

from fprobe.linux.perf import parse_perf_sample, parse_perf_stat

STAT = ("4098647,,task-clock,4098647,100.00,0.038,CPUs utilized\n"
        "3632765,,cycles,4098647,100.00,0.886,GHz\n"
        "<not supported>,,cache-misses,0,100.00,,\n")
SCRIPT = ("              sh    2693/2693    [001]  2109.319414:          1 cycles:P:          8016046c finish_task_switch ([kernel.kallsyms])\n"
          "  fprobe tick  2700/2701    [003]  2109.499247:    1803322 cycles:P:             203f4 [unknown] (/usr/bin/dash)\n"
          "garbage line\n")


def test_parse_stat_rows_are_facts():
    rows = parse_perf_stat(STAT)
    assert rows[0] == {"event": "task-clock", "value": 4098647, "unit": "", "run_time": 4098647, "run_pct": 100.0,
                       "perf_metric": {"value": 0.038, "unit": "CPUs utilized"}}
    assert rows[2]["value"] is None and rows[2]["counted"] == "<not supported>"


def test_parse_sample_line():
    s = parse_perf_sample(SCRIPT.splitlines()[1])
    assert s == {"comm": "fprobe tick", "pid": 2700, "tid": 2701, "cpu": 3, "t_ns": 2109_499247_000, "period": 1803322,
                 "event": "cycles:P", "ip": 0x203F4, "sym": "[unknown]", "dso": "/usr/bin/dash"}
    assert parse_perf_sample("garbage line") is None


def test_stat_runs_perf_with_explicit_events_and_duration(probe, monkeypatch, tmp_path):  # noqa: F811
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FPROBE_FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"perf stat": ""}))
    probe.linux_connect(target="pi", host="h", user="u", sudo=True)
    probe._linux["pi"].caps["tools"]["perf"] = "/usr/bin/perf"
    monkeypatch.setattr(type(probe._linux["pi"].ssh), "run", _stderr_reply(STAT))
    out = probe.linux_perf_stat(target="pi", events=["task-clock", "cycles", "cache-misses"], duration_s=0.5, pid=812)
    assert [r["event"] for r in out["rows"]] == ["task-clock", "cycles", "cache-misses"]
    assert "perf stat --no-scale -x, -e task-clock,cycles,cache-misses -p 812 -- sleep 0.500000" in out["command"]
    assert "LC_ALL=C" in out["command"]
    with pytest.raises(ValueError):
        probe.linux_perf_stat(target="pi", events=["cycles;rm"], duration_s=0.5)
    with pytest.raises(ValueError):
        probe.linux_perf_stat(target="pi", events=["cycles\n"], duration_s=0.5)


def test_record_puts_samples_into_events(probe, monkeypatch):  # noqa: F811
    probe.linux_connect(target="pi", host="h", user="u", sudo=True)
    probe._linux["pi"].caps["tools"]["perf"] = "/usr/bin/perf"
    err = ("[ perf record: Captured and wrote 0.015 MB /tmp/x (2 samples) ]\n"
           "fprobe_perf record=0 script=0 lines=3\n")
    monkeypatch.setattr(type(probe._linux["pi"].ssh), "run", _reply(SCRIPT, err))
    out = probe.linux_perf_record(target="pi", duration_s=0.3, frequency_hz=499, pid=2700, max_samples=1000)
    assert out["samples"] == 2 and out["unparsed"] == 1 and out["perf_reported_samples"] == 2
    assert (out["record_exit_code"], out["script_exit_code"], out["script_lines"]) == (0, 0, 3)
    assert out["unparsed_examples"] == ["garbage line"] and out["evicted"] == 0
    assert "head -n 1000" in out["command"]
    assert "--clockid monotonic_raw" in out["command"] and "-F 499 -p 2700" in out["command"]
    ev = [e for e in probe.linux_events_read(target="pi")["records"] if e["method"] == "perf"]
    assert [(e["probe"], e["target_ns"], e["clock"], e["resolution_ns"]) for e in ev] == [
        ("finish_task_switch", 2109_319414_000, "CLOCK_MONOTONIC_RAW", 1000), ("[unknown]", 2109_499247_000, "CLOCK_MONOTONIC_RAW", 1000)]


def test_perf_needs_the_tool(probe):  # noqa: F811
    probe.linux_connect(target="pi", host="h", user="u", sudo=True)
    probe._linux["pi"].caps["tools"]["perf"] = None
    with pytest.raises(RuntimeError, match="perf"):
        probe.linux_perf_record(target="pi", duration_s=0.3, frequency_hz=499, max_samples=10)


def test_record_reports_a_failed_perf_record(probe, monkeypatch):  # noqa: F811
    probe.linux_connect(target="pi", host="h", user="u", sudo=True)
    probe._linux["pi"].caps["tools"]["perf"] = "/usr/bin/perf"
    monkeypatch.setattr(type(probe._linux["pi"].ssh), "run",
                        _reply("", "Error: No permission\nfprobe_perf record=255 script=1 lines=0\n"))
    out = probe.linux_perf_record(target="pi", duration_s=0.3, frequency_hz=499, max_samples=10)
    assert out["record_exit_code"] == 255 and out["script_exit_code"] == 1 and out["samples"] == 0


def test_parse_sample_with_a_deleted_binary():
    line = "  app  10/11  [000]  5.000001:  7 cycles:P:  1040c tick (/home/u/app (deleted))"
    s = parse_perf_sample(line)
    assert s is not None and s["sym"] == "tick" and s["dso"] == "/home/u/app (deleted)"


def _reply(stdout: str, stderr: str = ""):
    import subprocess

    def run(self, cmd, input=None, timeout=30):
        return subprocess.CompletedProcess(cmd, 0, stdout.encode(), stderr.encode())
    return run


def _stderr_reply(stderr: str):
    return _reply("", stderr)
