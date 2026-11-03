"""実機の試験: linux.halt.debug.*（AI が JTAG の全コア停止で止めて調べる。HaltSession.debug_*）を、2 スレッドのデータ競合の
アプリ（tests/data/linux_halt/race.c を -DRACE_PTHREAD で作ったもの）で、端から端まで:
行にブレークポイント → 当たる → 全コア停止 → 両スレッドの行と変数を 1 回で読む → 再開 を N 回 → break.remove → 再開 → close。

    LXDBG_HW=1 python -m pytest tests/linux_halt/hw/test_hw_debug.py -v -s

環境は harness.py の説明と同じ（LXDBG_BACKEND=fprobe・LXDBG_PART・LXDBG_PROBE_ETH 等。FPGA の停止の見張りは
LXDBG_FPGA_HALT_WATCH_*、--fpga-request-times は LXDBG_EXTRA_FLAGS）。この試験だけの値（既定なし。無ければ名前で言う）:
  LXDBG_DEBUG_ROUNDS       当たりを何回確かめるか（例 5）
  LXDBG_DEBUG_WAIT_S       1 回の run で当たりを待つ上限（秒。例 30）
  LXDBG_DEBUG_MAX_HALT_S   止めたまま放置しない上限（秒。attach の max_halt_s。例 120）
任意: LXDBG_EXPECT_FPGA_TIMED=1 なら、どの当たりの停止も FPGA の時刻の上限（basis に fpga-timed、上限が null でない）で
あることを確かめる。付けてよい構成: FPGA の停止の見張り（LXDBG_FPGA_HALT_WATCH_*。DAP_LINE_TIME の回路）か、見張りなしで
--fpga-request-times（LXDBG_EXTRA_FLAGS）。どちらも無い構成では上限は PC の時計で、basis に理由が出る（付けない）。
実機の結果（2026-11-03、e7837b7、PYNQ-Z2 → Raspberry Pi 2B、LXDBG_DEBUG_ROUNDS=5、10 回とも run・threads・break.remove・
debug.close・halt.close が通り、Pi は無事）:
  FPGA の見張りあり: skew_upper_bound_ns 40800〜61270（basis: fpga halt watch + fpga-timed）、bound_without_assumptions_ns 約 9 ms
  （仮定なしは PC の時計: 再開から全コアが止まったのを読むまで）。
  見張りなし・--fpga-request-times: 1055100〜1554120（basis: fpga-timed (stop by a core)）、仮定なしで約 2.1〜2.6 ms（FPGA の時刻）。
アプリは WSL の arm-linux-gnueabihf-gcc で作る（動的リンクの PIE: 読み込み位置のずれと共有ライブラリの鎖も確かめる）。
各回の止めの事実（方式・上限・根拠）と両スレッドの値を -s で表示する（数ではなく事実として残す）。
"""
import os
import subprocess
import unittest

from .harness import HOST, JOURNAL, NCORES, Rig, backend_flags, required, wslpath

HW = os.environ.get("LXDBG_HW") == "1"
SRC_DIR = os.path.join(HOST, "tests", "data", "linux_halt")
APP = "race_hw"
REMOTE = "/tmp/" + APP


def values(entries):
    return {e["name"]: e["value"].get("value") for e in entries or []}


@unittest.skipUnless(HW, "set LXDBG_HW=1 to run on the real board")
class LinuxHaltDebug(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = Rig()
        cls.rig.prepare_pi()
        cls.elf = os.path.join(SRC_DIR, APP)
        subprocess.run(["wsl", "-d", "Ubuntu", "--", "bash", "-c",
                        "cd %s && arm-linux-gnueabihf-gcc -g -O0 -marm -pthread -DRACE_PTHREAD -o %s race.c"
                        % (wslpath(SRC_DIR), APP)], check=True)
        cls.rig.deploy(cls.elf, REMOTE)

    def setUp(self):
        self.pid = self.rig.start_app(REMOTE)

    def tearDown(self):
        self.rig.cleanup()
        self.rig.kill_app(APP)

    def open_session(self):
        from fprobe.linux_halt import cli
        args = cli.parse(["status", *backend_flags()])
        return cli.open_session(args)

    def test_line_breakpoint_all_stop_read_both_threads_resume_close(self):
        rounds = int(required("LXDBG_DEBUG_ROUNDS"))
        wait_s = float(required("LXDBG_DEBUG_WAIT_S"))
        expect_fpga = os.environ.get("LXDBG_EXPECT_FPGA_TIMED") == "1"
        if os.path.exists(JOURNAL):
            os.remove(JOURNAL)
        session, close = self.open_session()
        closed = None
        facts = []
        try:
            a = session.debug_attach(pid=self.pid, elf=self.elf, skew_limit_ns=int(required("LXDBG_SKEW_LIMIT_NS")),
                                     thread_id="stack", kernel_isa="arm", max_step_retries=8, bp_unmapped="error",
                                     max_halt_s=float(required("LXDBG_DEBUG_MAX_HALT_S")))
            print("attach: image %s; libraries %s" % (a["image"], a["libraries"]))
            self.assertTrue(a["image"]["phdr_match"] and a["image"]["entry_match"], a["image"])
            self.assertEqual(len(a["threads"]), 3, a["threads"])               # main + worker × 2
            names = [lib["name"] for lib in a["libraries"]["libraries"] or []]
            self.assertTrue(any("libc" in n for n in names), a["libraries"])  # 動的リンク: 鎖をたどれた
            bp = session.debug_break_insert(location="race.c:11", kind="sw")
            self.assertEqual((bp["function"], bp["line"]), ("add_one", 11), bp)
            workers = set()
            for k in range(rounds):
                stop = session.debug_run(wait_s=wait_s)
                self.assertEqual((stop["kind"], stop["line"], stop["breakpoint"]), ("swbreak", 11, bp["id"]), stop)
                self.assertEqual(stop["stop"]["cores_halted"], list(range(NCORES)), stop)
                t = session.debug_threads(variables=True, expressions=["counter"], max_elements=16)
                rows = [r for r in t["threads"] if r["function"] in ("add_one", "worker")]
                self.assertEqual(len(rows), 2, t["threads"])                   # 2 つの worker が同じ時点で読めた
                counters = {r["tid"]: r["expressions"][0]["value"]["value"] for r in t["threads"]}
                self.assertEqual(len(set(counters.values())), 1, counters)     # 大域変数は全スレッドで同じ時点の値
                hit = next(r for r in rows if r["tid"] == stop["tid"])
                self.assertEqual((hit["line"], hit["on_core"] is not None), (11, True), hit)
                self.assertTrue({"before", "after"} <= set(values(hit["locals"])), hit)
                workers.add(stop["tid"])
                s = stop["stop"]
                facts.append(s)
                print("round %d: tid %d hit; bound %s ns (%s, without assumptions %s ns); basis: %s"
                      % (k, stop["tid"], s["skew_upper_bound_ns"], s["skew_verdict"], s["bound_without_assumptions_ns"],
                         s["basis"]))
                for r in rows:
                    print("   tid %d core %s %s:%s args %s locals %s counter %s" % (
                        r["tid"], r["on_core"], r["function"], r["line"], values(r["arguments"]), values(r["locals"]),
                        counters[r["tid"]]))
                if expect_fpga:
                    self.assertIn("fpga-timed", s["basis"], s)
                    self.assertIsNotNone(s["skew_upper_bound_ns"], s)
            print("threads that hit: %s" % sorted(workers))
            session.debug_break_remove(id=bp["id"])
            self.assertEqual(session.debug_run(wait_s=0)["kind"], "timeout")
            closed = session.debug_close()
            self.assertEqual(closed["cleanup_errors"], [], closed)
        finally:
            if closed is None and session.debugging:
                session.debug_close()
            close()
        self.assertEqual(self.rig.cores_running(), ["running"] * NCORES)
        clean, detail = self.rig.text_clean(self.pid, REMOTE)
        self.assertTrue(clean, "BKPT left in target text: %s" % detail)
        self.assertFalse(os.path.exists(JOURNAL), "journal left behind")
