from pathlib import Path

from fprobe.linux.ssh import SshTarget

ECHO = Path(__file__).with_name("linux_fake_ssh_echo.py")


def test_argv_has_batchmode_and_key(monkeypatch):
    monkeypatch.delenv("FPROBE_SSH", raising=False)
    assert SshTarget("h", "u", 2222, "k").argv("ls") == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", "2222", "-i", "k", "u@h", "ls"]


def test_run_uses_env_ssh(monkeypatch):
    monkeypatch.setenv("FPROBE_SSH", str(ECHO))
    assert SshTarget("h", "u").run("echo x").stdout == b"echo x"


def test_timeout_is_runtime_error(monkeypatch, tmp_path):
    slow = tmp_path / "slow_ssh.py"
    slow.write_text("import time\ntime.sleep(5)\n")
    monkeypatch.setenv("FPROBE_SSH", str(slow))
    import pytest
    with pytest.raises(RuntimeError, match="秒"):
        SshTarget("h", "u").run("x", timeout=0.5)
