"""実機: Linux のターゲット（R26 段階 1、道 A）で ptrace のプローブと、ネットワークの時刻合わせを確かめる。

FPROBE_LINUX_HOST（例 192.168.77.2）・FPROBE_LINUX_USER（既定 xilinx）・FPROBE_LINUX_KEY（秘密鍵のパス）。
題材のアプリをターゲットの gcc で -marm / -mthumb で作り、tick(i) を 10 ms ごとに N 回呼ぶ（起動の 1 秒後から）。"""
import os
import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_LINUX_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="FPROBE_LINUX_HOST が無い")
N = 300
APP = r"""
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
volatile int sink;
__attribute__((noinline)) int tick(int i) { sink += i; return i * 2; }
int main(int argc, char **argv) {
    int n = atoi(argv[1]);
    struct timespec d = {0, 10 * 1000 * 1000};
    sleep(1);                                   /* プローブを付ける間 */
    for (int i = 0; i < n; i++) { tick(i); nanosleep(&d, 0); }
    return 0;
}
"""


@pytest.fixture(scope="module")
def probe():
    p = Probe("unused")
    p.linux_connect(target="t", host=HOST, user=os.environ.get("FPROBE_LINUX_USER", "xilinx"),
                    key=os.environ.get("FPROBE_LINUX_KEY"))
    print("deploy:", p.linux_agent_deploy(target="t"))
    p.linux_agent_start(target="t")
    ssh = p._linux["t"].ssh
    ssh.run("cat > /tmp/fprobe_tick.c", input=APP.encode()).check_returncode()
    for isa in ("arm", "thumb"):
        ssh.run(f"gcc -O1 -march=armv7-a+fp -m{isa} -o /tmp/fprobe_tick_{isa} /tmp/fprobe_tick.c", timeout=60).check_returncode()
    yield p
    p.close()


@pytest.mark.parametrize("isa", ["arm", "thumb"])
def test_ptrace_probe_counts_every_call(probe, isa):
    ssh = probe._linux["t"].ssh
    app = ssh.open(f"sh -c 'echo $$; exec /tmp/fprobe_tick_{isa} {N}'")
    pid = int(app.stdout.readline())
    probe.linux_sync(target="t", count=8)
    added = probe.linux_probe_add(target="t", name=f"tick_{isa}", binary=f"/tmp/fprobe_tick_{isa}", symbol="tick",
                                  method="ptrace", regs=["r0"], pid=pid)
    print(isa, "plan:", added["plan"])
    probe.linux_trace_start(target="t", sync_interval_s=1.0)
    app.wait(timeout=30)
    time.sleep(0.5)
    probe.linux_trace_stop(target="t")
    probe.linux_sync(target="t", count=2)                       # 最後の出来事の後ろにも組を置く
    events, since = [], 0
    while True:
        r = probe.linux_events_read(target="t", since=since, limit=10000)
        if not r["records"]:
            break
        events += r["records"]
        since = r["next"]
    hits = [e for e in events if e["probe"] == f"tick_{isa}"]
    r0 = [e["regs"]["r0"] for e in hits]
    gaps = [b["target_ns"] - a["target_ns"] for a, b in zip(hits, hits[1:])]
    halves = [e["sync"]["half_rtt_ns"] for e in hits if e.get("sync")]
    exits = [e for e in events if e["probe"] == "exit" and e["detail"]["pid"] == pid]
    print(isa, "hits", len(hits), "min gap ms", min(gaps) / 1e6, "half_rtt_ns max", max(halves) if halves else None,
          "outside", sum(e["sync"]["outside"] for e in hits if e.get("sync")))
    assert r0 == list(range(N))
    assert min(gaps) >= 10_000_000
    assert exits and exits[0]["detail"]["code"] == 0             # ptrace でアプリを壊していない
    probe._linux["t"].probes.pop(f"tick_{isa}")                  # プロセスは終わった（外すものが無い）


@pytest.mark.skipif(not os.environ.get("FPROBE_PYNQ_HOST"), reason="FPROBE_PYNQ_HOST（fprobe の機器）が無い")
def test_events_go_into_fprobe_recording(probe, tmp_path, monkeypatch):   # probe: 題材のアプリを作る
    """fprobe の機器（FPROBE_PYNQ_HOST の中継）で記録しながら Linux の出来事を受け、記録の stream に入ることを確かめる。"""
    from fprobe.recording import Recording

    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe(eth=os.environ["FPROBE_PYNQ_HOST"])
    try:
        p.linux_connect(target="t", host=HOST, user=os.environ.get("FPROBE_LINUX_USER", "xilinx"),
                        key=os.environ.get("FPROBE_LINUX_KEY"))
        p.linux_agent_start(target="t")
        rec = p.recording_start(name="linux_hw")
        ssh = p._linux["t"].ssh
        app = ssh.open(f"sh -c 'echo $$; exec /tmp/fprobe_tick_thumb {N}'")
        pid = int(app.stdout.readline())
        p.linux_sync(target="t", count=8)
        p.linux_probe_add(target="t", name="tick", binary="/tmp/fprobe_tick_thumb", symbol="tick", method="ptrace",
                          regs=["r0"], pid=pid)
        p.linux_trace_start(target="t", sync_interval_s=1.0)
        app.wait(timeout=30)
        time.sleep(0.5)
        p.linux_trace_stop(target="t")
        p._linux["t"].probes.pop("tick")
        stopped = p.recording_stop()
        print("recording:", rec, stopped)
        directory = rec["path"]
        events = Recording.open(Path(directory)).events(streams=["t.ptrace"])
        r0 = [e.body["regs"]["r0"] for e in events]
        unc = [e.uncertainty for e in events]
        print("in recording", len(events), "uncertainty ticks max", max(unc), "min", min(unc))
        assert r0 == list(range(N))
        assert all(e.body["sync"]["outside"] is False for e in events)
        ticks = [e.t for e in events]
        assert ticks == sorted(ticks)
    finally:
        p.close()
