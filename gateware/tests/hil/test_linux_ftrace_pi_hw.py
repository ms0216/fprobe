"""実機: カーネルの出来事（ftrace の sched_switch）とアプリの関数の出来事（ptrace）が同じ時計で並ぶこと、journal が読めることを
Pi 2B で確かめる（R26 段階 4）。FPROBE_LINUX_HOST・FPROBE_LINUX_USER・FPROBE_LINUX_KEY。エージェントは sudo（trace_pipe）。"""
import os
import time

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_LINUX_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="FPROBE_LINUX_HOST が無い")
APP = "/tmp/fprobe_tick_thumb"
N = 300


def test_sched_switch_and_app_events_share_the_clock():
    p = Probe()
    try:
        caps = p.linux_connect(target="t", host=HOST, user=os.environ.get("FPROBE_LINUX_USER", "ms0216"),
                               key=os.environ.get("FPROBE_LINUX_KEY"), sudo=True)
        print("tracefs", caps["tracefs"], "trace_clocks", caps["trace_clocks"])
        p.linux_agent_deploy(target="t")
        p.linux_agent_start(target="t")
        ssh = p._linux["t"].ssh
        app = ssh.open(f"sh -c 'echo $$; exec {APP} {N}'")
        pid = int(app.stdout.readline())
        p.linux_ftrace_enable(target="t", events=["sched/sched_switch"])
        p.linux_probe_add(target="t", name="tick", binary=APP, symbol="tick", method="ptrace", regs=["r0"], pid=pid)
        p.linux_sync(target="t", count=4)
        print("trace", p.linux_trace_start(target="t", sync_interval_s=1.0))
        app.wait(timeout=30)
        time.sleep(0.5)
        p.linux_trace_stop(target="t")
        p.linux_ftrace_disable(target="t", events=["sched/sched_switch"])
        events, since = [], 0
        while True:
            r = p.linux_events_read(target="t", since=since, limit=10000)
            if not r["records"]:
                break
            events += r["records"]
            since = r["next"]
        ticks = [e for e in events if e["probe"] == "tick"]
        into_app = [e for e in events if e["method"] == "ftrace" and e["args"].get("next_pid") == str(pid)]
        print(f"出来事 {len(events)}、tick {len(ticks)}、アプリへの切り替え {len(into_app)}、時計 {ticks[0]['clock']} / {into_app[0]['clock']}")
        r0 = [e["regs"]["r0"] for e in ticks]
        assert r0 == list(range(r0[0], N)) and into_app                  # 付けた後の呼び出しは抜けなく（付ける前の分は無い）
        assert ticks[0]["clock"] == "CLOCK_MONOTONIC_RAW" and into_app[0]["clock"] == "mono_raw"
        # アプリは nanosleep から起き（sched_switch でアプリへ）、それから tick を呼ぶ: 同じ時計なら、ftrace を受け始めた後の
        # 各 tick の直前（10 ms の周期より短い間）に、アプリへの切り替えがある
        import bisect
        sw = sorted(e["target_ns"] for e in into_app)
        gaps = []
        for e in ticks:
            k = bisect.bisect_right(sw, e["target_ns"])
            if k and e["target_ns"] > sw[0]:
                gaps.append(e["target_ns"] - sw[k - 1])
        gaps.sort()
        print(f"tick の直前のアプリへの切り替えまで: 件数 {len(gaps)}、中央 {gaps[len(gaps) // 2] / 1e3:.0f} us、最大 {gaps[-1] / 1e3:.0f} us")
        assert gaps and gaps[-1] < 10_000_000
        assert all("host_s" in e for e in ticks + into_app)               # どちらも PC の時計に換算できた
        log = p.linux_log_read(target="t", lines=5)
        print("journal", log["exit_code"], len(log["entries"]), log["next_cursor"] is not None)
        assert log["exit_code"] == 0 and log["entries"] and log["next_cursor"]
    finally:
        p.close()
