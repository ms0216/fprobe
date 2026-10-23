"""実機: 道 B（リングを JTAG で読む）で、ネットワークが切れている間も出来事を読めることを Pi 2B で確かめる（R26 段階 2）。

FPROBE_PI2_JTAG=1・FPROBE_PI2_HOST（例 <Pi のアドレス>）・FPROBE_PI2_USER・FPROBE_PI2_KEY。Pi の準備は docs/linux.md
（reserved-memory 0x20000000 + 1 MB、isolcpus=3 irqaffinity=0-2、JTAG の配線）。止めるのはコア 3（何の仕事もしていないコア）。
Pi の LAN（eth0）を 15 秒だけ落とす（Pi の上の nohup の台本が自分で戻す）。"""
import os
import time

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_PI2_HOST")
pytestmark = pytest.mark.skipif(not (os.environ.get("FPROBE_PI2_JTAG") and HOST), reason="FPROBE_PI2_JTAG・FPROBE_PI2_HOST が無い")
N = 3000                                    # 10 ms ごと = 30 秒
PHYS, SIZE = 0x20000000, 0x100000
VIRTUAL = 0x80000000 + PHYS                 # Pi 2B（PAGE_OFFSET 0x80000000）の線形の写し
APP = "/tmp/fprobe_tick_thumb"


def test_ring_is_read_while_the_network_is_down():
    p = Probe()
    try:
        p.target_connect(target="bcm2836", frequency_hz=10_000_000, connect_mode="attach", core=3)   # R28: 10 MHz は隣の一定の線が前提
        p.linux_connect(target="pi", host=HOST, user=os.environ.get("FPROBE_PI2_USER", "ms0216"),
                        key=os.environ.get("FPROBE_PI2_KEY"), sudo=True)
        p.linux_agent_deploy(target="pi")
        p.linux_agent_start(target="pi")
        print("ring", p.linux_ring_open(target="pi", phys=PHYS, size=SIZE, cached=True, linger=True))
        ssh = p._linux["pi"].ssh
        app = ssh.open(f"sh -c 'echo $$; exec nohup {APP} {N} >/dev/null 2>&1'")   # SSH が切れても続く（PYNQ の試験と同じ）
        pid = int(app.stdout.readline())
        p.linux_sync(target="pi", count=8)
        p.linux_probe_add(target="pi", name="tick", binary=APP, symbol="tick", method="ptrace", regs=["r0"], pid=pid)
        p.linux_trace_start(target="pi", sync_interval_s=1.0)
        p.linux_ring_watch(target="pi", interval_s=0.5, address=VIRTUAL, access="halt", max_records=1000)
        time.sleep(5)
        ssh.run("sudo nohup sh -c 'sleep 1; ip link set eth0 down; sleep 15; ip link set eth0 up' >/dev/null 2>&1 &")
        time.sleep(4)                                           # LAN が落ちている間
        try:
            down = ssh.run("true", timeout=8).returncode
        except RuntimeError:
            down = "timeout"                                    # SSH が 8 秒で終わらない = LAN が落ちている
        before = p._linux["pi"].ring_stats["records"]
        time.sleep(5)
        during = p._linux["pi"].ring_stats["records"] - before
        print(f"LAN が落ちている間（ssh の結果 {down}）に道 B で読んだ記録 {during} 件")
        end = time.monotonic() + 120                            # LAN が戻ってから、アプリが終わったかを見る（起動した
        while time.monotonic() < end:                           # SSH の接続は LAN が落ちた後に戻らないことがある）
            try:
                if ssh.run(f"kill -0 {pid} 2>/dev/null && echo alive || echo gone", timeout=10).stdout.strip() == b"gone":
                    break
            except RuntimeError:
                pass
            time.sleep(2)
        time.sleep(2)
        p.linux_ring_unwatch(target="pi")
        p.linux_ring_read(target="pi", address=VIRTUAL, access="halt", max_records=N)    # 残り
        p.linux_trace_stop(target="pi")
        stats = p.linux_agent_status(target="pi")["ring_stats"]
        events, since = [], 0
        while True:
            r = p.linux_events_read(target="pi", since=since, limit=10000)
            if not r["records"]:
                break
            events += r["records"]
            since = r["next"]
        hits = [e for e in events if e["probe"] == "tick"]
        via_b = sorted(e["regs"]["r0"] for e in hits if "b" in e["paths"])
        via_a = sorted(e["regs"]["r0"] for e in hits if "a" in e["paths"])
        print("stats", stats)
        print(f"出来事 {len(hits)}、B {len(via_b)}、A {len(via_a)}、止めた平均 {stats['halted_ms_total'] / max(stats['reads'], 1):.1f} ms"
              f"・最大 {stats['halted_ms_max']:.1f} ms・回数 {stats['reads']}")
        assert down != 0                                        # 本当に LAN が落ちていた
        assert during > 0                                       # LAN が落ちている間も道 B で読めた
        assert via_b == list(range(N))
        assert stats["lost"] == 0 and stats["torn"] == 0 and stats["corrupt"] == 0 and stats["error"] is None
    finally:
        try:
            p.close()
        finally:
            r = os.system(f"ssh -o BatchMode=yes -o ConnectTimeout=20 -i {os.environ.get('FPROBE_PI2_KEY')} "
                          f"{os.environ.get('FPROBE_PI2_USER', 'ms0216')}@{HOST} uptime")
            print("pi alive" if r == 0 else "pi NOT reachable")
