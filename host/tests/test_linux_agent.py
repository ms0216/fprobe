import json
import time
from pathlib import Path

import pytest

from fprobe.linux.agent import Agent
from fprobe.linux.ssh import SshTarget

FAKE_SSH = Path(__file__).with_name("linux_fake_ssh.py")


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("FPROBE_SSH", str(FAKE_SSH))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))
    events = []
    a = Agent(SshTarget("h", "u"), ".fprobe/fprobe_agent", sudo=False, on_event=events.append)
    a.events = events
    yield a
    a.stop()


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


def test_hello_and_time(agent):
    assert agent.start()["clock"] == "CLOCK_MONOTONIC_RAW"
    assert agent.request("time")["t_ns"] > 0


def test_events_are_delivered(agent):
    agent.start()
    agent.request("trace_open", path="/sys/kernel/tracing/trace_pipe")
    assert _wait(lambda: len(agent.events) == 2)
    assert all(e["ev"] == "trace" for e in agent.events)


def test_bp_event_is_an_event(agent):
    agent.start()
    agent.request("emit_bp", bp=1, t_ns=5)
    assert _wait(lambda: len(agent.events) == 1)
    assert agent.events[0] == {"ev": "bp", "bp": 1, "tid": 42, "t_ns": 5, "regs": {"r0": 7}}


def test_error_reply_raises(agent):
    agent.start()
    with pytest.raises(RuntimeError, match="unknown op nosuch"):
        agent.request("nosuch")


def test_agent_exit_is_reported(agent):
    agent.start()
    with pytest.raises(RuntimeError):
        agent.request("crash", timeout=5)
    assert _wait(lambda: not agent.status()["running"])
    st = agent.status()
    assert st["exit_code"] == 3 and "boom" in st["stderr_tail"]


def test_on_event_error_does_not_kill_reader(monkeypatch):
    monkeypatch.setenv("FPROBE_SSH", str(FAKE_SSH))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))

    def boom(ev):
        raise ValueError("bad handler")
    a = Agent(SshTarget("h", "u"), ".fprobe/fprobe_agent", sudo=False, on_event=boom)
    try:
        a.start()
        a.request("emit_bp", bp=1, t_ns=5)
        a.request("emit_raw", text="[1, 2]")             # オブジェクトでない JSON
        assert a.request("time")["t_ns"] > 0           # 読む側は生きている
        st = a.status()
        assert st["event_errors"] == 1 and "bad handler" in st["last_event_error"]
    finally:
        a.stop()


def test_deploy_reports_ssh_failure(monkeypatch, tmp_path):
    from fprobe.linux.agent import deploy
    fail = tmp_path / "fail_ssh.py"
    fail.write_text("import sys\nsys.stderr.write('ssh: connect to host h port 22: Connection timed out')\nsys.exit(255)\n")
    monkeypatch.setenv("FPROBE_SSH", str(fail))
    with pytest.raises(RuntimeError, match="入れない"):
        deploy(SshTarget("h", "u"))


def test_watchdog_sends_pings(monkeypatch, tmp_path):
    """watchdog_s を渡すと、エージェントに watchdog を設定し、その 3 分の 1 ごとに ping を送る（デーモンが生きている合図）"""
    log = tmp_path / "agent.log"
    monkeypatch.setenv("FPROBE_SSH", str(FAKE_SSH))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))
    monkeypatch.setenv("FPROBE_FAKE_AGENT_LOG", str(log))
    a = Agent(SshTarget("h", "u"), ".fprobe/fprobe_agent", sudo=False, on_event=lambda ev: None)
    try:
        a.start(watchdog_s=0.3)
        time.sleep(0.6)
    finally:
        a.stop()
    sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [x for x in sent if x["op"] == "watchdog"][0]["seconds"] == 0.3
    assert sum(x["op"] == "ping" for x in sent) >= 2


def test_watchdog_refused_by_old_agent_stops_it(monkeypatch):
    """watchdog を知らない古いエージェントなら、起動を失敗にして止める（watchdog なしで残さない）"""
    monkeypatch.setenv("FPROBE_SSH", str(FAKE_SSH))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))
    monkeypatch.setenv("FPROBE_FAKE_AGENT_OLD", "1")
    a = Agent(SshTarget("h", "u"), ".fprobe/fprobe_agent", sudo=False, on_event=lambda ev: None)
    with pytest.raises(RuntimeError, match="watchdog"):
        a.start(watchdog_s=1.0)
    assert _wait(lambda: not a.status()["running"])


def test_watchdog_must_be_finite():
    a = Agent(SshTarget("h", "u"), ".fprobe/fprobe_agent", sudo=False, on_event=lambda ev: None)
    with pytest.raises(ValueError, match="watchdog"):
        a.start(watchdog_s=float("inf"))
