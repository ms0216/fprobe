"""実機: Primer の JTAG（PJTAG）で PYNQ-Z2 の Linux のアプリを、標準の gdb（WSL の gdb-multiarch）でデバッグする（`python -m fprobe.linux_halt attach`、CTI で全コア同時停止）。

PYNQ で `linux-awareness-trial/testapp/lxtest.c`（-g -O0 -no-pie）を作って走らせ、attach → gdb（ブレークポイント・bt・next・step・変数・finish・detach）→
アプリが走り続けていることを確かめる。**Linux 全体が止まる**（止まる間だけ）。
環境変数: FPROBE_ZYNQ_JTAG=1・FPROBE_ZYNQ_SSH・FPROBE_ZYNQ_KEY・FPROBE_ZYNQ_OFFSETS（pynq/offsets-pynq.json）・FPROBE_ZYNQ_LXTEST_C（lxtest.c）。
"""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

import pytest

ENV = ("FPROBE_ZYNQ_JTAG", "FPROBE_ZYNQ_SSH", "FPROBE_ZYNQ_KEY", "FPROBE_ZYNQ_OFFSETS", "FPROBE_ZYNQ_LXTEST_C")
pytestmark = pytest.mark.skipif(not all(os.environ.get(n) for n in ENV), reason="環境変数が足りない: " + ", ".join(ENV))
HOST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) + "/host"
PORT = "3340"


def ssh(cmd, timeout=120):
    r = subprocess.run(["ssh", "-i", os.environ["FPROBE_ZYNQ_KEY"], "-o", "BatchMode=yes", os.environ["FPROBE_ZYNQ_SSH"], cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def wslpath(p):
    p = os.path.abspath(p).replace("\\", "/")
    return "/mnt/%s%s" % (p[0].lower(), p[2:])


def resume_if_halted():
    """attach が後始末をしそこねても、Linux を止めたまま残さない（Primer から両コアを再開する）。"""
    from fprobe.arm_a_target import arm_a_part
    from fprobe.linux_halt.backend_arma import ArmACores
    from fprobe.session import Session
    session = Session.open(None)
    cores = ArmACores.open(session.dap_access(), arm_a_part("zynq7000"), frequency_hz=2_000_000,
                           page_offset=0xC0000000, phys_offset=0, linear_end=0xDE000000)
    try:
        if any(cores.halted()):
            cores.resume_all()
            return True
        return False
    finally:
        cores.close()
        session.close()


def test_debug_a_linux_app_with_gdb():
    work = tempfile.mkdtemp(prefix="zynq_gdb_")
    key, who = os.environ["FPROBE_ZYNQ_KEY"], os.environ["FPROBE_ZYNQ_SSH"]
    subprocess.run(["scp", "-q", "-i", key, os.environ["FPROBE_ZYNQ_LXTEST_C"], who + ":/tmp/lxtest.c"], check=True)
    ssh("pkill lxtest; cd /tmp && gcc -g -O0 -no-pie -o lxtest lxtest.c && (nohup ./lxtest >/tmp/lxtest.out 2>&1 & echo $! > /tmp/lxtest.pid)")
    time.sleep(1)
    pid = ssh("cat /tmp/lxtest.pid")
    elf = os.path.join(work, "lxtest")
    subprocess.run(["scp", "-q", "-i", key, who + ":/tmp/lxtest", elf], check=True)
    cmd = [sys.executable, "-u", "-X", "utf8", "-m", "fprobe.linux_halt", "attach", "--backend", "fprobe", "--part", "zynq7000",
           "--jtag-khz", "2000", "--offsets", os.environ["FPROBE_ZYNQ_OFFSETS"], "--page-offset", "0xC0000000", "--phys-offset", "0",
           "--linear-map-end", "0xDE000000", "--max-tasks", "100000", "--max-threads", "4096", "--mmu-format", "short",
           "--journal", os.path.join(work, "journal.json"),
           "--cti-board", "zynq7000", "--cti-request-bound-ns", "1000", "--cti-event-bound-ns", "unmeasured",
           "--cti-hw-wait-s", "0.05", "--cti-ack-polls", "100",
           "--pid", pid, "--port", PORT, "--skew-limit-ns", "1000", "--thread-id", "stack", "--kernel-isa", "arm",
           "--max-step-retries", "8", "--bp-unmapped", "error"]
    lx = subprocess.Popen(cmd, cwd=HOST, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                          stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONPATH": HOST},
                          creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        log, deadline = [], time.time() + 120
        while time.time() < deadline:
            line = lx.stdout.readline()
            if not line:
                raise AssertionError("attach が途中で終わった:\n" + "".join(log))
            log.append(line)
            if line.startswith("gdb:"):
                break
        else:
            raise AssertionError("attach の準備が終わらない:\n" + "".join(log))
        script = os.path.join(work, "t.gdb")
        with open(script, "w", newline="\n") as f:
            f.write("set architecture arm\nset pagination off\nset confirm off\nset remotetimeout 60\n"
                    "file %s\ntarget remote localhost:%s\nbreak accumulate\ncontinue\nbt 2\nnext\nnext\nstep\n"
                    "printf \"@@v=%%d\\n\", v\nfinish\ndetach\n" % (wslpath(elf), PORT))
        env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
        out = subprocess.run(["wsl", "-d", "Ubuntu", "--", "gdb-multiarch", "-q", "-batch", "-x", wslpath(script)],
                             capture_output=True, text=True, errors="replace", timeout=240, env=env).stdout.replace("\0", "")
        print("\n" + out[-1500:])
    finally:
        # kill しない（デバッグ対象に BKPT が残り、コアが止まったままになる）。Ctrl-Break で後始末をさせる
        try:
            lx.wait(timeout=60)                                  # gdb が detach したら、attach は後始末をして自分で終わる
        except subprocess.TimeoutExpired:
            lx.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            try:
                lx.wait(timeout=60)
            except subprocess.TimeoutExpired:
                lx.kill()
            resume_if_halted()
            pytest.fail("attach が後始末に応じなかった（コアは Primer から再開した）")
    assert not resume_if_halted(), "gdb が終わったあと、コアが止まったままだった（再開した）"
    assert "Breakpoint 1, accumulate (p=" in out
    assert "square (v=" in out
    v = int(re.search(r"@@v=(-?\d+)", out).group(1))
    assert f"Value returned is $1 = {v * v}" in out
    # 後始末: アプリが走り続けている・コアは止まっていない・BKPT が残っていない（途中で落ちていない）
    assert ssh("ps -o stat= -p %s" % pid).startswith(("S", "R"))
    ssh("pkill lxtest")
