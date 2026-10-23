"""実機: core dump の回収（R26 段階 4）を Pi 2B で確かめる。FPROBE_LINUX_HOST・FPROBE_LINUX_USER・FPROBE_LINUX_KEY。
落ちる題材（5 秒待ってから NULL に書く。その間に上限を変える）を起動し、上限を unlimited に、core_pattern を /tmp に変え、落ちた後の core を
PC に写し、ターゲットの gdb で bt に落ちた関数が出ることを見る。core_pattern は最後に元へ戻す。"""
import os
import time

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_LINUX_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="FPROBE_LINUX_HOST が無い")
APP = "/tmp/fprobe_crash"
APP_C = r"""
#include <unistd.h>
__attribute__((noinline)) void fprobe_crash_here(volatile int *p) { *p = 42; }
int main(void) { sleep(5); fprobe_crash_here((volatile int *)0); return 0; }
"""


def test_core_dump_is_collected_and_readable(tmp_path):
    p = Probe()
    p.linux_connect(target="t", host=HOST, user=os.environ.get("FPROBE_LINUX_USER", "ms0216"),
                    key=os.environ.get("FPROBE_LINUX_KEY"), sudo=True)
    ssh = p._linux["t"].ssh
    previous = None
    try:
        ssh.run("cat > /tmp/fprobe_crash.c", input=APP_C.encode()).check_returncode()
        ssh.run(f"gcc -O1 -g -o {APP} /tmp/fprobe_crash.c", timeout=60).check_returncode()
        before = p.linux_core_status(target="t")
        print("before", before)
        previous = p.linux_core_pattern_set(target="t", pattern="/tmp/fprobe-core.%e.%p")["previous"]
        app = ssh.open(f"sh -c 'echo $$; exec {APP}'")
        pid = int(app.stdout.readline())
        st = p.linux_core_limit_set(target="t", pid=pid, soft="unlimited", hard="unlimited")
        print("limit", st)
        assert st["process"]["limit_soft"] is None and st["pattern"] == "/tmp/fprobe-core.%e.%p"
        app.wait(timeout=30)
        print("exit", app.returncode)
        core = f"/tmp/fprobe-core.fprobe_crash.{pid}"
        for _ in range(20):
            if ssh.run(f"test -e {core}").returncode == 0:
                break
            time.sleep(0.5)
        got = p.linux_core_fetch(target="t", path=core, local_path=str(tmp_path / "core"))
        print("fetch", got)
        assert got["bytes"] > 0 and (tmp_path / "core").read_bytes()[:4] == b"\x7fELF"
        r = p.linux_gdb_batch(target="t", commands=["bt"], core=core, binary=APP, timeout_s=60)
        print(r["stdout"][-600:])
        assert "fprobe_crash_here" in r["stdout"]
        ssh.run(f"sudo -n rm -f {core}")
    finally:
        if previous is not None:
            print("restored", p.linux_core_pattern_set(target="t", pattern=previous))
        p.close()
