"""実機: Zynq DUT の仕込んだ不具合を、fprobe の事実（ロジアナの変化の数）だけから突き止める（検証 5）。

FPROBE_ZYNQ_DUT=1 で走る。前提: PYNQ に zynq_dut の回路が載っていて（build zynq_dut → program --shifter）、PMOD JB の 1〜4・7〜8 番が
Primer の J14（T8 P8 R8 T7 T6 P6）につながり、Primer が fprobe_la、デーモン（7820）が動いている。操作はデーモン経由。
dut_app は PYNQ の ~/dut にビルド済み（host/pynq/dut）。FPROBE_ZYNQ_SSH / FPROBE_ZYNQ_KEY / FPROBE_ZYNQ_SUDO_PASSWORD で入る。
dbg の ch: 10 = 生成の拍、11 = FIFO 満杯、12 = DATA の読みの拍、13 = 溢れの拍、14 = 不具合で捨てた拍、15 = 生成が有効。
（2026-10-25 の実機で、JB の 3 番と 4 番の線が J14 の R8 と T7 に交差しているのを見つけた。dbg[2]（溢れ）が ch13、dbg[3]（読み）が ch12 に来る。
配線を直したら READ と OVERFLOW を入れ替える）
試験は変化の数・間隔という事実を比べるだけで、判定の基準は fprobe にも入れない。"""
import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FPROBE_ZYNQ_DUT"), reason="FPROBE_ZYNQ_DUT が無い")
TICK, FULL, READ, OVERFLOW, DROP, ENABLED = 10, 11, 12, 13, 14, 15
PERIOD = 100_000                      # 100 MHz で 1 kHz


def _ssh(command: str, timeout: float = 60) -> str:
    who = os.environ.get("FPROBE_ZYNQ_SSH", "xilinx@192.168.77.2")
    key = os.environ.get("FPROBE_ZYNQ_KEY")
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *(["-i", key] if key else []), who, command]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _sudo(command: str, timeout: float = 60) -> str:
    password = os.environ.get("FPROBE_ZYNQ_SUDO_PASSWORD", "xilinx")
    return _ssh(f"echo {password} | sudo -S -p '' {command}", timeout)


@pytest.fixture(scope="module")
def daemon():
    from fprobe.api.client import DaemonClient
    url = os.environ.get("FPROBE_DAEMON_URL", "ws://127.0.0.1:7820/api")
    with DaemonClient(url, "claude", "ai", token=os.environ.get("FPROBE_DAEMON_TOKEN"), source="hil-zynq-dut") as c:
        yield c


def _run(daemon, args: str, seconds: int):
    """dut_app を走らせながら深いバッファで捕捉し、(アプリの出力の数値, 各 ch の変化の数) を返す。"""
    _sudo("pkill -x dut_app || true")
    _sudo(f"sh -c 'cd /home/xilinx/dut && nohup ./dut_app {args} --seconds {seconds} > /home/xilinx/dut/out.txt 2>&1 &'")
    cap = daemon.call("la.deep.capture", rate_hz=100_000_000, rle=True, rise=1 << TICK, before_beats=16,
                      after_beats=2000, timeout_s=10)
    import time
    time.sleep(seconds + 1)
    out = _ssh("cat /home/xilinx/dut/out.txt")
    numbers = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", out)}
    edges = {int(ch): v["edges"] for ch, v in cap["channels"].items()}
    return numbers, edges, cap


def _pulse_starts(cap, ch):
    """ch の脈の立ち上がりの時刻（ns）。first_edges_ns は先頭の変化だけ（偶数番目が立ち上がり）。"""
    return cap["channels"][str(ch)]["first_edges_ns"][::2]


def test_without_a_fault_nothing_is_dropped_or_overflowing(daemon):
    app, edges, _ = _run(daemon, f"--period {PERIOD}", 4)
    assert edges.get(DROP, 0) == 0 and edges.get(OVERFLOW, 0) == 0
    assert app["overflows"] == 0 and abs(app["generated"] - app["consumed"]) <= 1


def test_every_seventh_sample_dropped_is_visible_in_the_la_intervals(daemon):
    app, edges, cap = _run(daemon, f"--period {PERIOD} --fault 7", 4)
    ticks, drops = _pulse_starts(cap, TICK), _pulse_starts(cap, DROP)
    assert len(drops) >= 3
    tick_interval = (ticks[-1] - ticks[0]) / (len(ticks) - 1)          # 生成の間隔（約 1 ms）
    drop_intervals = [b - a for a, b in zip(drops, drops[1:])]
    for interval in drop_intervals:                                    # 捨てた拍の間隔は生成の間隔の 7 倍
        assert abs(interval / tick_interval - 7) < 0.05, (interval, tick_interval)
    assert app["generated"] - app["consumed"] > 0


def test_a_slow_reader_overflows_and_reads_are_spaced_by_its_delay(daemon):
    app, edges, cap = _run(daemon, f"--period {PERIOD} --delay-us 20000", 4)
    assert app["overflows"] > 0 and edges[OVERFLOW] // 2 > 0
    reads = _pulse_starts(cap, READ)
    gaps = [b - a for a, b in zip(reads, reads[1:])]
    assert gaps and min(gaps) > 15_000_000                              # 読みの間隔 >= 約 20 ms（遅れた分）
