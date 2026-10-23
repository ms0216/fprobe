"""実機: Raspberry Pi 2B（BCM2836、Cortex-A7 × 4）を Primer の JTAG で止めて読む（U32 の実機の確かめ・R26 段階 2 の土台）。

FPROBE_PI2_JTAG=1 で走る（配線: docs/linux.md の Pi 2B の節）。FPROBE_PI2_SSH（例 ms0216@<Pi のアドレス>）と FPROBE_PI2_KEY が
あれば、最後に SSH で Pi が生きていることを確かめる。止めるのはコア 3 だけで（割り込みの行き先でない）、どの経路でも最後に再開する。"""
import os
import subprocess
import time

import pytest

from fprobe.mcp_server import Probe

pytestmark = pytest.mark.skipif(not os.environ.get("FPROBE_PI2_JTAG"), reason="FPROBE_PI2_JTAG が無い")
CORE = 3                                # 割り込みの行き先でないコア（isolcpus=3・irqaffinity=0-2）。コア 1 を止めると SD・LAN のドライバが止まりうる
LINEAR = 0x80008000                     # カーネルの線形の写し（PAGE_OFFSET 0x80000000 + 物理 0x8000。カーネルの頭）


def _alive() -> None:
    who, key = os.environ.get("FPROBE_PI2_SSH"), os.environ.get("FPROBE_PI2_KEY")
    if not who:
        return
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *(["-i", key] if key else []), who, "uptime"]
    r = subprocess.run(args, capture_output=True, timeout=30)
    print("pi:", r.stdout.decode().strip())
    assert r.returncode == 0


@pytest.fixture
def pi():
    p = Probe()
    p.target_connect(target="bcm2836", frequency_hz=10_000_000, connect_mode="attach", core=CORE)   # 信号の線の隣に一定の線（RTCK・GND）が要る（docs/linux.md の R28）
    try:
        yield p
    finally:
        try:
            if p._debug.target.core.state() == "halted":
                p.target_resume()
        finally:
            p.close()


def test_fast_read_matches_slow_read_and_is_short(pi):
    t = pi._debug.target
    t0 = time.perf_counter()
    pi.target_halt()
    try:
        t1 = time.perf_counter()
        fast = t.read_memory_block32(LINEAR, 1024)
        t2 = time.perf_counter()
        slow = t.core.read_memory(LINEAR, 64)
        t3 = time.perf_counter()
    finally:
        pi.target_resume()
    t4 = time.perf_counter()
    print(f"速い読み 1024 語 {(t2 - t1) * 1e3:.1f} ms（{4096 / (t2 - t1) / 1e3:.1f} kB/s）、"
          f"遅い読み 64 語 {(t3 - t2) * 1e3:.1f} ms（{256 / (t3 - t2) / 1e3:.1f} kB/s）、止めていた時間 {(t4 - t0) * 1e3:.1f} ms")
    assert fast[:64] == slow
    _alive()
