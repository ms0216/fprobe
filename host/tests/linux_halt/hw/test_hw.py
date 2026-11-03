"""Hardware regression tests (port of the prototype's tests/hw/test_hw.py). Run (see harness.py for the environment):
    LXDBG_HW=1 python -m pytest tests/linux_halt/hw -v
Needs the Pi reachable, WSL gdb-multiarch and arm-linux-gnueabihf-gcc in WSL, and the fprobe (or OpenOCD) on the Pi's JTAG."""
import os
import shutil
import subprocess
import tempfile
import unittest

from .harness import JOURNAL, NCORES, ROOT, Rig, Session, values, wslpath

HW = os.environ.get("LXDBG_HW") == "1"
APP = os.path.join(ROOT, "testapp", "lxtest")
REMOTE = "/tmp/lxtest"


def f(x):
    """What accumulate(&{x,4}, 5) returns in lxtest.c."""
    return sum((x + i) ** 2 + 4 for i in range(5))


@unittest.skipUnless(HW, "set LXDBG_HW=1 to run on the real board")
class HwCase(unittest.TestCase):
    rig = None
    app = "lxtest"          # testapp/<app>.c, built static and run as /tmp/<app> on the Pi
    cflags = ""

    @classmethod
    def setUpClass(cls):
        cls.rig = Rig()
        cls.rig.prepare_pi()
        cls.elf = os.path.join(ROOT, "testapp", cls.app)
        cls.remote = "/tmp/" + cls.app
        subprocess.run(["wsl", "-d", "Ubuntu", "--", "bash", "-c",
                        "cd %s/testapp && arm-linux-gnueabihf-gcc -g -O0 -static -marm %s -o %s %s.c"
                        % (wslpath(ROOT), cls.cflags, cls.app, cls.app)], check=True)
        cls.rig.deploy(cls.elf, cls.remote)

    app_args = ()

    def setUp(self):
        self.pid = self.rig.start_app(self.remote, *self.app_args)

    def tearDown(self):
        self.rig.cleanup()                    # 落ちた試験が残した BKPT・止めたコアを戻す（次の試験とターゲットを守る）
        self.rig.kill_app(self.app)

    def clean_files(self):
        """Files whose executable mappings must equal the file after a session (no BKPT left behind)."""
        return [self.remote]

    def session(self, script, check_text=True, **kw):
        with Session(self.rig, self.pid) as s:
            out = s.gdb(self.elf, script, **kw)
        self.out = out
        self.assertEqual(self.rig.cores_running(), ["running"] * NCORES, "cores left halted\n" + out)
        if check_text:                        # (not for a target that has exited: nothing is mapped any more)
            clean, detail = self.rig.text_clean(self.pid, *self.clean_files())
            self.assertTrue(clean, "BKPT left in target text: %s\n%s" % (detail, out))
        self.assertFalse(os.path.exists(JOURNAL), "journal left behind")
        return out


class Basics(HwCase):
    def test_attach_backtrace_threads(self):
        out = self.session("info threads\nbt\ndetach\n")
        self.assertIn("main () at lxtest.c", out)
        self.assertRegex(out, r'Thread \d+ "lxtest"')

    def test_breakpoint_never_misses_consecutive_calls(self):
        out = self.session("break accumulate\n" + "continue\nprintf \"@@x=%d\\n\", p->x\n" * 6 + "detach\n")
        xs = values(out, "x")
        self.assertEqual(len(xs), 6, out)
        for a, b in zip(xs, xs[1:]):
            self.assertEqual((b - a) % 7, 1, "a call was skipped: %r" % xs)   # x = loop % 7

    def test_next_step_finish_follow_the_source(self):
        out = self.session("break accumulate\ncontinue\nnext\nnext\nnext\nstep\nbt\nfinish\n"
                           "printf \"@@x=%d\\n\", pt.x\ndetach\n")
        lines = [l for l in out.splitlines() if l[:3].strip().isdigit() and "\t" in l]
        self.assertEqual([int(l.split("\t")[0]) for l in lines[:5]], [11, 12, 13, 12, 13], out)
        self.assertIn("accumulate (p=", out)
        x = values(out, "x")[0]
        self.assertIn("Value returned is $1 = %d" % f(x), out)

    def test_step_into_function_and_out(self):
        out = self.session("break accumulate\ncontinue\nnext\nnext\nstep\n"
                           "printf \"@@v=%d\\n\", v\nbt 2\nfinish\ndetach\n")
        self.assertIn("square (v=", out)
        v = values(out, "v")[0]
        self.assertIn("Value returned is $1 = %d" % (v * v), out)
        self.assertRegex(out, r"accumulate \(p=")

    def test_write_memory_changes_behaviour(self):
        out = self.session("break accumulate\ncontinue\nset var p->x = 5\nfinish\ndetach\n")
        self.assertIn("Value returned is $1 = %d" % f(5), out)

    def test_register_write(self):
        # -O0 keeps v on the stack after the prologue, so write r0 at the very first instruction
        out = self.session("break *square\ncontinue\nset var $r0 = 9\nfinish\ndetach\n")
        self.assertIn("Value returned is $1 = 81", out)

    def test_conditional_breakpoint_stops_only_when_true(self):
        out = self.session("break accumulate if p->x == 3\n" + "continue\nprintf \"@@x=%d\\n\", p->x\n" * 3 + "detach\n")
        self.assertEqual(values(out, "x"), [3, 3, 3], out)

    def test_ctrl_c_interrupts_a_running_target(self):
        out = self.session("continue\nbt 1\nprintf \"@@done=1\\n\"\ndetach\n", interrupt_after=40)
        self.assertEqual(values(out, "done"), [1], out)
        self.assertIn("SIGINT", out)

    def test_delete_breakpoint_then_continue_runs_free(self):
        out = self.session("break accumulate\ncontinue\ndelete\ncontinue\nprintf \"@@never=1\\n\"\n",
                           interrupt_after=40)
        self.assertEqual(values(out, "never"), [1], out)


class Watchpoints(HwCase):
    """lxtest.c line 26: g_counter is written every loop (and read once), g_flag every 5th loop."""

    def session(self, script, **kw):
        out = super().session(script, **kw)
        left = self.rig.watchpoints_left()
        self.assertEqual(left, [""] * NCORES, "watchpoints left programmed after detach: %r\n%s" % (left, out))
        return out

    @staticmethod
    def changes(out):
        import re
        return [(int(a), int(b)) for a, b in re.findall(r"Old value = (\d+)\s+New value = (\d+)", out)]

    def test_write_watch_reports_every_change_with_old_and_new_value(self):
        out = self.session("watch g_counter\n" + "continue\n" * 6 + "detach\n")
        ch = self.changes(out)
        self.assertEqual(len(ch), 6, out)
        for old, new in ch:
            self.assertEqual(new, old + 1, out)                    # the new value is the value after the store
        for (_, new), (old, _) in zip(ch, ch[1:]):
            self.assertEqual(new, old, "a store was missed: %r" % ch)

    def test_two_watchpoints_both_fire(self):
        out = self.session("watch g_counter\nwatch g_flag\n" + "continue\n" * 14 + "detach\n")
        self.assertIn("Hardware watchpoint 1: g_counter", out)
        self.assertIn("Hardware watchpoint 2: g_flag", out)
        import re
        hits = re.findall(r"Hardware watchpoint (\d): (g_\w+)\s+Old value", out)
        self.assertIn(("2", "g_flag"), hits, "g_flag (written every 5th loop) never fired:\n" + out)
        self.assertIn(("1", "g_counter"), hits)

    def test_read_watch_stops_on_a_load(self):
        out = self.session("rwatch g_counter\ncontinue\ncontinue\ndetach\n")
        self.assertRegex(out, r"Hardware read watchpoint 1: g_counter\s+Value = \d+")

    def test_access_watch_stops_on_reads_and_writes(self):
        out = self.session("awatch g_counter\n" + "continue\n" * 4 + "detach\n")
        self.assertRegex(out, r"Hardware access \(read/write\) watchpoint 1: g_counter")
        self.assertRegex(out, r"Old value = \d+\s+New value = \d+")           # a write
        self.assertRegex(out, r"Value = \d+")                                 # a read

    def test_deleted_watch_lets_the_target_run_free(self):
        out = self.session("watch g_counter\ncontinue\ndelete\ncontinue\nprintf \"@@never=1\\n\"\n", interrupt_after=40)
        self.assertEqual(values(out, "never"), [1], out)

    def test_step_over_an_instruction_that_writes_a_watched_variable(self):
        out = self.session("watch g_counter\ncontinue\nstep\nstep\nprintf \"@@c=%lu\\n\", g_counter\ndetach\n")
        self.assertEqual(len(self.changes(out)), 1, out)
        self.assertEqual(len(values(out, "c")), 1)


class Threads(HwCase):
    """lxthreads.c: three workers (w0..w2) meet at a barrier, then all call worker_step(id) at the same
    moment on different cores; g_hits[id] counts each worker's calls."""
    app = "lxthreads"
    cflags = "-pthread"

    def test_all_threads_are_listed_with_their_names(self):
        out = self.session("info threads\ndetach\n")
        for name in ("w0", "w1", "w2"):
            self.assertIn('"%s"' % name, out)
        self.assertRegex(out, r'"lxthreads"')                               # the main thread

    def test_simultaneous_hits_in_different_threads_are_all_reported(self):
        """Every worker calls worker_step each round, together. With 3 workers and N rounds there must be
        3*N stops, and for each worker its g_hits values must be consecutive (no hit lost)."""
        rounds = 6
        out = self.session("break worker_step\n" + ("continue\nprintf \"@@id=%d\\n\", id\n"
                                                    "printf \"@@n=%lu\\n\", g_hits[id]\n") * (3 * rounds) + "detach\n")
        ids, ns = values(out, "id"), values(out, "n")
        self.assertEqual(len(ids), 3 * rounds, out)
        per = {}
        for i, n in zip(ids, ns):
            per.setdefault(i, []).append(n)
        self.assertEqual(sorted(per), [0, 1, 2], "not every worker stopped: %r" % per)
        for i, seq in per.items():
            self.assertEqual(seq, list(range(seq[0], seq[0] + len(seq))), "worker %d lost a hit: %r" % (i, seq))

    def test_switching_threads_shows_each_threads_own_stack(self):
        out = self.session("break worker_step\ncontinue\n"
                           "thread apply all bt 3\ndetach\n")
        import re
        self.assertGreaterEqual(len(re.findall(r"^Thread \d+ ", out, re.M)), 4, out)        # main + 3 workers
        self.assertIn("worker_step (id=", out)
        self.assertIn("worker (arg=", out)

    def test_selecting_a_thread_then_stepping_it(self):
        """Select a worker other than the one that was reported, then step it. Which of the other workers have
        reached worker_step when a stop completes depends on timing (the stop is fast: the others may still be on
        their way out of the barrier), so look for one that is there instead of assuming a thread number."""
        script = (
            "break worker_step\n"
            "python\n"
            "found = 0\n"
            "for _ in range(10):\n"
            "    gdb.execute('continue')\n"
            "    cur = gdb.selected_thread()\n"
            "    for t in gdb.selected_inferior().threads():\n"
            "        if t.num != cur.num:\n"
            "            t.switch()\n"
            "            if gdb.selected_frame().name() == 'worker_step':\n"
            "                found = t.num\n"
            "                break\n"
            "    if found:\n"
            "        break\n"
            "    cur.switch()\n"
            "print('@@other=%d' % found)\n"
            "if found:\n"
            "    gdb.execute('thread %d' % found)\n"
            "end\n"
            "printf \"@@id=%d\\n\", id\nnext\nnext\ndetach\n")
        out = self.session(script)
        self.assertEqual(values(out, "other") and values(out, "other")[0] > 0, True,
                         "no other worker was at worker_step in 10 stops:\n" + out[-1500:])
        self.assertEqual(len(values(out, "id")), 1, out)
        self.assertIn("lxthreads.c", out)


class Dynamic(HwCase):
    """lxdyn: a dynamically linked PIE (random load address) that calls into libcalc.so and libc. gdb gets the
    Pi's own libraries as sysroot so it can read their symbols; the stub only has to serve memory and auxv."""
    app = "lxdyn"
    LIBC = "/usr/lib/arm-linux-gnueabihf/libc.so.6"
    # gdb に渡す、対象の機器の libc を置いた所。既定は「対象の機器から写す」（別の機器の libc を渡すと、gdb が libc の別の所に BKPT を入れ、
    # 対象の全プロセスが落ちる。2026-11-01 に PYNQ で起きた）。LXDBG_SYSROOT で、用意した所を明示できる
    SYSROOT = os.environ.get("LXDBG_SYSROOT") or os.path.join(tempfile.gettempdir(), "fprobe_linux_halt_sysroot")

    @classmethod
    def setUpClass(cls):
        cls.rig = Rig()
        cls.rig.prepare_pi()
        cls.elf = os.path.join(ROOT, "testapp", cls.app)
        cls.remote = "/tmp/" + cls.app
        subprocess.run(["wsl", "-d", "Ubuntu", "--", "bash", "-c",
                        "cd %s/testapp && arm-linux-gnueabihf-gcc -g -O0 -marm -fPIC -shared -o libcalc.so libcalc.c && "
                        "arm-linux-gnueabihf-gcc -g -O0 -marm -fPIE -pie -o lxdyn lxdyn.c -L. -lcalc -Wl,-rpath,/tmp"
                        % wslpath(ROOT)], check=True)
        cls.rig.deploy(os.path.join(ROOT, "testapp", "libcalc.so"), "/tmp/libcalc.so")
        cls.rig.deploy(cls.elf, cls.remote)
        # gdb looks for a library at <sysroot><path the target loaded it from>: give it our (-g) build at /tmp/
        if not os.environ.get("LXDBG_SYSROOT"):
            for path in (cls.LIBC, "/lib/ld-linux-armhf.so.3"):
                cls.rig.fetch(path, os.path.join(cls.SYSROOT, path.lstrip("/")))
            ld = os.path.join(cls.SYSROOT, "lib", "ld-linux-armhf.so.3")
            for alias in ("lib/arm-linux-gnueabihf/libc.so.6", "lib/arm-linux-gnueabihf/ld-linux-armhf.so.3"):
                src = os.path.join(cls.SYSROOT, cls.LIBC.lstrip("/")) if alias.endswith("libc.so.6") else ld
                os.makedirs(os.path.dirname(os.path.join(cls.SYSROOT, alias)), exist_ok=True)
                shutil.copy(src, os.path.join(cls.SYSROOT, alias))
        os.makedirs(os.path.join(cls.SYSROOT, "tmp"), exist_ok=True)
        shutil.copy(os.path.join(ROOT, "testapp", "libcalc.so"), os.path.join(cls.SYSROOT, "tmp", "libcalc.so"))

    def clean_files(self):
        return [self.remote, "/tmp/libcalc.so", self.LIBC]

    def run_gdb(self, script, **kw):
        return self.session(script, pre="set sysroot %s\n" % wslpath(self.SYSROOT), **kw)

    def test_shared_libraries_are_found_through_the_target_memory(self):
        out = self.run_gdb("info sharedlibrary\ndetach\n")
        self.assertIn("libcalc.so", out)
        self.assertIn("libc.so.6", out)

    def test_breakpoint_inside_a_shared_library_with_source_and_backtrace(self):
        out = self.run_gdb("break calc_sum\ncontinue\nbt\nprintf \"@@a=%d\\n\", a\nprintf \"@@b=%d\\n\", b\n"
                           "finish\ndetach\n")
        self.assertIn("calc_sum (a=", out)
        self.assertIn("libcalc.c", out)
        self.assertIn("local_work", out)
        self.assertIn("main ()", out)
        a, b = values(out, "a")[0], values(out, "b")[0]
        self.assertEqual(b, 2 * a)
        self.assertIn("Value returned is $1 = %d" % (3 * a), out)

    def test_watchpoint_on_a_variable_in_a_shared_library(self):
        out = self.run_gdb("watch g_calc_calls\n" + "continue\n" * 4 + "detach\n")
        import re
        ch = [(int(x), int(y)) for x, y in re.findall(r"Old value = (\d+)\s+New value = (\d+)", out)]
        self.assertEqual(len(ch), 4, out)
        for old, new in ch:
            self.assertEqual(new, old + 1)

    def test_breakpoint_in_libc_leaves_libc_text_intact(self):
        out = self.run_gdb("break usleep\ncontinue\nbt 2\ndetach\n")
        self.assertIn("usleep", out)


class Exit(HwCase):
    """lxexit idles until /tmp/lxexit.go exists. The test lets gdb `continue`, then creates the file so the
    target exits while the debugger is running it: the exit must be reported at once (not at the next stop)."""
    app = "lxexit"
    cflags = "-pthread"
    GO_AFTER = 28          # seconds after gdb starts (attach + connect + continue take about this long)

    def setUp(self):
        self.rig.ssh("rm -f /tmp/lxexit.go")
        super().setUp()

    def tearDown(self):
        self.rig.ssh("rm -f /tmp/lxexit.go")
        super().tearDown()

    def finish_session(self, script="continue\nprintf \"@@after=1\\n\"\n"):
        out = self.session(script, check_text=False, timeout=180,
                           at=(self.GO_AFTER, lambda: self.rig.ssh("touch /tmp/lxexit.go")))
        self.assertFalse(os.path.exists(JOURNAL), "journal left behind")
        return out

    def test_normal_exit_reports_the_exit_code(self):
        out = self.finish_session()
        self.assertRegex(out, r"exited with code 07")

    def test_a_breakpoint_that_was_never_hit_is_removed_before_the_process_dies(self):
        self.app_args = ()
        # `exit` has not run yet, so its page is not mapped: a software breakpoint is refused, a hardware one is asked for
        out = self.session("hbreak exit\ncontinue\nbt 1\ndetach\n", check_text=False, timeout=180,
                           at=(self.GO_AFTER, lambda: self.rig.ssh("touch /tmp/lxexit.go")))
        self.assertIn("exit", out)                    # stopped at the (libc-static) exit() call, alive
        self.assertFalse(os.path.exists(JOURNAL), "journal left behind")


class ExitBySignal(Exit):
    """A crash stops *at the faulting instruction* (with the registers and source line of the crash); the death
    itself comes on the next `continue`."""
    app_args = ("segv",)

    def test_normal_exit_reports_the_exit_code(self):
        out = self.finish_session("continue\nbt 1\ninfo registers pc\ncontinue\n")
        self.assertRegex(out, r"Program received signal SIGSEGV")
        self.assertRegex(out, r"main \(.*\) at lxexit\.c:\d+")          # the crashing source line is shown
        self.assertRegex(out, r"terminated with signal SIGSEGV")

    test_a_breakpoint_that_was_never_hit_is_removed_before_the_process_dies = None


class ExitByAbort(Exit):
    """abort() is an asynchronous signal (tgkill), not a fault: it is stopped on just the same."""
    app_args = ("abort",)

    def test_normal_exit_reports_the_exit_code(self):
        out = self.finish_session("continue\nbt 3\ncontinue\n")
        self.assertRegex(out, r"Program received signal SIGABRT")
        self.assertRegex(out, r"terminated with signal SIGABRT")

    test_a_breakpoint_that_was_never_hit_is_removed_before_the_process_dies = None


class ExitWithWorkersAlive(Exit):
    """exit(6) from one thread while two workers still run: the code is the group's, seen at the first thread."""
    app_args = ("workers",)

    def test_normal_exit_reports_the_exit_code(self):
        out = self.finish_session()
        self.assertRegex(out, r"exited with code 06")

    test_a_breakpoint_that_was_never_hit_is_removed_before_the_process_dies = None


class ExitOfTheLastThread(Exit):
    app_args = ("thread",)

    def test_normal_exit_reports_the_exit_code(self):
        """A worker thread ends first (not the process); only the final exit(5) is reported."""
        out = self.finish_session()
        self.assertRegex(out, r"exited with code 05")

    test_a_breakpoint_that_was_never_hit_is_removed_before_the_process_dies = None


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(NCORES == 4, "lxspin.c は 4 コア（Pi 2B）の試験。Zynq の時間差は gateware/tests/hil/test_zynq_cti_skew_hw.py")
class HaltSkew(HwCase):
    """lxspin.c: one spinning thread per core stamps g_ts[core] with the time. After an interrupt (all cores halted)
    max(g_ts)-min(g_ts) is how far apart the cores stopped. BCM2836 has no CTI, so this is bounded by the time
    the halt requests take to issue (a few ms), not zero; the bound below is a regression guard against going
    back to one halt-and-poll (~33 ms) per core."""
    app = "lxspin"
    cflags = "-pthread"

    def test_cores_stop_within_a_few_milliseconds(self):
        skews = []
        for _ in range(3):
            out = self.session("continue\n" + "".join('printf "@@t%d=%%llu\\n", g_ts[%d]\n' % (i, i) for i in range(4))
                               + "detach\n", interrupt_after=20)
            self.assertEqual([len(values(out, "t%d" % i)) for i in range(4)], [1] * 4, out[-1500:])
            ts = [values(out, "t%d" % i)[0] for i in range(4)]
            skews.append((max(ts) - min(ts)) / 1e6)
        print("halt skew (ms) per trial:", ["%.3f" % s for s in skews])
        self.assertLess(sorted(skews)[1], 8.0, skews)          # median

    def test_latency_from_the_breakpoint_hit_to_the_last_core_halting(self):
        """Core 0 stamps g_hit just before calling trigger() (breakpoint). Other cores' g_ts minus g_hit is how long
        they kept running after the event, which is what matters when inspecting a race."""
        late, stops = [], []
        for _ in range(3):
            out = self.session("break trigger\ncontinue\n" + 'printf "@@hit=%llu\\n", g_hit\n'
                               + "".join('printf "@@t%d=%%llu\\n", g_ts[%d]\n' % (i, i) for i in range(4))
                               + "monitor stops\ndetach\n")
            self.assertTrue(values(out, "hit"), out[-1500:])
            hit = values(out, "hit")[0]
            late.append(max(values(out, "t%d" % i)[0] for i in range(4)) - hit)
            # その回の止めの方式と根拠（monitor stops の 2 行）。遅れが大きい回の理由を事実で追えるように残す
            stops.append(" / ".join(line.strip() for line in out.splitlines()
                                    if line.strip().startswith(("last stop:", "basis:"))))
        print("last core ran this long after the event (ms):", ["%.3f" % (x / 1e6) for x in late])
        for x, s in zip(late, stops):
            print("  %.3f ms: %s" % (x / 1e6, s))
