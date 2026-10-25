"""Linux 系（probe_linux.py と fprobe/linux/*）の不具合の再現試験（検出専用。製品コードは直していない）。

どれも xfail(strict=True): 直ったら XPASS で失敗するので、marker を外して通常の試験にする。
台帳は docs/reports/bugs/linux.md。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fprobe.linux.agent import Agent
from fprobe.linux.gdb_relay import GdbRelay
from fprobe.linux.perf import parse_perf_sample
from fprobe.linux.ssh import SshTarget
from fprobe.linux.uprobe import NAME, parse_trace_line, site_of
from fprobe.mcp_server import Probe
from fprobe.probe_linux import _Target

DATA = Path(__file__).parent / "data" / "linux"


class StubSsh:
    """命令を記録するだけの ssh の偽物（rc_for: 命令の部分の文字 → 返す終了コード）。"""

    def __init__(self, stdout: bytes = b"", rc_for: dict | None = None):
        self.stdout, self.rc_for, self.cmds, self.gets = stdout, rc_for or {}, [], []

    def run(self, cmd, input=None, timeout=30):
        self.cmds.append(cmd)
        rc = next((v for k, v in self.rc_for.items() if k in cmd), 0)
        return subprocess.CompletedProcess(cmd, rc, self.stdout, b"boom" if rc else b"")

    def get(self, remote, local, timeout=120):
        self.gets.append((remote, Path(local)))
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(DATA / "tick_arm", local)


def _probe(monkeypatch, tmp_path, ssh, caps=None) -> Probe:
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path / "home"))
    p = Probe("unused")
    p._linux["t"] = _Target(ssh, False, caps or {"uprobe_events": True, "tracefs": "/sys/kernel/tracing", "config": {},
                                                  "tools": {"gdb": "/usr/bin/gdb", "perf": "/usr/bin/perf"}})
    return p


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-01: 未定義（インポート）シンボルが offset 0 = ELF ヘッダに解決される")
def test_undefined_symbol_is_not_resolved_to_elf_header():
    # tick_arm の .symtab に UNDEF の nanosleep@GLIBC_2.4（st_value 0）がある。最初の PT_LOAD が vaddr 0 から始まる PIE では 0 が区画に入る
    with pytest.raises(ValueError):
        site_of(DATA / "tick_arm", "/tmp/tick_arm", symbol="nanosleep@GLIBC_2.4")


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-02: プローブ名の正規表現が ^...$ + match で、末尾の改行を通す（シェルの命令が割れる）")
def test_probe_name_rejects_trailing_newline():
    assert NAME.match("abc\n") is None


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-03: perf script の dso の正規表現が貪欲で、シンボルに ' (' を含む C++ の名前を取り違える")
def test_perf_sample_symbol_with_space_paren():
    line = "app 10/10 [000] 1.000001: 1 cycles: 4005d0 std::function<void ()>::operator()() const (/usr/bin/app)"
    s = parse_perf_sample(line)
    assert s is not None and s["dso"] == "/usr/bin/app" and s["sym"] == "std::function<void ()>::operator()() const"


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-04: linux.log.read は新しい行が無いとき next_cursor を None にし、呼び手が位置を失う")
def test_log_read_keeps_cursor_when_nothing_new(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, StubSsh(stdout=b""))
    r = p.linux_log_read(target="t", lines=10, since_cursor="s=abc;i=1")
    assert r["next_cursor"] == "s=abc;i=1"


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-05: linux.gdb.batch が timeout_s の検査をしない（0 は timeout コマンドで「無制限」、gdb がつきっぱなしになる）")
@pytest.mark.parametrize("bad", [0, -1, float("nan")])
def test_gdb_batch_rejects_nonpositive_timeout(monkeypatch, tmp_path, bad):
    p = _probe(monkeypatch, tmp_path, StubSsh())
    with pytest.raises(ValueError):
        p.linux_gdb_batch(target="t", commands=["bt"], timeout_s=bad, pid=1)


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-06: SshTarget の user が - で始まると ssh のオプションとして解釈される（ProxyCommand 等）")
def test_ssh_user_cannot_become_option():
    try:
        argv = SshTarget("host", "-oProxyCommand=calc").argv("true")
    except ValueError:
        return
    dest = next(a for a in argv if a.endswith("@host"))
    assert argv[argv.index(dest) - 1] == "--"


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-07: uprobe_events に書いた後の enable が失敗すると、カーネルに行が残り、プローブ表にも無い")
def test_probe_add_uprobe_failure_does_not_leak_kernel_event(monkeypatch, tmp_path):
    ssh = StubSsh(rc_for={"echo 1 >": 1})
    p = _probe(monkeypatch, tmp_path, ssh)
    with pytest.raises(RuntimeError):
        p.linux_probe_add(target="t", name="tick", binary="/tmp/tick_arm", symbol="tick", method="uprobe")
    cleaned = any("-:fprobe/tick" in c for c in ssh.cmds)
    tracked = "tick" in p._linux["t"].probes
    assert cleaned or tracked


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-08: binary の .. が sysroot の外に出る（lstrip('/') だけで正規化しない）")
def test_probe_add_binary_cannot_escape_sysroot(monkeypatch, tmp_path):
    ssh = StubSsh()
    p = _probe(monkeypatch, tmp_path, ssh)
    try:
        p.linux_probe_add(target="t", name="tick", binary="/../../escape/tick", symbol="tick", method="ptrace", pid=1)
    except Exception:                                   # noqa: BLE001  後ろの失敗は問わない（取り寄せた先だけ見る）
        pass
    sysroot = (tmp_path / "home" / "linux" / "t" / "sysroot").resolve()
    for _, local in ssh.gets:
        assert local.resolve().is_relative_to(sysroot), local


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-09: Agent.start が hello に失敗すると ssh の子プロセスを止めずに残す")
def test_agent_start_failure_does_not_leak_process(monkeypatch):
    procs = []

    class Ssh:
        def open(self, cmd):
            pr = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append(pr)
            return pr

    orig = Agent.request
    monkeypatch.setattr(Agent, "request", lambda self, op, timeout=10, **a: orig(self, op, timeout=0.5, **a))
    agent = Agent(Ssh(), "bin", False, lambda ev: None)
    try:
        with pytest.raises(RuntimeError):
            agent.start()
        end = time.monotonic() + 3
        while procs[0].poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        assert procs[0].poll() is not None
    finally:
        procs[0].kill()
        procs[0].wait()


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-10: gdb.serve の port=0 は OS が選んだ番号を返さず、port 0 と connect 0 を返す")
def test_gdb_relay_port_zero_reports_real_port():
    r = GdbRelay(SshTarget("h", "u"), "true", 0)
    try:
        assert r.status()["port"] != 0
    finally:
        r.stop()


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-11: trace の時刻の小数部が 10 桁以上だと t_ns が float になる（10 ** 負の数）")
def test_trace_line_ns_is_int_for_long_fraction():
    r = parse_trace_line("  app-1  [000] d... 100.1234567891: tick: (0x4005d0)")
    assert r is not None and isinstance(r["t_ns"], int)


@pytest.mark.xfail(strict=True, reason="BUG-LINUX-12: 最後の組の時刻ちょうどの出来事が outside=true になる（bisect_right。flush は <= last で待ちを終える）")
def test_clock_event_at_last_pair_is_not_outside():
    from fprobe.linux.clock import TargetClock
    c = TargetClock()
    for n, h in [(0, 0.0), (1000, 1e-6), (2000, 2e-6)]:
        c.add(n, h, 1e-4)
    assert c.to_host(2000)["outside"] is False
