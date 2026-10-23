"""実機: 道 B の止めずに読む口（memap、xsdb で PYNQ の USB の JTAG から AHB-AP）で、ネットワークが切れている間も出来事を読めることを
PYNQ-Z2 で確かめる（R26 段階 2、案 3）。CPU は止めない。

FPROBE_PYNQ_HOST（192.168.77.2）・FPROBE_PYNQ_KEY・FPROBE_PYNQ_SUDO_PASSWORD（sudo のパスワードを入れた環境変数）・
FPROBE_XSDB（xsdb.bat の場所）。リングは物理番地 0x1DF00000 の 1 MB（mem=479M で Linux から外した、
深いバッファ 0x1E000000〜 のすぐ下。リング専用）。PYNQ の LAN を 15 秒だけ落とす（PYNQ の上の台本が自分で戻す）。"""
import os
import time

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_PYNQ_HOST")
XSDB = os.environ.get("FPROBE_XSDB")
pytestmark = pytest.mark.skipif(not (HOST and XSDB), reason="FPROBE_PYNQ_HOST・FPROBE_XSDB が無い")
N = 3000
PHYS, SIZE = 0x1DF00000, 0x100000
APP_C = r"""
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
volatile int sink;
__attribute__((noinline)) int tick(int i) { sink += i; return i * 2; }
int main(int argc, char **argv) {
    int n = atoi(argv[1]);
    struct timespec d = {0, 10 * 1000 * 1000};
    sleep(1);
    for (int i = 0; i < n; i++) { tick(i); nanosleep(&d, 0); }
    return 0;
}
"""
APP = "/tmp/fprobe_tick_thumb"


def test_ring_is_read_without_halting_while_the_network_is_down():
    p = Probe()
    try:
        p.linux_connect(target="z", host=HOST, user="xilinx", key=os.environ.get("FPROBE_PYNQ_KEY"), sudo=True,
                        sudo_password_env="FPROBE_PYNQ_SUDO_PASSWORD")
        ssh = p._linux["z"].ssh
        ssh.run("cat > /tmp/fprobe_tick.c", input=APP_C.encode()).check_returncode()
        ssh.run(f"gcc -O1 -march=armv7-a+fp -mthumb -o {APP} /tmp/fprobe_tick.c", timeout=60).check_returncode()
        p.linux_agent_deploy(target="z")
        p.linux_agent_start(target="z")
        print("ring", p.linux_ring_open(target="z", phys=PHYS, size=SIZE, cached=False, linger=True))
        print("memap", p.linux_memap_connect(target="z", xsdb=XSDB, cable_serial="1234-tulA", chain_idcode=0x23727093, timeout_s=30))
        app = ssh.open(f"sh -c 'echo $$; exec nohup {APP} {N} >/dev/null 2>&1'")   # SSH が切れても続く
        pid = int(app.stdout.readline())
        p.linux_sync(target="z", count=8)
        p.linux_probe_add(target="z", name="tick", binary=APP, symbol="tick", method="ptrace", regs=["r0"], pid=pid)
        p.linux_trace_start(target="z", sync_interval_s=1.0)
        p.linux_ring_watch(target="z", interval_s=0.5, address=PHYS, access="memap", max_records=1000)
        time.sleep(5)
        p._sh(p._linux["z"], "nohup sh -c 'sleep 1; ip link set eth0 down; sleep 15; ip link set eth0 up' >/dev/null 2>&1 &")
        time.sleep(4)
        try:
            down = ssh.run("true", timeout=8).returncode
        except RuntimeError:
            down = "timeout"
        before = p._linux["z"].ring_stats["records"]
        time.sleep(5)
        during = p._linux["z"].ring_stats["records"] - before
        print(f"LAN が落ちている間（ssh の結果 {down}）に道 B で読んだ記録 {during} 件")
        end = time.monotonic() + 120                             # LAN が戻ってから、アプリが終わったかを見る
        while time.monotonic() < end:
            try:
                if ssh.run(f"kill -0 {pid} 2>/dev/null && echo alive || echo gone", timeout=10).stdout.strip() == b"gone":
                    break
            except RuntimeError:
                pass
            time.sleep(2)
        time.sleep(2)
        p.linux_ring_unwatch(target="z")
        p.linux_ring_read(target="z", address=PHYS, access="memap", max_records=N)
        stats = p.linux_agent_status(target="z")["ring_stats"]
        events, since = [], 0
        while True:
            r = p.linux_events_read(target="z", since=since, limit=10000)
            if not r["records"]:
                break
            events += r["records"]
            since = r["next"]
        via_b = sorted(e["regs"]["r0"] for e in events if e["probe"] == "tick" and "b" in e["paths"])
        print("stats", stats)
        assert down != 0
        assert during > 0
        assert via_b == list(range(N))
        assert stats["halted_ms_total"] == 0 and stats["lost"] == 0 and stats["torn"] == 0 and stats["corrupt"] == 0
    finally:
        p.close()
