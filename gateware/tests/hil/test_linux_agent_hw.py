"""実機: エージェント（C）の ptrace の扱いの難しい場面（R26 段階 1 の最終レビュー C1・C2・I1・I2）。

アプリが動いている間にプローブを外す・エージェントを止める・複数のスレッドが同時に当たる・2 つ目のプローブを足す・fork する。
どれも、エージェントが固まらず、アプリが終了コード 0 で終わることを確かめる。
FPROBE_LINUX_HOST・FPROBE_LINUX_USER（既定 xilinx）・FPROBE_LINUX_KEY。"""
import os
import time
from pathlib import Path

import pytest

from fprobe.linux.agent import BINARY, Agent, deploy
from fprobe.linux.ptrace_plan import plan_breakpoint
from fprobe.linux.ssh import SshTarget
from fprobe.linux.uprobe import site_of

HOST = os.environ.get("FPROBE_LINUX_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="FPROBE_LINUX_HOST が無い")
APP = r"""
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
volatile int sink;
__attribute__((noinline)) int tick(int i) { sink += i; return i * 2; }
__attribute__((noinline)) int tock(int i) { sink -= i; return i; }
static void nap(long ms) { struct timespec d = {ms / 1000, (ms % 1000) * 1000000L}; nanosleep(&d, 0); }
static void *worker(void *arg) { for (int i = 0; i < 100; i++) { tick(i); tock(i); nap(1); } return arg; }
int main(int argc, char **argv) {
    sleep(1);                                         /* プローブを付ける間 */
    if (!strcmp(argv[1], "loop")) { for (int i = 0; i < atoi(argv[2]); i++) { tick(i); tock(i); nap(10); } return 0; }
    if (!strcmp(argv[1], "threads")) {
        pthread_t t[4];
        for (int i = 0; i < 4; i++) pthread_create(&t[i], 0, worker, 0);
        for (int i = 0; i < 4; i++) pthread_join(t[i], 0);
        return 0;
    }
    if (!strcmp(argv[1], "fork")) {                   /* 子も tick を呼ぶ（子には BKPT が写る） */
        for (int i = 0; i < 10; i++) { tick(i); nap(10); }
        pid_t c = fork();
        if (c == 0) { for (int i = 0; i < 10; i++) { tick(i); nap(10); } _exit(0); }
        int st; waitpid(c, &st, 0);
        for (int i = 10; i < 20; i++) { tick(i); nap(10); }
        return WIFEXITED(st) && WEXITSTATUS(st) == 0 ? 0 : 7;
    }
    return 2;
}
"""
ELF = Path(__file__).parent / "_linux_agent_app"


@pytest.fixture(scope="module")
def ssh():
    s = SshTarget(HOST, os.environ.get("FPROBE_LINUX_USER", "xilinx"), key=os.environ.get("FPROBE_LINUX_KEY"))
    deploy(s)
    s.run("cat > /tmp/fprobe_agent_app.c", input=APP.encode()).check_returncode()
    s.run("gcc -O1 -march=armv7-a+fp -mthumb -pthread -o /tmp/fprobe_agent_app /tmp/fprobe_agent_app.c", timeout=60).check_returncode()
    s.get("/tmp/fprobe_agent_app", ELF)
    yield s
    ELF.unlink(missing_ok=True)


class Run:
    def __init__(self, ssh, mode, arg=""):
        self.events = []
        self.agent = Agent(ssh, BINARY, False, self.events.append)
        self.agent.start()
        self.app = ssh.open(f"sh -c 'echo $$; exec /tmp/fprobe_agent_app {mode} {arg}; '")
        self.pid = int(self.app.stdout.readline())

    def add(self, bp, symbol):
        plan = plan_breakpoint(ELF, site_of(ELF, "/tmp/fprobe_agent_app", symbol=symbol))
        return self.agent.request("bp_add", bp=bp, pid=self.pid, path=plan["path"], offset=plan["offset"],
                                  thumb=plan["thumb"], length=plan["length"], regs=["r0"])

    def hits(self, bp):
        return [e for e in self.events if e.get("ev") == "bp" and e["bp"] == bp]

    def exit_code(self, timeout=30):
        self.app.wait(timeout=timeout)
        time.sleep(0.3)
        ex = [e for e in self.events if e.get("ev") == "exit" and e["pid"] == self.pid]
        return ex[0]["code"] if ex else None


def test_remove_while_running_does_not_hang(ssh):
    r = Run(ssh, "loop", 300)
    r.add(1, "tick")
    time.sleep(2.0)
    t0 = time.monotonic()
    removed = r.agent.request("bp_remove", bp=1, timeout=5)
    assert time.monotonic() - t0 < 5 and removed["hits"] > 0
    n = len(r.hits(1))
    r.app.wait(timeout=30)                            # 外した後はアプリが自分で走り切る（ptrace も外れている）
    assert r.app.returncode == 0
    assert len(r.hits(1)) == n
    assert r.agent.status()["running"]
    r.agent.stop()


def test_stop_agent_while_running_releases_app(ssh):
    r = Run(ssh, "loop", 300)
    r.add(1, "tick")
    time.sleep(1.5)
    r.agent.stop()
    assert r.agent.status()["exit_code"] == 0
    r.app.wait(timeout=30)
    assert r.app.returncode == 0


def test_threads_hitting_together(ssh):
    r = Run(ssh, "threads")
    r.add(1, "tick")
    assert r.exit_code() == 0
    assert sorted(e["regs"]["r0"] for e in r.hits(1)) == sorted(list(range(100)) * 4)
    r.agent.stop()


def test_second_probe_while_hitting(ssh):
    r = Run(ssh, "threads")
    r.add(1, "tick")
    time.sleep(1.1)                                   # スレッドが tick に当たり続けている間に足す
    r.add(2, "tock")
    assert r.exit_code() == 0
    assert len(r.hits(1)) == 400 and 0 < len(r.hits(2)) <= 400
    r.agent.stop()


def test_fork_child_is_not_killed(ssh):
    r = Run(ssh, "fork")
    r.add(1, "tick")
    assert r.exit_code() == 0                         # 子が SIGTRAP で死ねば 7
    assert [e["regs"]["r0"] for e in r.hits(1)] == list(range(20))
    assert any(e.get("ev") == "fork" for e in r.events)
    r.agent.stop()
