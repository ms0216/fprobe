"""実機: perf（R26 段階 4）を Pi 2B で確かめる。FPROBE_LINUX_HOST・FPROBE_LINUX_USER・FPROBE_LINUX_KEY。Pi に linux-perf を
入れてある（2026-10-23）。回り続ける題材の pid に perf stat と perf record をかけ、標本が出来事として時刻の換算つきで入る。"""
import os

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_LINUX_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="FPROBE_LINUX_HOST が無い")


def test_perf_stat_and_record_on_a_busy_process():
    p = Probe()
    p.linux_connect(target="t", host=HOST, user=os.environ.get("FPROBE_LINUX_USER", "ms0216"),
                    key=os.environ.get("FPROBE_LINUX_KEY"), sudo=True)
    ssh = p._linux["t"].ssh
    busy = ssh.open("sh -c 'echo $$; exec sh -c \"while :; do :; done\"'")
    pid = int(busy.stdout.readline())
    try:
        p.linux_agent_start(target="t")
        p.linux_sync(target="t", count=8)
        st = p.linux_perf_stat(target="t", events=["task-clock", "cycles", "instructions"], duration_s=0.5, pid=pid)
        print("stat", st["rows"], st["command"])
        assert st["exit_code"] == 0 and [r["event"] for r in st["rows"]][:1] == ["task-clock"]
        assert all(r["value"] is not None and r["value"] > 0 for r in st["rows"])
        rec = p.linux_perf_record(target="t", duration_s=0.5, frequency_hz=499, max_samples=10000, pid=pid)
        p.linux_sync(target="t", count=8)
        print("record", {k: rec[k] for k in ("samples", "unparsed", "perf_reported_samples", "record_exit_code", "script_exit_code", "script_lines", "evicted")})
        assert rec["record_exit_code"] == 0 and rec["script_exit_code"] == 0
        assert rec["samples"] > 50 and rec["samples"] == rec["perf_reported_samples"]
        ev = [e for e in p.linux_events_read(target="t", limit=10000)["records"] if e["method"] == "perf"]
        assert len(ev) == rec["samples"] and all(e["pid"] == pid for e in ev)
        assert all("host_s" in e for e in ev)                  # 時刻合わせの組で換算できた（同じ時計）
    finally:
        ssh.run(f"kill {pid}")
        p.close()
