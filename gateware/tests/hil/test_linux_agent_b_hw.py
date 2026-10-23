"""実機: 道 A（SSH の標準出力）が詰まっても・切れても、エージェントが道 B（リング）に書き続けること（R26 段階 2、PYNQ の試験で見つけた）。

FPROBE_LINUX_HOST・FPROBE_LINUX_USER・FPROBE_LINUX_KEY（sudo がパスワード無しで使えるターゲット。Pi 2B）、FPROBE_RING_PHYS
（リングの物理番地。Pi 2B は 0x20000000）。リングは Pi の /dev/mem から dd で読んで確かめる（JTAG は使わない）。"""
import os
import time
from pathlib import Path

import pytest

from fprobe.linux.agent import BINARY, deploy
from fprobe.linux.ptrace_plan import plan_breakpoint
from fprobe.linux.ring import HEADER, RECORD, RingReader, parse_header
from fprobe.linux.ssh import SshTarget
from fprobe.linux.uprobe import site_of

HOST = os.environ.get("FPROBE_LINUX_HOST")
PHYS = int(os.environ.get("FPROBE_RING_PHYS", "0"), 0)
pytestmark = pytest.mark.skipif(not (HOST and PHYS), reason="FPROBE_LINUX_HOST・FPROBE_RING_PHYS が無い")
APP = "/tmp/fprobe_tick_thumb"
N = 300
ELF = Path(__file__).parent / "_agent_b_app"


@pytest.fixture(scope="module")
def ssh():
    s = SshTarget(HOST, os.environ.get("FPROBE_LINUX_USER", "ms0216"), key=os.environ.get("FPROBE_LINUX_KEY"))
    deploy(s)
    s.get(APP, ELF)
    yield s
    ELF.unlink(missing_ok=True)


def _ring_seqs(ssh) -> list[int]:
    raw = ssh.run(f"sudo -n dd if=/dev/mem bs=4096 skip=$(({PHYS}/4096)) count=16 status=none", timeout=30).stdout
    h = parse_header(raw)
    out = RingReader(h["capacity"], h["first"]).take(h["head"], lambda f, n: raw[HEADER + f * RECORD: HEADER + (f + n) * RECORD])
    assert out["torn"] == [] and out["corrupt"] == []
    return [r["regs"][0] for r in out["records"]]


def _start(ssh, linger: int):
    """エージェントを sudo で起動し、リングを開いて bp を付ける。標準出力は読まない（道 A が詰まった状態）"""
    plan = plan_breakpoint(ELF, site_of(ELF, APP, symbol="tick"))
    agent = ssh.open(f"sudo -n {BINARY}")
    app = ssh.open(f"sh -c 'echo $$; exec {APP} {N}'")
    pid = int(app.stdout.readline())
    req = (f'{{"id":1,"op":"ring_open","phys":{PHYS},"size":1048576,"cached":true,"linger":{linger}}}\n'
           f'{{"id":2,"op":"bp_add","bp":1,"pid":{pid},"path":"{APP}","offset":{plan["offset"]},'
           f'"thumb":{str(plan["thumb"]).lower()},"length":{plan["length"]},"regs":["r0"]}}\n')
    agent.stdin.write(req.encode())
    agent.stdin.flush()
    return agent, app


def test_ring_keeps_going_when_stdout_is_not_read(ssh):
    agent, app = _start(ssh, linger=0)
    app.wait(timeout=60)                                     # 標準出力を読まないまま、アプリが最後まで走る
    seqs = _ring_seqs(ssh)
    agent.stdin.close()
    out = agent.stdout.read().decode(errors="replace")       # 詰まっていた行（落とした数の知らせを含みうる）
    agent.wait(timeout=30)
    print("ring", len(seqs), "stdout lines", out.count("\n"), "a_dropped" in out)
    assert app.returncode == 0 and seqs[-1] == N - 1 and seqs == list(range(seqs[0], N))


def test_ring_keeps_going_after_the_session_is_cut(ssh):
    agent, app = _start(ssh, linger=1)
    time.sleep(2.0)
    agent.kill()                                             # SSH が切れた（ターゲットのエージェントは標準入出力を失う）
    app.wait(timeout=60)
    seqs = _ring_seqs(ssh)
    print("ring", len(seqs), "last", seqs[-1] if seqs else None)
    assert app.returncode == 0 and seqs[-1] == N - 1 and seqs == list(range(seqs[0], N))
    left = ssh.run(f"pgrep -f '^{BINARY}' || true").stdout.decode().strip()
    assert left == ""                                        # アプリが終わったらエージェントも終わる


def _start_with_watchdog(ssh, linger: int, seconds: float):
    agent, app = _start(ssh, linger=linger)
    agent.stdin.write(f'{{"id":3,"op":"watchdog","seconds":{seconds}}}\n'.encode())
    agent.stdin.flush()
    return agent, app


def _agents_left(ssh) -> str:
    return ssh.run(f"pgrep -f '^{BINARY}' || true").stdout.decode().strip()


def test_watchdog_without_linger_releases_the_app(ssh):
    """ping が watchdog の時間来なければ、デーモンがいなくなったとみなし、bp を戻して終わる（アプリは止まらずに走り切る）"""
    agent, app = _start_with_watchdog(ssh, linger=0, seconds=2.0)
    time.sleep(4.0)                                          # ping を送らない（SSH の接続は残ったまま = 古い接続）
    assert _agents_left(ssh) == ""
    app.wait(timeout=60)
    assert app.returncode == 0
    seqs = _ring_seqs(ssh)
    assert seqs and seqs[-1] < N - 1                         # 終わった後の呼び出しはリングに無い（bp を外した）
    agent.kill()


def test_watchdog_with_linger_keeps_writing_until_the_app_ends(ssh):
    agent, app = _start_with_watchdog(ssh, linger=1, seconds=2.0)
    app.wait(timeout=60)
    seqs = _ring_seqs(ssh)
    assert app.returncode == 0 and seqs == list(range(seqs[0], N))
    time.sleep(1.0)
    assert _agents_left(ssh) == ""                           # アプリが終わったら、古い接続のままでも終わる
    agent.kill()
