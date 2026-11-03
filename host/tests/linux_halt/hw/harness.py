"""Hardware regression rig for fprobe.linux_halt (port of the prototype's tests/hw): Raspberry Pi 2B on JTAG, through
the fprobe (LXDBG_BACKEND=fprobe) or through OpenOCD (LXDBG_BACKEND=openocd, already running on tcp 6666).

Each test starts its own `python -m fprobe.linux_halt attach`, drives gdb (in WSL) with a script, then checks the output
AND that the target was left clean: no BKPT in the debuggee's text, cores running, no breakpoint/watchpoint programmed,
journal removed.

EVERY setting comes from the environment (the rig has no hidden defaults); a missing one is reported by name:
  LXDBG_PROTOTYPE_ROOT   the linux-awareness-trial checkout (it holds testapp/*.c and the offsets files)
  LXDBG_BACKEND          fprobe | openocd
  LXDBG_OFFSETS          the kernel offsets JSON (offsets-stock.json for the stock kernel)
  LXDBG_JTAG_KHZ  LXDBG_PAGE_OFFSET  LXDBG_PHYS_OFFSET  LXDBG_LINEAR_END  LXDBG_SKEW_LIMIT_NS
  LXDBG_PART             (fprobe) the part name, e.g. bcm2836
Optional (fprobe), each only when set:
  LXDBG_PROBE_ETH        the probe is the PYNQ-Z2 fprobe at this address (Ethernet relay) instead of the Primer (USB)
  LXDBG_FPGA_HALT_WATCH_BOUND_NS and LXDBG_FPGA_HALT_WATCH_POLL_S   use the FPGA halt watch (both, or neither;
                         the bound is ns measured on the target, or "unmeasured")
"""
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

def required(name):
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError("the environment variable %s is not set (see the docstring of tests/linux_halt/hw/harness.py)" % name)
    return value


ROOT = os.environ.get("LXDBG_PROTOTYPE_ROOT", "")            # testapp/*.c live there (checked when the rig is used)
HOST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))   # fprobe's host/
JOURNAL = os.path.join(tempfile.gettempdir(), "fprobe_linux_halt_journal.json")


def backend_flags():
    """The settings every subcommand needs, from the environment (rig constants: tasks 100000, threads 4096 are the prototype's)."""
    backend = required("LXDBG_BACKEND")
    flags = ["--backend", backend, "--jtag-khz", required("LXDBG_JTAG_KHZ"), "--offsets", required("LXDBG_OFFSETS"),
             "--page-offset", required("LXDBG_PAGE_OFFSET"), "--phys-offset", required("LXDBG_PHYS_OFFSET"),
             "--linear-map-end", required("LXDBG_LINEAR_END"), "--max-tasks", "100000", "--max-threads", "4096",
             "--mmu-format", "short", "--journal", JOURNAL]
    if backend == "fprobe":
        flags += ["--part", required("LXDBG_PART")]
        flags += os.environ.get("LXDBG_EXTRA_FLAGS", "").split()          # 例: --cti-board zynq7000 --cti-apb-offset 0 ...
        if os.environ.get("LXDBG_PROBE_ETH"):
            flags += ["--probe-eth", os.environ["LXDBG_PROBE_ETH"],
                      "--probe-eth-link-up-wait", required("LXDBG_PROBE_ETH_LINK_UP_WAIT")]
        if os.environ.get("LXDBG_FPGA_HALT_WATCH_BOUND_NS") or os.environ.get("LXDBG_FPGA_HALT_WATCH_POLL_S"):
            flags += ["--fpga-halt-watch-bound-ns", required("LXDBG_FPGA_HALT_WATCH_BOUND_NS"),
                      "--fpga-halt-watch-poll-s", required("LXDBG_FPGA_HALT_WATCH_POLL_S")]
    else:
        flags += ["--ocd-board", "rpi2b", "--ocd-port", "6666", "--ocd-timeout-s", "90", "--hw-slots", "5", "--watch-slots", "4"]
    return flags


def module_cmd(sub, *extra):
    return [sys.executable, "-u", "-m", "fprobe.linux_halt", sub, *backend_flags(), *extra]
# 対象の機器（既定は Raspberry Pi 2B。PYNQ-Z2 の Zynq は環境変数で切り替える）
KEY = os.path.expanduser(os.environ.get("LXDBG_SSH_KEY", "~/.ssh/fprobe_pi"))
PI = os.environ.get("LXDBG_SSH_TARGET", "ms0216@<Pi の LAN のアドレス>")
SUDO = os.environ.get("LXDBG_SUDO", "sudo -n")                  # 例（PYNQ）: echo xilinx | sudo -S -p ''
NCORES = int(os.environ.get("LXDBG_NCORES", "4"))
GDB = ["wsl", "-d", "Ubuntu", "--", "gdb-multiarch"]
PRELUDE = """set architecture arm
set pagination off
set confirm off
set remotetimeout 60
set print pretty off
"""


def wslpath(p):
    p = os.path.abspath(p).replace("\\", "/")
    return "/mnt/%s%s" % (p[0].lower(), p[2:])


class Rig:
    def ssh(self, cmd, timeout=60):
        r = subprocess.run(["ssh", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                            "-o", "ConnectTimeout=10", PI, cmd],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.stdout.strip()

    def deploy(self, local, remote):
        subprocess.run(["scp", "-q", "-i", KEY, "-o", "IdentitiesOnly=yes", local, "%s:%s.new" % (PI, remote)],
                       check=True, timeout=120, stdin=subprocess.DEVNULL)
        self.ssh("chmod +x %s.new && mv -f %s.new %s" % (remote, remote, remote))   # mv: target may be running

    def fetch(self, remote, local):
        """対象の機器のファイルを手元に写す（gdb の sysroot 用。symlink は対象の上で実体をたどる）。"""
        real = self.ssh("readlink -f %s" % remote)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        subprocess.run(["scp", "-q", "-i", KEY, "-o", "IdentitiesOnly=yes", "%s:%s" % (PI, real), local],
                       check=True, timeout=120, stdin=subprocess.DEVNULL)

    def prepare_pi(self):
        """Watchdog off (a halted system would be reset after ~1 min), stale apps killed."""
        if os.environ.get("LXDBG_NO_WATCHDOG_SETUP"):
            return                                    # ウォッチドッグが無い機器（PYNQ）では触らない
        self.ssh("sudo -n mkdir -p /run/systemd/system.conf.d && printf '[Manager]\\nRuntimeWatchdogSec=0\\n"
                 "RebootWatchdogSec=0\\n' | sudo -n tee /run/systemd/system.conf.d/90-nowdt.conf >/dev/null; "
                 "sudo -n systemctl daemon-reexec")

    def start_app(self, remote, *args):
        """Run the app as the child of an SSH session that stays open for the whole test. (Started from a
        session that ends, systemd-logind kills it with the session scope; no change to the Pi is needed.)"""
        name = os.path.basename(remote)
        self.kill_app(name)
        self._apps = getattr(self, "_apps", {})
        self._apps[name] = subprocess.Popen(
            ["ssh", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=5", PI,
             "exec %s %s >/tmp/%s.log 2>&1" % (remote, " ".join(args), name)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            pid = self.ssh("pgrep -x %s | head -1" % name)
            if pid.isdigit():
                return int(pid)
            time.sleep(0.5)
        raise RuntimeError("could not start %s" % remote)

    def kill_app(self, name):
        p = getattr(self, "_apps", {}).pop(name, None)
        if p is not None:
            p.terminate()
            p.wait(timeout=10)
        self.ssh("pkill -x %s; sleep 0.3; true" % name)

    def cleanup(self):
        """試験が落ちて、デバッガが BKPT やコアの停止を残したときの後始末（残したままだと、共有の libc・カーネルの BKPT で、ターゲットの
        全プロセスが落ちる）。journal があれば `recover` で戻し、止まったコアがあれば `rescue` で再開する。fprobe の経路だけ。"""
        if required("LXDBG_BACKEND") != "fprobe":
            return
        if os.path.exists(JOURNAL):
            subprocess.run(module_cmd("recover"), cwd=HOST, capture_output=True, text=True, errors="replace", timeout=300, stdin=subprocess.DEVNULL)
        try:
            running = [r for r, _, _ in self._status()]
        except AssertionError:
            running = []
        if not all(running):
            subprocess.run(module_cmd("rescue"), cwd=HOST, capture_output=True, text=True, errors="replace", timeout=300, stdin=subprocess.DEVNULL)

    def text_clean(self, pid, *files):
        """True if every executable mapping of each file in the debuggee equals the file's bytes, i.e. no BKPT
        was left behind (works for PIE and shared libraries: the mappings are found in /proc/PID/maps)."""
        script = (
            'P=%d; for F in %s; do found=0; '
            'while read range perms off rest; do '
            '  s=$((0x${range%%%%-*})); e=$((0x${range##*-})); o=$((0x$off)); n=$((e-s)); found=1; '
            '  %s dd if=/proc/$P/mem iflag=skip_bytes,count_bytes skip=$s count=$n 2>/dev/null '
            '    | cmp -s - <(dd if=$F iflag=skip_bytes,count_bytes skip=$o count=$n 2>/dev/null) || echo "DIFF $F $range"; '
            'done < <(grep -F " $F" /proc/$P/maps | grep " r-xp "); '
            '[ $found = 1 ] || echo "NOMAP $F"; done; echo END' % (pid, " ".join(files), SUDO))
        out = self.ssh("bash -c '%s'" % script.replace("'", "'\\''"))
        return out.strip() == "END", out

    def _status(self):
        """Read-only `status` of the fprobe backend: [(running, [bp slots], [wp slots])] per core."""
        r = subprocess.run(module_cmd("status"), cwd=HOST, capture_output=True, text=True, errors="replace", timeout=120,
                           stdin=subprocess.DEVNULL)
        out = r.stdout
        rows = re.findall(r"cpu(\d) (running|halted) bp=\[(.*?)\] wp=\[(.*?)\]", out)
        if len(rows) != NCORES:
            raise AssertionError("status did not list the expected cores:\n" + out + "\n--- stderr:\n" + r.stderr[-2000:])
        return [(state == "running", bp, wp) for _, state, bp, wp in rows]

    def _ocd(self, script):
        import socket
        s = socket.create_connection(("127.0.0.1", 6666))
        try:
            s.sendall((script + "\x1a").encode())
            buf = b""
            while not buf.endswith(b"\x1a"):
                buf += s.recv(4096)
            return buf[:-1].decode().strip()
        finally:
            s.close()

    def watchpoints_left(self):
        """Per core: what is still programmed (an empty string = nothing)."""
        if required("LXDBG_BACKEND") == "fprobe":
            return [wp for _, _, wp in self._status()]
        return [self._ocd("targets rpi2.cpu%d; wp" % n) for n in range(NCORES)]

    def cores_running(self):
        if required("LXDBG_BACKEND") == "fprobe":
            return ["running" if running else "halted" for running, _, _ in self._status()]
        return self._ocd("set lxs {}; foreach n {0 1 2 3} {lappend lxs [rpi2.cpu$n curstate]}; set lxs").split()


class Session:
    """One `lxdbg attach` + one gdb run."""

    def __init__(self, rig, pid):
        self.rig, self.pid = rig, pid
        self.lx = None
        self.log = []

    def __enter__(self):
        self.logfile = os.path.join(tempfile.gettempdir(), "fprobe_linux_halt_%d.log" % self.pid)
        if os.path.exists(JOURNAL):
            os.remove(JOURNAL)                                    # a test starts from no journal (and asserts there is none after)
        self.lx = subprocess.Popen(module_cmd("attach", "--pid", str(self.pid), "--port", "3340",
                                              "--skew-limit-ns", required("LXDBG_SKEW_LIMIT_NS"), "--thread-id", "stack",
                                              "--kernel-isa", "arm", "--max-step-retries", "8", "--bp-unmapped", "error",
                                              "--log", self.logfile),
                                   cwd=HOST, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors="replace",
                                   stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        deadline = time.time() + 180
        while time.time() < deadline:
            line = self.lx.stdout.readline()
            if not line:
                raise RuntimeError("fprobe.linux_halt exited early:\n" + "".join(self.log))
            self.log.append(line)
            if line.startswith("gdb:"):
                return self
        raise RuntimeError("fprobe.linux_halt did not become ready:\n" + "".join(self.log))

    def gdb(self, elf, script, timeout=240, interrupt_after=None, pre="", at=None):
        """`pre` runs before `file`/`target remote` (e.g. `set sysroot ...` must precede the connection)."""
        with tempfile.NamedTemporaryFile("w", suffix=".gdb", dir=tempfile.gettempdir(), delete=False, newline="\n") as f:
            f.write(PRELUDE + pre + "file %s\ntarget remote localhost:3340\n" % wslpath(elf) + script)
            path = f.name
        try:
            p = subprocess.Popen(GDB + ["-q", "-batch", "-x", wslpath(path)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL)
            if at:                                    # (seconds, callable): do something while gdb runs the target
                time.sleep(at[0])
                at[1]()
            if interrupt_after:
                time.sleep(interrupt_after)
                subprocess.run(["wsl", "-d", "Ubuntu", "--", "pkill", "-INT", "gdb-multiarch"])
            try:
                out, _ = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                subprocess.run(["wsl", "-d", "Ubuntu", "--", "pkill", "gdb-multiarch"])
                out, _ = p.communicate()
                raise AssertionError("gdb timed out. gdb output so far:\n%s\n--- lxdbg log tail:\n%s"
                                     % (out[-3000:], "".join(self.log[-40:])))
            return out
        finally:
            os.remove(path)

    def packet_log_tail(self, n=30):
        try:
            with open(self.logfile, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return "(no packet log)"

    def __exit__(self, *exc):
        try:
            self.lx.wait(timeout=60)
        except subprocess.TimeoutExpired:
            # Never kill -9: that would leave BKPTs in the debuggee. Ask it to clean up (Ctrl-Break).
            self.lx.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            try:
                self.lx.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.lx.kill()
        self.lx.stdout.close()


def values(out, name):
    """All integers printed as '@@name=<int>' markers in gdb output, in order."""
    return [int(x) for x in re.findall(r"@@%s=(-?\d+)" % re.escape(name), out)]
