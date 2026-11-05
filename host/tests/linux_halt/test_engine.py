import struct

import pytest

from fprobe.linux_halt.bp import BKPT, BreakpointTable
from fprobe.linux_halt.core_access import (CoreState, MOE_BKPT_INSN, MOE_EXT, MOE_HALT, MOE_WATCH_ASYNC,
                                           MOE_WATCH_SYNC)
from fprobe.linux_halt.engine import Engine, EngineError, Stop
from fprobe.linux_halt.kernel import Kernel
from fprobe.linux_halt.process import Process
from fprobe.linux_halt.sim_cores import SimCores
from fprobe.linux_halt.watch import WatchTable
from .helpers import PGD, map_page
from .test_kernel import BASE, OFF, kernel_with, layout

MM = BASE + 0x9000
PGD_VA = BASE + PGD                                   # the linear map is the identity here: virtual BASE+x is physical x
SIG = BASE + 0xA000
DO_EXIT = BASE + 0xF000
STACK = {100: BASE + 0x10000, 101: BASE + 0x12000}
EXTRA = dict(pgd=0x10, saved_auxv=0x20, page_offset=BASE, phys_offset=0, linear_end=BASE + 0x100000, do_exit=DO_EXIT,
             signal_flags=0x40,
             live=0x44, group_exit_code=0x48, signal_group_exit=4)
CODE = bytes.fromhex("0000a0e1") * 8
PA = 0x20000
USER = 0x10
SVC = 0x13
SKEW_LIMIT = 68


class World:
    def __init__(self, tmp_path, bound=50, simultaneous=True, fail_halt_on=frozenset(), interrupted=lambda: False,
                 **sim):
        k0, m = kernel_with([(100, [100, 101], "app")])
        self.k = Kernel(m.read, layout(**EXTRA), max_tasks=100, max_threads=16)
        leader = self.k.find_leader(100)
        for t in self.k.threads(leader.addr):
            m.w32(t.addr + OFF["mm"], MM)
            m.w32(t.addr + OFF["stack"], STACK[t.pid])
            m.w32(t.addr + OFF["signal"], SIG)
        m.w32(MM + EXTRA["pgd"], PGD_VA)
        m.w32(SIG + EXTRA["live"], 2)
        self.m = m
        self.cores = SimCores(4, simultaneous=simultaneous, skew_upper_bound_ns=bound, fail_halt_on=fail_halt_on, **sim)
        map_page(self.cores, 0x10000, PA)
        self.cores.write_phys(PA, CODE)
        self.proc = Process(self.k, self.cores, 100, fmt="short")
        self.bps = BreakpointTable(self.cores, journal=str(tmp_path / "j.json"), hw_slots=2, unmapped="error")
        self.bps.attach(100, self.proc.mem)
        self.watches = WatchTable(self.cores, hw_slots=2)
        self.eng = Engine(self.cores, self.k, self.proc, self.bps, skew_limit_ns=SKEW_LIMIT, thread_id="stack",
                          kernel_thumb=False, max_step_retries=8, interrupted=interrupted, watches=self.watches)

    def run(self, core, tid, pc, cpsr=USER, moe=MOE_BKPT_INSN, other_sp=None):
        sp = other_sp if other_sp is not None else STACK[tid] + 8000
        self.cores.states[core] = CoreState(False, pc=pc, cpsr=cpsr, moe=moe, sp_svc=sp)


def test_stop_verdicts_within_exceeded_and_unknown(tmp_path):
    for bound, verdict in [(50, "within"), (68, "within"), (100, "exceeded"), (None, "unknown")]:
        w = World(tmp_path, bound=bound)
        assert w.eng.stop().skew_verdict == verdict


def test_failed_halt_resumes_the_cores_that_halted_and_raises(tmp_path):
    w = World(tmp_path, fail_halt_on=frozenset({2}))
    with pytest.raises(EngineError, match=r"\[2\]"):
        w.eng.stop()
    assert w.cores.halted() == (False, False, False, False)


def test_event_and_interrupt_skew_are_kept_apart(tmp_path):
    flag = {"int": False}
    w = World(tmp_path, bound=40, interrupted=lambda: flag["int"])
    w.run(0, 100, 0x10004)
    w.eng.stop()
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.eng.refresh()
    w.cores.queue_event(0)
    assert w.eng.cont().kind == "swbreak"
    w.cores.states[0] = CoreState(False, pc=0x10010, cpsr=USER, moe=MOE_HALT, sp_svc=STACK[100] + 8000)
    flag["int"] = True
    assert w.eng.cont().kind == "int"
    s = w.eng.stats().skew
    assert s["event"]["stops"] == 1 and s["event"]["max_ns"] == 40
    assert s["interrupt"]["stops"] == 1 and s["interrupt"]["max_ns"] == 40


def test_unknown_bound_is_counted_not_folded_into_the_maximum(tmp_path):
    w = World(tmp_path, bound=None)
    w.eng.stop(kind="event")
    s = w.eng.stats().skew["event"]
    assert s["unknown"] == 1 and s["max_ns"] is None


def test_breakpoint_hit_of_our_thread_is_reported_as_swbreak(tmp_path):
    w = World(tmp_path)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(0, 101, 0x10004)
    w.cores.queue_event(0)
    w.eng.stop()
    stop = w.eng.cont()
    assert (stop.kind, stop.tid, stop.addr) == ("swbreak", 101, 0x10004)


def test_other_process_hitting_a_shared_bkpt_is_stepped_over_counted_and_not_reported(tmp_path):
    """The same physical page is mapped by another process at another address. A core running it hits our BKPT: the engine
    finds the breakpoint by the physical address of that core's pc, steps it over with the original instruction
    showing, counts it, and goes on waiting."""
    w = World(tmp_path)
    map_page(w.cores, 0x7000, PA, pgd=0xC000)                  # TTBR0 needs a 16 KiB aligned table
    w.cores.cp15[(1, "ttbr0")] = 0xC000
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(1, 100, 0x7004, other_sp=BASE + 0x14000 + 100)       # not inside any of our threads' kernel stacks
    w.cores.queue_event(1)
    seen = {}

    def on_step(core):
        seen["during"] = w.cores.read_phys(PA + 4, 4)
        w.run(0, 100, 0x10004)                                  # then our own thread reaches the breakpoint
        w.cores.queue_event(0)
    w.cores.on_step = on_step
    w.eng.stop()
    stop = w.eng.cont()
    assert seen["during"] == CODE[4:8]                           # the original instruction was visible for the step
    assert w.cores.read_phys(PA + 4, 4) == BKPT[4]               # and the BKPT is back
    assert w.cores.steps == [1]
    assert w.eng.stats().counters["foreign_bp_hits"] == 1
    assert stop.kind == "swbreak" and stop.tid == 100


def test_two_simultaneous_hits_report_one_and_keep_the_other_pending(tmp_path):
    w = World(tmp_path)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.bps.insert(100, 0x10008, kind="sw", width=4)
    w.run(0, 100, 0x10004)
    w.run(2, 101, 0x10008)
    w.cores.queue_event(0, 2)
    w.eng.stop()
    first = w.eng.cont()
    resumes = len(w.cores.resumed)
    second = w.eng.cont()
    assert (first.tid, second.tid) == (100, 101)
    assert len(w.cores.resumed) == resumes                       # the pending hit was reported without running anything


def test_step_of_a_live_thread_runs_one_instruction_on_its_core_only(tmp_path):
    w = World(tmp_path)
    w.run(0, 100, 0x10000, moe=MOE_HALT)
    w.cores.fetches[(0, 0x10000)] = 0xE1A00000                   # mov r0, r0: not an svc
    w.cores.on_step = lambda core: w.run(core, 100, 0x10004, moe=MOE_HALT)
    w.eng.stop()
    stop = w.eng.step(100)
    assert (stop.kind, stop.tid, stop.addr) == ("trap", 100, 0x10004)
    assert w.cores.steps == [0]


def test_lone_thread_exit_reports_exit_with_r0(tmp_path):
    w = World(tmp_path)
    w.m.w32(SIG + EXTRA["live"], 1)
    w.run(0, 100, DO_EXIT, cpsr=SVC, moe=MOE_BKPT_INSN)
    w.cores.set_regs(0, [7] + [0] * 16)
    w.cores.queue_event(0)
    w.eng.stop()
    stop = w.eng.cont()
    assert (stop.kind, stop.tid, stop.code) == ("exit", 100, 7)


def test_fatal_signal_is_a_stop_first_and_the_death_comes_on_the_next_resume(tmp_path):
    w = World(tmp_path)
    w.m.w32(SIG + EXTRA["signal_flags"], 4)                      # SIGNAL_GROUP_EXIT
    w.m.w32(SIG + EXTRA["group_exit_code"], 11)                  # SIGSEGV
    w.run(0, 100, DO_EXIT, cpsr=SVC, moe=MOE_BKPT_INSN)
    w.cores.queue_event(0)
    w.eng.stop()
    first = w.eng.cont()
    assert (first.kind, first.signal) == ("signal", 11)
    resumes = len(w.cores.resumed)
    second = w.eng.cont()
    assert (second.kind, second.code) == ("exit", 11) and len(w.cores.resumed) == resumes


def test_close_leaves_no_bkpt_no_hw_and_all_cores_running(tmp_path):
    w = World(tmp_path)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.bps.insert(100, 0x10010, kind="hw", width=4)
    w.watches.add(0x1000, 4, "w")
    w.eng.stop()
    assert w.eng.close() == []
    assert w.cores.read_phys(PA, 32) == CODE
    assert w.cores.hw_bps == {} and w.cores.hw_watches == {}
    assert w.cores.halted() == (False,) * 4
    assert w.bps.pending_journal() == []


def test_close_after_a_failure_while_the_target_runs_still_removes_every_bkpt(tmp_path):
    # 実機: 走らせている間の JTAG の失敗で attach が終わると、close はコアが走ったまま呼ばれる。BKPT を外すにはメモリを
    # 書くのでコアを止める必要がある（止めずに書こうとして断られ、BKPT がメモリに残って Pi が固まった。2026-11-02）
    w = World(tmp_path)
    w.eng.stop()
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.bps.insert(100, 0x10010, kind="hw", width=4)
    w.cores.resume_all()
    w.cores.memory_needs_halt = True
    assert w.eng.close() == []
    w.cores.memory_needs_halt = False
    assert w.cores.read_phys(PA, 32) == CODE
    assert w.cores.hw_bps == {}
    assert w.cores.halted() == (False,) * 4
    assert w.bps.pending_journal() == []


def test_foreign_breakpoint_handling_refuses_other_formats_instead_of_guessing(tmp_path):
    w = World(tmp_path)
    w.proc.mem.fmt = "lpae"
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(1, 100, 0x7004, other_sp=BASE + 0x14000 + 100)
    w.cores.queue_event(1)
    w.eng.stop()
    with pytest.raises(EngineError, match="short"):
        w.eng.cont()


def test_synchronous_watch_steps_with_watches_lifted_and_names_the_range_that_changed(tmp_path):
    w = World(tmp_path)
    w.watches.add(0x10100, 4, "w")
    w.watches.add(0x10200, 4, "w")
    w.run(0, 100, 0x10020, moe=MOE_HALT)

    def on_step(core):
        assert not any(k[0] == core for k in w.cores.hw_watches)      # lifted on the stepped core, for the step
        w.cores.write_phys(PA + 0x200, b"\x01\x00\x00\x00")           # the stepped instruction stores to the second range
        w.run(core, 100, 0x10024, moe=MOE_HALT)
    w.cores.on_step = on_step
    w.cores.queue_event(0, moe=MOE_WATCH_SYNC)
    w.eng.stop()
    stop = w.eng.cont()
    assert (stop.kind, stop.tid, stop.addr, stop.access, stop.candidates) == ("watch", 100, 0x10200, "w", 1)
    assert (0, 0) in w.cores.hw_watches and (0, 1) in w.cores.hw_watches   # restored afterwards


def test_a_watch_that_changed_nothing_names_every_registered_range_as_a_candidate(tmp_path):
    w = World(tmp_path)
    w.watches.add(0x10100, 4, "w")
    w.watches.add(0x10200, 4, "w")
    w.run(0, 100, 0x10020, moe=MOE_HALT)
    w.cores.on_step = lambda core: w.run(core, 100, 0x10024, moe=MOE_HALT)
    w.cores.queue_event(0, moe=MOE_WATCH_SYNC)
    w.eng.stop()
    stop = w.eng.cont()
    assert (stop.kind, stop.candidates) == ("watch", 2)             # the hardware does not say which; the count says so


def test_kernel_mode_and_foreign_watch_hits_are_counted_not_reported(tmp_path):
    w = World(tmp_path)
    w.watches.add(0x10100, 4, "w")
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(0, 100, 0x10020, cpsr=SVC)                              # the kernel touching our memory for thread 100
    w.run(1, 100, 0x7000, other_sp=BASE + 0x14000 + 100)          # another process
    w.cores.queue_event(0, 1, moe=MOE_WATCH_ASYNC)
    w.eng.stop()

    def then_our_thread_hits_the_breakpoint(core):
        w.run(2, 101, 0x10004)
        w.cores.queue_event(2)
    w.cores.on_step = then_our_thread_hits_the_breakpoint
    w.cores.queue_event(2)                                         # (placeholder replaced below)
    w.cores.events.pop()                                           # keep only the watch event; the hit follows the resume
    w.run(2, 101, 0x10004)
    w.cores.events.append(((2,), MOE_BKPT_INSN))
    stop = w.eng.cont()
    c = w.eng.stats().counters
    assert (c["kernel_mode_watch_hits"], c["foreign_watch_hits"]) == (1, 1)
    assert (stop.kind, stop.tid) == ("swbreak", 101)


# --- findings of the whole-branch review --------------------------------------------------------------------------------

def test_the_recovery_after_a_failed_halt_keeps_resuming_cores_a_backend_parked(tmp_path):
    """review finding 3: a resume() that parks the rest must not leave the halted cores halted."""
    w = World(tmp_path, fail_halt_on=frozenset({3}), park_after_first=True)
    with pytest.raises(EngineError, match=r"\[3\]") as e:
        w.eng.stop()
    assert w.cores.halted() == (False, False, False, False)
    assert "resumed" in str(e.value) and "STILL HALTED" not in str(e.value)


def test_the_recovery_says_so_when_it_could_not_resume(tmp_path):
    w = World(tmp_path, fail_halt_on=frozenset({3}), fail_resume=RuntimeError("link down"))
    with pytest.raises(EngineError) as e:
        w.eng.stop()
    assert "link down" in str(e.value) and "may still be halted" in str(e.value)


def test_a_backend_exception_inside_halt_all_still_resumes_whatever_halted(tmp_path):
    w = World(tmp_path, raise_after_halt=RuntimeError("usb stalled"))
    with pytest.raises(EngineError, match="usb stalled"):
        w.eng.stop()
    assert w.cores.halted() == (False, False, False, False)


def test_a_core_halted_by_the_cti_request_is_not_taken_for_one_that_stopped_by_itself(tmp_path):
    """review finding 8b: MOE_EXT (CTI/EDBGRQ) is a requested halt; a thread caught exactly on an unexecuted breakpoint
    must keep its hit, not be stepped over while another process's hit is handled."""
    w = World(tmp_path, halt_moe=MOE_EXT)
    map_page_for_foreign(w)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(0, 100, 0x10004, moe=MOE_HALT)                          # our thread, about to execute the BKPT
    w.run(1, 100, 0x7004, other_sp=BASE + 0x14000 + 100)          # another process hits the shared BKPT
    w.cores.queue_event(1)
    w.eng.stop()

    def then_a_real_event(core):
        w.run(2, 101, 0x10008)
        w.cores.queue_event(2)
    w.cores.on_step = then_a_real_event
    w.bps.insert(100, 0x10008, kind="sw", width=4)
    stop = w.eng.cont()
    assert w.cores.steps == [1]                                     # only the foreign core was stepped, never core 0
    assert (stop.kind, stop.tid) == ("swbreak", 101)


def map_page_for_foreign(w):
    from .helpers import map_page
    map_page(w.cores, 0x7000, PA, pgd=0xC000)
    w.cores.cp15[(1, "ttbr0")] = 0xC000


def test_the_step_retry_limit_has_no_default(tmp_path):
    w = World(tmp_path)
    with pytest.raises(TypeError):
        Engine(w.cores, w.k, w.proc, w.bps, skew_limit_ns=68, thread_id="stack", kernel_thumb=False)


def test_the_registers_of_a_thread_that_is_not_on_a_core_are_read_and_written_through_read_kernel(tmp_path):
    """review finding 9 / VMAP_STACK: the saved pt_regs sit on a kernel stack that may be in vmalloc, so they are reached
    through the core's MMU (read_kernel/write_kernel), not by a linear-map offset."""
    w = World(tmp_path, kernel_base=BASE)
    w.eng.stop()
    stack = STACK[101]
    regs_at = stack + 8192 - 8 - 72                                  # task_pt_regs: top of the stack - 8 - sizeof(pt_regs)
    w.cores.write_phys(regs_at - BASE, struct.pack("<18I", *range(100, 118)))
    assert w.eng.get_regs(101) == list(range(100, 117))              # r0-r15 + cpsr (orig_r0 dropped)
    new = list(range(500, 517))
    w.eng.set_regs(101, new)
    assert struct.unpack("<18I", w.cores.read_phys(regs_at - BASE, 72))[:17] == tuple(new)
    assert struct.unpack("<18I", w.cores.read_phys(regs_at - BASE, 72))[17] == 117     # orig_r0 untouched


def test_waiting_for_an_instruction_that_trapped_uses_a_hardware_breakpoint_so_the_kernel_reads_the_original_instruction(tmp_path):
    """ステップで例外に入った命令（VFP の初回使用の未定義命令など）に戻るのを待つ間、カーネルは例外の処理でその命令をメモリから読み直す。
    BKPT に書き換えていると SIGILL になる（実機 2026-11-05: data_types の step）。メモリを書き換えないハードウェアのブレークポイントを使い、
    空きが無ければソフトウェアに戻り、どちらでも終わりに必ず外す。"""
    from fprobe.linux_halt.engine_types import Stop
    w = World(tmp_path)
    seen = {}

    def cont(want, deadline=None, clock=None):
        seen["hw"] = w.bps.get_hw(0x10000) is not None
        seen["sw"] = w.bps.sw_at_pa(PA) is not None
        return Stop("trap", 101, 0x10000), True

    w.eng._cont_until = cont
    w.eng._run_to(0x10000, 4, 101, prefer_hw=True)
    assert seen == {"hw": True, "sw": False}                       # メモリは書き換えない
    assert w.bps.get_hw(0x10000) is None and w.bps.sw_at_pa(PA) is None   # 終わりに外す
    w.eng._run_to(0x10000, 4, 101)                                  # 既定は従来どおりソフトウェア
    assert seen == {"hw": False, "sw": True}
    w.bps.insert(100, 0x10100, kind="hw", width=4)                  # ハードウェアの空きを使い切る（2 本）
    w.bps.insert(100, 0x10200, kind="hw", width=4)
    w.eng._run_to(0x10000, 4, 101, prefer_hw=True)
    assert seen == {"hw": False, "sw": True}                       # 空きが無ければソフトウェアに戻る
    assert w.bps.sw_at_pa(PA) is None
