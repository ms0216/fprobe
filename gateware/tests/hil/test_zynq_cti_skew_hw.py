"""実機: CTI で止めた 2 コア（Zynq-7000）の、止まった時間差を、目標の側で測る。

PYNQ の `/home/xilinx/lxskew2`（root。両コアで Global Timer を r4 に読み続ける 2 命令の周回。linux-awareness-trial の testapp/lxskew2.c）を
走らせ、CTI で止めてから r4・r5・PC・CPSR を読む。r4 の差 = 止まった時間差（周回 1 つ分の幅の中）。両方のコアが「ユーザーモードで周回の中」に
いた回だけ数える（Linux がコアを取り上げていた回は捨て、理由を数える）。**時間差の上限の値はこの試験が決めない**（測った最大値を出すだけ）。

JTAG のクロックは FPROBE_ZYNQ_JTAG_HZ（既定 2 MHz）。FPROBE_ZYNQ_LOAD=1 なら、測る間、PYNQ に SD カードの書き込み（割り込みと I/O）と
圧縮（CPU）の負荷をかける（lxskew2 は SCHED_FIFO なので、負荷は主に割り込みとして効く）。
FPROBE_ZYNQ_SKEW=1・FPROBE_ZYNQ_JTAG=1・FPROBE_ZYNQ_SSH（例 xilinx@<家の LAN のアドレス>）・FPROBE_ZYNQ_KEY・FPROBE_ZYNQ_SUDO_PASSWORD が要る。
**Linux 全体が止まる**（回ごとに再開する）。
"""
import os
import statistics
import subprocess
import threading
import time

import pytest

ENV = ("FPROBE_ZYNQ_SKEW", "FPROBE_ZYNQ_JTAG", "FPROBE_ZYNQ_SSH", "FPROBE_ZYNQ_KEY", "FPROBE_ZYNQ_SUDO_PASSWORD")
pytestmark = pytest.mark.skipif(not all(os.environ.get(n) for n in ENV), reason="環境変数が足りない: " + ", ".join(ENV))
TRIALS = int(os.environ.get("FPROBE_ZYNQ_TRIALS", "40"))           # 上限の見積もりは、回数を増やすほど、まれな最大を拾える
JTAG_HZ = int(os.environ.get("FPROBE_ZYNQ_JTAG_HZ", "2000000"))
USER_MODE = 0x10
MAGIC = 0xA5C0DE00
LOOP_BYTES = 33 * 4                                # ldr 32 個と b 1 個（lxskew2.c の周回）


def ssh(*cmd, **kw):
    return subprocess.Popen(["ssh", "-i", os.environ["FPROBE_ZYNQ_KEY"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                             os.environ["FPROBE_ZYNQ_SSH"], *cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kw)


@pytest.fixture()
def probe_app():
    """lxskew2 を root で走らせ、(ticks_per_s, ldr_ticks, ループの番地) を返す。終わったら止める。"""
    pw = os.environ["FPROBE_ZYNQ_SUDO_PASSWORD"]
    proc = ssh("sudo -S -p '' timeout 600 /home/xilinx/lxskew2")
    proc.stdin.write(pw + "\n")
    proc.stdin.flush()
    ready, lines = threading.Event(), []

    def reader():
        for line in proc.stdout:
            lines.append(line.rstrip())
            if line.strip() == "running":
                ready.set()
    threading.Thread(target=reader, daemon=True).start()
    assert ready.wait(30), lines
    load = None
    if os.environ.get("FPROBE_ZYNQ_LOAD"):
        load = ssh("timeout 600 sh -c 'while :; do dd if=/dev/zero of=/tmp/fprobe_load bs=64k count=64 conv=fsync 2>/dev/null; done' & "
                   "timeout 600 sh -c 'cat /dev/urandom | gzip > /dev/null' & wait")
        time.sleep(2)
    info = {l.split()[0]: l.split()[1:] for l in lines if l.strip()}
    try:
        yield (float(info["ticks_per_s"][0]), [float(x) for x in info["ldr_ticks"]], [int(x, 16) for x in info["loop"][:2]])
    finally:
        if load is not None:
            ssh("pkill -f fprobe_load; pkill -x gzip; rm -f /tmp/fprobe_load; true").communicate(timeout=60)
            load.terminate()
        k = ssh("sudo -S -p '' pkill -x lxskew2; true")
        k.communicate(pw + "\n", timeout=60)
        proc.terminate()


def signed32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def test_cti_stop_skew_measured_on_the_target(probe_app):
    from fprobe.arm_a_target import arm_a_part
    from fprobe.linux_halt.backend_arma import ArmACores
    from fprobe.linux_halt.cti import ApbIo
    from fprobe.plugins import zynq_cti
    from fprobe.session import Session
    ticks_per_s, ldr_ticks, loops = probe_app
    session = Session.open(None)
    cores = ArmACores.open(session.dap_access(), arm_a_part("zynq7000"), frequency_hz=JTAG_HZ,
                           page_offset=0xC0000000, phys_offset=0, linear_end=0xE0000000)
    results, discarded = [], {}
    try:
        cross = zynq_cti.cross_halt(zynq_cti.ZYNQ7000, ApbIo(cores._dap, cores.debug_ap), apb_offset=zynq_cti.ZYNQ7000.apb_offset_for(cores.discovery), ack_timeout_polls=100)
        cores.use_cross_halt(cross, request_skew_upper_bound_ns=1000, event_skew_upper_bound_ns=None, event_trigin=None,
                             hardware_halt_wait_s=0.05)
        for _ in range(TRIALS):
            report = cores.halt_all()
            assert report.cores_halted == (0, 1), report
            r = [{n: cores.read_reg(c, n) for n in ("cpsr", "pc", "r4", "r5")} for c in (0, 1)]
            cores.resume_all()
            why = None
            for c in (0, 1):
                if r[c]["cpsr"] & 0x1F != USER_MODE:
                    why = "ユーザーモードでない（Linux がコアを取り上げていた）"
                elif not -8 <= r[c]["pc"] - 8 - loops[c] <= LOOP_BYTES:       # PC の読みは、止まった理由（ブレークポイントか停止要求か）で 0〜8 ずれる（実測）
                    why = f"周回の中にいない（コア {c}、PC-8-周回の頭 = {r[c]['pc'] - 8 - loops[c]:#x}）"
                elif r[c]["r5"] != (MAGIC | c):
                    why = "r5 が周回の印と違う"
            if why:
                discarded[why] = discarded.get(why, 0) + 1
            else:
                results.append(signed32(r[0]["r4"] - r[1]["r4"]))
            time.sleep(0.3)
    finally:
        if any(cores.halted()):
            cores.resume_all()
        cores.close()
        session.close()
    ns = sorted(abs(t) * 1e9 / ticks_per_s for t in results)
    print(f"\nCTI で止めた時間差: {TRIALS} 回中 {len(results)} 回が有効。捨てた理由: {discarded}")
    print(f"1 刻み {1e9 / ticks_per_s:.2f} ns、ldr 1 つ {ldr_ticks} 刻み")
    if ns:
        print(f"|差| ns: 最小 {ns[0]:.0f}、中央値 {statistics.median(ns):.0f}、最大 {ns[-1]:.0f}。符号つき刻み: {results}")
    assert results, discarded


def test_breakpoint_on_core0_halts_core1_through_the_cti_and_the_delay_is_measured_on_the_target(probe_app):
    """コア 0 がブレークポイントで止まり、DBGACK → CTI → もう一方のコアの停止要求。コア 1 が遅れて止まる分（r4 の差）を測る。"""
    from fprobe.arm_a_target import arm_a_part
    from fprobe.linux_halt.backend_arma import ArmACores
    from fprobe.linux_halt.cti import ApbIo
    from fprobe.plugins import zynq_cti
    from fprobe.session import Session
    ticks_per_s, ldr_ticks, loops = probe_app
    session = Session.open(None)
    cores = ArmACores.open(session.dap_access(), arm_a_part("zynq7000"), frequency_hz=JTAG_HZ,
                           page_offset=0xC0000000, phys_offset=0, linear_end=0xE0000000)
    results, discarded = [], {}
    try:
        cross = zynq_cti.cross_halt(zynq_cti.ZYNQ7000, ApbIo(cores._dap, cores.debug_ap), apb_offset=zynq_cti.ZYNQ7000.apb_offset_for(cores.discovery), ack_timeout_polls=100)
        cores.use_cross_halt(cross, request_skew_upper_bound_ns=1000, event_skew_upper_bound_ns=None,
                             event_trigin=zynq_cti.ZYNQ7000.dbgack_in, hardware_halt_wait_s=0.05)
        for _ in range(TRIALS):
            cores.set_hw_bp(0, 0, loops[0], 4)
            hit = cores.wait_event(5.0)
            cores.clear_hw_bp(0, 0)
            assert hit == (0,) or hit == (0, 1), hit
            time.sleep(0.05)
            assert cores.halted() == (True, True), cores.halted()
            r = [{n: cores.read_reg(c, n) for n in ("cpsr", "pc", "r4", "r5")} for c in (0, 1)]
            cores.resume_all()
            why = None
            for c in (0, 1):
                if r[c]["cpsr"] & 0x1F != USER_MODE:
                    why = "ユーザーモードでない（Linux がコアを取り上げていた）"
                elif not -8 <= r[c]["pc"] - 8 - loops[c] <= LOOP_BYTES:       # PC の読みは、止まった理由（ブレークポイントか停止要求か）で 0〜8 ずれる（実測）
                    why = f"周回の中にいない（コア {c}、PC-8-周回の頭 = {r[c]['pc'] - 8 - loops[c]:#x}）"
                elif r[c]["r5"] != (MAGIC | c):
                    why = "r5 が周回の印と違う"
            if why:
                discarded[why] = discarded.get(why, 0) + 1
            else:
                results.append(signed32(r[1]["r4"] - r[0]["r4"]))      # 正 = コア 1 が後に止まった
            time.sleep(0.3)
    finally:
        if any(cores.halted()):
            cores.resume_all()
        cores.close()
        session.close()
    ns = sorted(t * 1e9 / ticks_per_s for t in results)
    print(f"\nブレークポイント → CTI の時間差（コア 1 − コア 0）: {TRIALS} 回中 {len(results)} 回が有効。捨てた理由: {discarded}")
    if ns:
        print(f"ns: 最小 {ns[0]:.0f}、中央値 {statistics.median(ns):.0f}、最大 {ns[-1]:.0f}。刻み（1 刻み {1e9 / ticks_per_s:.2f} ns）: {results}")
    assert results, discarded
