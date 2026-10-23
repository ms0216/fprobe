"""Linux のアプリの core dump（R26 段階 4。仕様 §3.3 の linux.core.*）: 設定の事実・core_pattern と上限の設定・PC への回収。
偽の ssh（linux_fake_ssh.py）で試す。"""

import hashlib
import json

import pytest
from test_probe_linux import probe  # noqa: F401  （fixture）

STATUS = ("pattern=/tmp/core.%e.%p\nuses_pid=0\nsuid_dumpable=0\n"
          "Max core file size        0                    unlimited            bytes     \n"
          "cwd=/home/ms0216\nexe=/tmp/fprobe_tick\n")


def test_status_reports_the_settings_as_facts(probe, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"core_pattern": STATUS}))
    probe.linux_connect(target="pi", host="h", user="u")
    st = probe.linux_core_status(target="pi", pid=812)
    assert st == {"pattern": "/tmp/core.%e.%p", "pipe": False, "uses_pid": 0, "suid_dumpable": 0,
                  "process": {"pid": 812, "limit_soft": 0, "limit_hard": None, "cwd": "/home/ms0216",
                              "exe": "/tmp/fprobe_tick"}}


def test_status_without_pid_and_a_pipe_pattern(probe, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps(
        {"core_pattern": "pattern=|/usr/share/apport/apport %p %s\nuses_pid=0\nsuid_dumpable=2\n"}))
    probe.linux_connect(target="pynq", host="h", user="u")
    st = probe.linux_core_status(target="pynq")
    assert st["pipe"] is True and st["suid_dumpable"] == 2 and st["process"] is None


def test_pattern_set_needs_sudo_and_returns_previous_and_now(probe, monkeypatch, tmp_path):  # noqa: F811
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FPROBE_FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"core_pattern": STATUS}))
    probe.linux_connect(target="pi", host="h", user="u")
    with pytest.raises(RuntimeError, match="sudo"):
        probe.linux_core_pattern_set(target="pi", pattern="/tmp/core.%e.%p")
    probe.linux_connect(target="pi", host="h", user="u", sudo=True)
    out = probe.linux_core_pattern_set(target="pi", pattern="/tmp/core.%e.%p")
    assert out["previous"] == "/tmp/core.%e.%p" and out["pattern"] == "/tmp/core.%e.%p"
    assert any(line.startswith("sudo -n sh -c") and "printf %s" in line and "/tmp/core.%e.%p" in line
               and "> /proc/sys/kernel/core_pattern" in line for line in log.read_text().splitlines())
    with pytest.raises(ValueError):
        probe.linux_core_pattern_set(target="pi", pattern="")


def test_limit_set_uses_prlimit(probe, monkeypatch, tmp_path):  # noqa: F811
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FPROBE_FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"core_pattern": STATUS}))
    probe.linux_connect(target="pi", host="h", user="u", sudo=True)
    out = probe.linux_core_limit_set(target="pi", pid=812, soft="unlimited", hard="unlimited")
    assert out["process"]["pid"] == 812
    assert any("prlimit --pid 812 --core=unlimited:unlimited" in line for line in log.read_text().splitlines())
    with pytest.raises(ValueError):
        probe.linux_core_limit_set(target="pi", pid=812, soft="lots", hard="unlimited")
    with pytest.raises(ValueError):
        probe.linux_core_limit_set(target="pi", pid=812, soft=True, hard="unlimited")


def test_fetch_copies_the_file_and_reports_its_hash(probe, monkeypatch, tmp_path):  # noqa: F811
    body = "ELF-core-bytes"
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"cat -- '/tmp/core.tick.812'": body}))
    probe.linux_connect(target="pi", host="h", user="u")
    local = tmp_path / "got" / "core.tick.812"
    out = probe.linux_core_fetch(target="pi", path="/tmp/core.tick.812", local_path=str(local))
    assert local.read_bytes() == body.encode()
    assert out == {"path": "/tmp/core.tick.812", "local_path": str(local), "bytes": len(body),
                   "sha256": hashlib.sha256(body.encode()).hexdigest()}
