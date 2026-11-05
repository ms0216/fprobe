"""fprobe's own backend: four Cortex-A cores through one DAP, against the fprobe fakes (no instrument)."""
import time

import pytest
from arm_a_fakes import ApbAp, ArmCore, HALTED, HDBGEN, V7DebugBlock, bcm2836
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from jtag_fakes import FakeChain, Tap

from fprobe.arm_a_target import arm_a_part
from fprobe.linux_halt.backend_arma import ArmACores
from fprobe.linux_halt.core_access import CoreState, MOE_BKPT_EVT, OutsideLinearMap

LINEAR = dict(page_offset=0xC0000000, phys_offset=0, linear_end=0xC8000000)
IDCODE = 0x4BA00477


class Clock:
    """Time that moves only when a test says so: the bounds can be asserted exactly."""

    def __init__(self, t=0):
        self.t = t

    def __call__(self):
        return self.t


def make(cores=None, clock=None):
    cores = cores or [ArmCore(mpidr=0x80000F00 | i) for i in range(4)]
    apb, _ = bcm2836(cores, True)
    target = FakeTarget(dpidr=IDCODE, swd=False, jtag_dp_index=0, aps={0: FakeMemAp(), 1: apb},
                        jtag_chain=FakeChain([Tap(4, IDCODE)]))
    link = FakeDapLink(target)
    extra = {"clock": clock} if clock is not None else {}
    return ArmACores.open(link, arm_a_part("bcm2836"), frequency_hz=1_000_000, **LINEAR, **extra), cores, link


def test_open_finds_all_four_cores_enables_halting_debug_and_leaves_them_running():
    a, cores, _ = make()
    assert a.n_cores == 4
    assert all(c.ctrl & HDBGEN and not c.halted for c in cores)
    assert a.halted() == (False,) * 4
    assert (a.hw_slots, a.watch_slots) == (5, 4)                      # 6 breakpoint pairs, the last one is for stepping


def test_halt_all_requests_every_core_and_reports_a_sequential_stop_with_a_host_timed_bound():
    a, cores, _ = make()
    report = a.halt_all()
    assert (report.method, report.cores_halted) == ("sequential", (0, 1, 2, 3))
    assert isinstance(report.skew_upper_bound_ns, int) and report.skew_upper_bound_ns >= 0
    assert "known" in report.basis
    assert all(c.halted for c in cores)


def test_nothing_to_halt_means_an_unknown_bound_not_zero():
    a, cores, _ = make()
    a.halt_all()
    assert a.halt_all().skew_upper_bound_ns is None


def test_a_core_that_will_not_halt_is_left_out_of_cores_halted():
    a, cores, _ = make()
    cores[2].ctrl &= ~HDBGEN                                          # ignores halt requests
    a._cores[2].timeout_s = 0.01
    assert a.halt_all().cores_halted == (0, 1, 3)


def test_a_core_that_stops_at_once_after_the_restart_leaves_no_core_parked_and_is_found_by_wait_event():
    """全コアの再開の要求は 1 回の往復でまとめて書く: 途中で止める「残り」が無い（parked は空）。すぐ止まったコアは、次の
    wait_event が見つけて他を止める（gdb の all-stop）。"""
    a, cores, _ = make()
    a.halt_all()
    restart = cores[1].restart

    def restart_then_core_0_hits():
        restart()
        cores[0].halted, cores[0].moe = True, MOE_BKPT_EVT           # the core restarted first reaches a breakpoint
    cores[1].restart = restart_then_core_0_hits
    a.resume(range(4))
    assert a.parked == set() and not any(c.halted for c in cores[1:])
    assert a.wait_event(0.5) == (0,) and all(c.halted for c in cores)


def test_resume_restores_every_cores_registers_before_any_restart_request():
    """時間のかかる所（レジスタを戻す）は、どのコアも止まったままの間に済ませる。"""
    import fprobe.arm_debug as arm_debug
    a, cores, _ = make()
    a.halt_all()
    seen, leave_all = [], arm_debug.leave_all

    def recording_leave(debug_cores, mask_interrupts):
        seen.append((len(debug_cores), tuple(k.halted for k in cores)))
        leave_all(debug_cores, mask_interrupts)
    arm_debug.leave_all = recording_leave
    try:
        a.resume_all()
    finally:
        arm_debug.leave_all = leave_all
    assert seen == [(4, (True,) * 4)]                                 # 全コアのレジスタをまとめて戻す（全コアが止まったまま）
    assert not any(c.halted for c in cores)


def test_resume_writes_every_cores_restart_request_and_reads_their_dscr_in_one_round_trip():
    """再開の要求の間の往復が、再開の時刻の差になる（その間にブレークポイントに当たると、FPGA の見張りがまだ効かない）。
    全コアの要求と、再開したかの確かめ（DSCR）を 1 回の往復で。"""
    a, cores, link = make()
    a.halt_all()
    seen = watch_flushes(link, cores)
    a.resume_all()
    assert all(s in ((True,) * 4, (False,) * 4) for s in seen), seen   # 一部のコアだけ走っている往復は無い
    assert sum(1 for s in seen if not any(s)) == 1, seen               # 再開の後の往復は 1 回だけ


def test_a_core_that_has_not_left_debug_state_yet_is_read_again_not_taken_for_stopped():
    """DSCR.RESTARTED が 0（要求は届いたがまだデバッグ状態を出ていない）なら止まったままに見える。すぐ止まったコア
    （HALTED と RESTARTED が両方 1）と取り違えず、読み直す。"""
    a, cores, _ = make()
    a.halt_all()
    restart, dscr = cores[2].restart, cores[2].dscr
    late = []

    def slow_restart():
        cores[2].restarted = False                                    # 要求で RESTARTED は 0 に。まだ止まっている
        late.append(1)

    def dscr_then_leave_debug_state():
        value = dscr()
        if late:
            late.clear()
            restart()
        return value
    cores[2].restart, cores[2].dscr = slow_restart, dscr_then_leave_debug_state
    a.resume_all()
    assert a.parked == set() and not any(c.halted for c in cores)


def test_a_core_that_never_leaves_debug_state_is_named_and_left_parked():
    """要求の後もデバッグ状態を出ないコアは、名前を添えて断り、`parked`（再開されずに止まったまま）に残す: 次の停止の上限は「分からない」。"""
    from fprobe.arm_debug import ArmDebugError
    a, cores, _ = make()
    a.halt_all()
    cores[2].restart = lambda: setattr(cores[2], "restarted", False)
    a._cores[2].timeout_s = 0.01
    with pytest.raises(ArmDebugError, match=r"\[2\]"):
        a.resume_all()
    assert a.parked == {2} and cores[2].halted and not any(c.halted for c in (cores[0], cores[1], cores[3]))
    assert a.halt_all().skew_upper_bound_ns is None


def test_resume_of_a_core_that_is_running_restarts_nothing():
    """止まっていないコアを求めたら、どのコアの再開の要求も書かずに断る（一部だけ走らせない）。"""
    from fprobe.arm_debug import ArmDebugError
    a, cores, _ = make()
    a.halt_all()
    cores[3].halted = False
    a._cores[3].state()                                               # デバッガも走っているのを見た
    with pytest.raises(ArmDebugError):
        a.resume_all()
    assert all(c.halted for c in cores[:3]) and all(c.restarts == 0 for c in cores)


def test_wait_event_returns_the_stopped_core_requests_the_others_and_remembers_the_timing():
    a, cores, _ = make()
    a.halt_all()
    a.resume_all()
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    assert a.wait_event(0.5) == (2,)
    assert all(c.halted for c in cores)                               # the others were asked to halt at once
    report = a.halt_all()                                             # completes the stop
    assert report.skew_upper_bound_ns is not None


def watch_flushes(link, cores):
    """flush（USB の往復。積んだ転送のあるものだけ）ごとに、各コアが止まっているかを記録する。"""
    seen = []
    flush = link.flush

    def recording_flush():
        before = link.flushes
        flush()
        if link.flushes != before:
            seen.append(tuple(c.halted for c in cores))
    link.flush = recording_flush
    return seen


def test_halt_all_sends_every_cores_halt_request_in_one_round_trip():
    """CTI の無い SoC の順の停止: 停止要求の間の USB の往復が、コアの止まる時刻の差になる。全コアの要求を 1 回の往復で出す。"""
    a, cores, link = make()
    seen = watch_flushes(link, cores)
    a.halt_all()
    assert all(s in ((False,) * 4, (True,) * 4) for s in seen), seen   # 一部のコアだけ止まった往復は無い


def test_the_bound_ends_when_every_core_was_seen_halted_not_after_their_state_was_read():
    """上限の終わり = 全コアが止まっているのを読んだ時刻。その後のレジスタ等の読み出しの時間は、止まる時刻の差ではない。"""
    clock = Clock(1000)
    a, cores, _ = make(clock=clock)
    for c in a._cores:
        halt = c.halt

        def slow_halt(halt=halt):
            clock.t += 1_000_000                                       # 状態の読み出しにかかる時間
            halt()
        c.halt = slow_halt
    report = a.halt_all()
    assert report.cores_halted == (0, 1, 2, 3) and report.skew_upper_bound_ns == 0


def test_wait_event_asks_the_other_cores_to_halt_in_one_round_trip():
    a, cores, link = make()
    a.halt_all()
    a.resume_all()
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    seen = watch_flushes(link, cores)
    assert a.wait_event(0.5) == (2,)
    assert a.last_wait == {"hit": (2,), "requested": (0, 1, 3), "by": "host"}
    assert all(s in ((False, False, True, False), (True,) * 4) for s in seen), seen


def test_wait_event_times_out_with_nothing():
    a, cores, _ = make()
    assert a.wait_event(0.02) == ()


def test_snapshot_reads_state_context_entry_reason_and_banked_stack():
    c0 = ArmCore(cpsr=0x10, cp15={"contextidr": 0x2A05}, bank_sp={0x13: 0xC0123450}, moe=1)
    c0.regs[15] = 0x8000
    a, cores, _ = make([c0] + [ArmCore(mpidr=0x80000F00 | i) for i in range(1, 4)])
    a.halt_all()
    cores[0].moe = 1
    a._cores[0].state()                                               # (the fake halted by request: set the reason after)
    cores[1].halted = False
    snap = a.snapshot([0, 1])
    assert snap[0].halted and snap[0].pc == 0x8000 and snap[0].cpsr == 0x10
    assert (snap[0].ctx, snap[0].sp_svc, snap[0].tid) == (0x2A05, 0xC0123450, 0x2A)
    assert snap[1] == CoreState(False)


def test_registers_roundtrip_through_the_cores_dcc():
    a, cores, _ = make()
    a.halt_all()
    vals = list(range(13)) + [0xC0001000, 0xC0002000, 0x8000, 0x13]
    a.set_regs(1, vals)
    assert a.regs(1) == vals
    a.write_reg(1, "r5", 0x55)
    assert a.read_reg(1, "r5") == 0x55


def test_sp_svc_is_read_from_user_mode_without_disturbing_the_user_registers():
    c = ArmCore(cpsr=0x10, bank_sp={0x13: 0xC0ABCDE0})
    c.regs[13] = 0x7EFFF000
    a, cores, _ = make([c] + [ArmCore(mpidr=0x80000F00 | i) for i in range(1, 4)])
    a.halt_all()
    assert a.read_reg(0, "sp_svc") == 0xC0ABCDE0
    assert a.read_reg(0, "sp") == 0x7EFFF000 and c.cpsr == 0x10


def test_physical_memory_goes_through_the_linear_map_of_core_0():
    a, cores, _ = make()
    a.halt_all()
    a.write_phys(0x1000, bytes(range(16)))
    assert bytes(cores[0].memory[0xC0001000 + i] for i in range(16)) == bytes(range(16))
    assert a.read_phys(0x1000, 16) == bytes(range(16))
    assert a.read_phys(0x1003, 5) == bytes(range(3, 8))                # unaligned start and end
    a.write_phys(0x1001, b"\xAA")
    assert a.read_phys(0x1000, 4) == bytes([0, 0xAA, 2, 3])


def test_cp15_and_fetch_are_per_core():
    c1 = ArmCore(cp15={"ttbr0": 0x40004000}, mpidr=0x80000F01)
    a, cores, _ = make([ArmCore()] + [c1] + [ArmCore(mpidr=0x80000F00 | i) for i in range(2, 4)])
    a.halt_all()
    assert a.read_cp15(1, "ttbr0") == 0x40004000
    for i, v in enumerate((0xE1A00000, 0xE12FFF1E, 0xE3A00001, 0xE3A00002)):
        cores[1].memory[0x9000 + i] = (v >> (8 * (i % 4))) & 0xFF
    cores[1].memory.update({0x9000: 0x00, 0x9001: 0x00, 0x9002: 0xA0, 0x9003: 0xE1})
    assert a.fetch(1, 0x9000, 4) == 0xE1A00000


def test_hardware_breakpoints_and_watchpoints_use_the_cores_own_pairs():
    a, cores, _ = make()
    a.halt_all()
    a.set_hw_bp(1, 2, 0x10004, 4)
    assert cores[1].bvr[2] == 0x10004 and cores[1].bcr[2] & 1
    a.clear_hw_bp(1, 2)
    assert cores[1].bcr[2] == 0
    a.set_hw_watch(1, 0, 0x2000, 4, "rw")
    assert cores[1].wvr[0] == 0x2000 and cores[1].wcr[0] & 1
    a.clear_hw_watch(1, 0)
    assert cores[1].wcr[0] == 0
    with pytest.raises(RuntimeError, match="alignment"):
        a.set_hw_watch(1, 0, 0x2001, 4, "w")                          # a 4-byte watch must be word aligned


def test_step_runs_one_instruction_on_that_core_only():
    a, cores, _ = make()
    cores[0].regs[15] = 0x8000
    a.halt_all()
    a.step(0)
    assert cores[0].regs[15] == 0x8004 and all(c.halted for c in cores)


def test_sync_caches_invalidates_instruction_caches_of_the_named_cores_only():
    a, cores, _ = make()
    a.halt_all()
    a.sync_caches([1])
    assert [op for op, _ in cores[1].cache_ops if op in ("iciallu", "bpiall")] == ["iciallu", "bpiall"]
    assert not any(op in ("iciallu", "bpiall") for op, _ in cores[0].cache_ops)


# --- CTI: simultaneous stop (the CTI driver itself is tested against a register-level model in test_cti.py) ----------

class FakeCross:
    """Stands in for CrossHalt, with the one thing the review showed matters: the halt GATE. release() closes it, rearm_halt()
    opens it; a request or a core's debug-acknowledge route reaches the other cores only while it is open."""

    def __init__(self, cores, events=None):
        self.cores, self.gate_open = cores, True
        self.log = events if events is not None else []

    def arm(self, event_sync_trigin=None):
        self.log.append(("arm", event_sync_trigin))

    def halt_all(self):
        self.log.append("halt_all")
        if self.gate_open:
            for c in self.cores:
                c.halt_request()

    def release(self):
        self.log.append("release")
        self.gate_open = False

    def rearm_halt(self):
        self.log.append("rearm_halt")
        self.gate_open = True

    def hardware_event(self, core):
        """A core stops by itself; its debug acknowledge halts the others in hardware if the gate is open."""
        self.cores[core].halted, self.cores[core].moe = True, MOE_BKPT_EVT
        if self.gate_open:
            for i, c in enumerate(self.cores):
                if i != core:
                    c.halt_request()


def with_cti(event_trigin=5, request_bound=68, event_bound=None, clock=None):
    a, cores, link = make(clock=clock)
    cross = FakeCross(cores)
    a.use_cross_halt(cross, request_skew_upper_bound_ns=request_bound, event_skew_upper_bound_ns=event_bound,
                     event_trigin=event_trigin, hardware_halt_wait_s=0.01)
    return a, cores, cross


def test_the_cross_halt_is_armed_with_the_event_trigger_the_caller_names_and_bounds_and_wait_are_required():
    a, cores, cross = with_cti(event_trigin=5)
    assert cross.log == [("arm", 5)]
    with pytest.raises(TypeError):
        a.use_cross_halt(cross, request_skew_upper_bound_ns=68, event_skew_upper_bound_ns=None, event_trigin=5)


def test_a_requested_stop_through_the_cti_is_simultaneous_when_the_hardware_halted_every_core():
    a, cores, cross = with_cti(request_bound=68)
    sent = []
    for c in a._cores:
        c.port.halt_request = lambda: sent.append(1)
    report = a.halt_all()
    assert (report.method, report.cores_halted, report.skew_upper_bound_ns) == ("simultaneous", (0, 1, 2, 3), 68)
    assert "cti" in report.basis and not sent
    assert cross.log == [("arm", 5), "halt_all"]


def test_if_the_cti_did_not_halt_the_cores_they_are_halted_by_software_and_the_stop_is_reported_sequential():
    """review finding 1: never call a software stop simultaneous."""
    a, cores, cross = with_cti(request_bound=68)
    cross.gate_open = False                                          # the request goes nowhere
    report = a.halt_all()
    assert report.method == "sequential" and report.cores_halted == (0, 1, 2, 3)
    assert "did not halt" in report.basis and report.skew_upper_bound_ns != 68


def test_resume_releases_before_any_restart_and_reopens_the_gate_after_the_last_one():
    events = []
    a, cores, link = make()
    cross = FakeCross(cores, events)
    a.use_cross_halt(cross, request_skew_upper_bound_ns=68, event_skew_upper_bound_ns=None, event_trigin=5,
                     hardware_halt_wait_s=0.01)
    a.halt_all()
    for i, c in enumerate(cores):
        c.restart = (lambda i=i, orig=c.restart: (events.append("restart%d" % i), orig())[1])
    events.clear()
    a.resume_all()
    assert events == ["release", "restart0", "restart1", "restart2", "restart3", "rearm_halt"]
    assert cross.gate_open
    events.clear()
    a.halt_all()
    assert events == ["halt_all"]                                    # the gate is already open: no rearm needed here


def test_an_event_stop_after_a_resume_still_runs_through_the_hardware_route():
    """review finding 1, the exact scenario: halt, resume, then a breakpoint on core 1."""
    a, cores, cross = with_cti(event_trigin=5, event_bound=1234)
    a.halt_all()
    a.resume_all()
    sent = []
    for c in a._cores:
        c.port.halt_request = lambda: sent.append(1)
    cross.hardware_event(1)
    assert 1 in a.wait_event(0.5) and not sent                      # (the others may already show as halted too)
    report = a.halt_all()
    assert (report.method, report.skew_upper_bound_ns) == ("simultaneous", 1234)


def test_a_closed_gate_makes_the_event_stop_sequential_with_the_ledger_bound():
    clock = Clock(1000)
    a, cores, cross = with_cti(event_trigin=5, event_bound=1234, clock=clock)
    a.halt_all()
    clock.t = 2000
    a.resume_all()
    cross.gate_open = False                                          # e.g. a missed rearm
    cross.hardware_event(1)
    clock.t = 2500
    assert a.wait_event(0.5) == (1,)
    clock.t = 9000
    report = a.halt_all()
    assert report.method == "sequential" and report.skew_upper_bound_ns == 9000 - 2000
    assert "did not halt" in report.basis


def test_an_event_stop_whose_hardware_bound_was_not_measured_says_unknown_not_the_request_bound():
    a, cores, cross = with_cti(event_trigin=5, event_bound=None, request_bound=68)
    a.halt_all()
    a.resume_all()
    cross.hardware_event(1)
    a.wait_event(0.5)
    report = a.halt_all()
    assert report.method == "simultaneous" and report.skew_upper_bound_ns is None and "not measured" in report.basis


def test_without_the_event_route_the_others_are_halted_by_software_and_the_stop_is_sequential():
    a, cores, cross = with_cti(event_trigin=None)
    a.halt_all()
    a.resume_all()
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT
    assert a.wait_event(0.5) == (1,)
    assert all(c.halted for c in cores)                              # software requests were issued
    assert a.halt_all().method == "sequential"


# --- the skew bound is an upper bound (review finding 4) ----------------------------------------------------------------

def test_the_bound_reaches_back_to_when_the_cores_were_last_known_not_to_have_halted():
    clock = Clock(1000)
    a, cores, _ = make(clock=clock)
    a.halt_all()
    clock.t = 2000
    a.resume_all()
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT               # it stopped sometime after the restart
    assert a.wait_event(0.5) == (1,)
    clock.t = 9000
    report = a.halt_all()
    assert (report.method, report.skew_upper_bound_ns) == ("sequential", 9000 - 2000)


def test_a_core_that_halted_by_itself_without_the_debugger_noticing_makes_the_bound_unknown():
    clock = Clock(1000)
    a, cores, _ = make(clock=clock)
    a.halt_all()
    clock.t = 2000
    a.resume_all()
    cores[0].halted, cores[0].moe = True, MOE_BKPT_EVT               # halted 200 ms ago, then a ^C arrives
    clock.t = 3000
    report = a.halt_all()
    assert report.skew_upper_bound_ns is None and "0" in report.basis and "already" in report.basis


def test_a_core_that_stops_at_once_after_the_restart_still_has_a_known_bound():
    """まとめて再開したので、どのコアも再開されている（止まったまま残るコアは無い）: 上限は分かる。"""
    clock = Clock(1000)
    a, cores, _ = make(clock=clock)
    a.halt_all()
    clock.t = 2000
    restart = cores[1].restart

    def restart_then_core_0_hits():
        restart()
        cores[0].halted, cores[0].moe = True, MOE_BKPT_EVT
    cores[1].restart = restart_then_core_0_hits
    a.resume(range(4))
    assert a.wait_event(0.5) == (0,)
    clock.t = 9000
    report = a.halt_all()
    assert (report.method, report.skew_upper_bound_ns) == ("sequential", 9000 - 2000)


def test_the_restart_time_is_taken_after_the_registers_are_back_and_before_the_requests_are_sent():
    """「ここより前には止まっていない」の時刻: 再開の要求を送る直前（レジスタを戻した後）。要求を送った後の時刻にすると、その往復の間に
    再開してすぐ止まったコアの停止が記録より前になり、上限が小さすぎる（嘘）。前のコアのレジスタを戻す前の時刻にすると、上限が無駄に大きい。"""
    import fprobe.arm_debug as arm_debug
    clock = Clock(1000)
    a, cores, _ = make(clock=clock)
    a.halt_all()
    leave_all = arm_debug.leave_all

    def slow_leave(debug_cores, mask_interrupts):
        clock.t += 1000 * len(debug_cores)                            # レジスタを戻す往復
        leave_all(debug_cores, mask_interrupts)
    arm_debug.leave_all = slow_leave
    batch = a._dap.mem_batch

    def round_trip(ap, ops):
        out = batch(ap, ops)
        if any(op[0] == "w" and op[1] & 0xFFF == 0x090 for op in ops):
            clock.t += 500                                            # 再開の要求と DSCR の読みの往復
        return out
    a._dap.mem_batch = round_trip
    clock.t = 2000
    try:
        a.resume_all()                                                # 要求を送るのは 2000 + 4 × 1000 = 6000
    finally:
        arm_debug.leave_all = leave_all
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT
    assert a.wait_event(0.5) == (1,)
    clock.t = 9000
    assert a.halt_all().skew_upper_bound_ns == 9000 - 6000


# --- stepping off a hardware breakpoint (review finding 2) --------------------------------------------------------------

def test_a_step_off_a_hardware_breakpoint_lifts_it_for_the_step_and_puts_it_back():
    c0 = ArmCore(match_on_restart=True)
    c0.regs[15] = 0x8000
    a, cores, _ = make([c0] + [ArmCore(mpidr=0x80000F00 | i) for i in range(1, 4)])
    a.halt_all()
    a.set_hw_bp(0, 1, 0x8000, 4)
    a.step(0)
    assert c0.regs[15] == 0x8004                                      # it moved: the breakpoint at the PC did not fire again
    assert c0.bcr[1] & 1 and c0.bvr[1] == 0x8000                      # and it is armed again afterwards


def test_a_breakpoint_elsewhere_is_left_alone_by_a_step():
    c0 = ArmCore(match_on_restart=True)
    c0.regs[15] = 0x8000
    a, cores, _ = make([c0] + [ArmCore(mpidr=0x80000F00 | i) for i in range(1, 4)])
    a.halt_all()
    a.set_hw_bp(0, 1, 0x9000, 4)
    a.step(0)
    assert c0.bcr[1] & 1 and c0.regs[15] == 0x8004


# --- memory from a core that halted in user mode (review finding 5) -----------------------------------------------------

def test_kernel_memory_and_cache_maintenance_work_from_a_core_halted_in_user_mode_and_the_mode_comes_back():
    c0 = ArmCore(cpsr=0x10, kernel_only=[(0xC0000000, 0xD0000000)], user_cp15_undefined=True,
                 cp15={"contextidr": 0x2A05, "ttbr0": 0x40004000})
    a, cores, _ = make([c0] + [ArmCore(mpidr=0x80000F00 | i) for i in range(1, 4)])
    a.halt_all()
    a.write_phys(0x1000, b"abcd")
    assert a.read_phys(0x1000, 4) == b"abcd"
    assert a.read_cp15(0, "ttbr0") == 0x40004000
    a.sync_caches([0])
    assert c0.cpsr == 0x10 and not c0.sticky                          # still user mode, no abort or undefined left behind


def test_a_poll_that_saw_the_cores_running_shortens_the_bound():
    """The record moves forward with every poll: the bound reaches back only to the last poll that saw the core running."""
    clock = Clock(1000)
    a, cores, _ = make(clock=clock)
    a.halt_all()
    clock.t = 2000
    a.resume_all()
    clock.t = 5000
    assert a.wait_event(0.0) == ()                                     # one poll: every core is still running
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT
    clock.t = 6000
    assert a.wait_event(0.5) == (1,)
    clock.t = 9000
    assert a.halt_all().skew_upper_bound_ns == 9000 - 5000


# --- what is left programmed in the cores (read-only; for the test rig's "left clean" checks) ----------------------------

def test_programmed_lists_the_enabled_breakpoint_and_watchpoint_slots_per_core_without_halting_anything():
    a, cores, _ = make()
    assert a.programmed() == {"running": [True] * 4, "breakpoints": {}, "watchpoints": {}}
    a.halt_all()
    a.set_hw_bp(1, 2, 0x10004, 4)
    a.set_hw_watch(0, 1, 0x2000, 4, "w")
    a.resume_all()
    state = a.programmed()
    assert state["breakpoints"] == {1: [2]} and state["watchpoints"] == {0: [1]}
    assert state["running"] == [True] * 4 and not any(c.halted for c in cores)
    a.halt_all()
    a.clear_hw_bp(1, 2)
    a.clear_hw_watch(0, 1)
    a.resume_all()
    assert a.programmed()["breakpoints"] == {} and a.programmed()["watchpoints"] == {}


# --- kernel virtual memory goes through the core's MMU (VMAP_STACK stacks are in vmalloc, not in the linear map) -----------

def test_read_kernel_and_write_kernel_reach_a_vmalloc_address_through_the_cores_mmu():
    a, cores, _ = make()
    a.halt_all()
    vmalloc = 0xE0001000                                              # above linear_end 0xC8000000
    a.write_kernel(vmalloc, bytes(range(16)))
    assert a.read_kernel(vmalloc, 16) == bytes(range(16))
    assert a.read_kernel(vmalloc + 3, 5) == bytes(range(3, 8))
    assert bytes(cores[0].memory[vmalloc + i] for i in range(16)) == bytes(range(16))


def test_physical_access_is_bounded_by_the_linear_map_end_and_says_so():
    a, cores, _ = make()
    a.halt_all()
    top = 0xC8000000 - 0xC0000000                                      # the first physical address that is NOT in the linear map
    a.write_phys(top - 4, b"abcd")                                     # the last word that is
    assert a.read_phys(top - 4, 4) == b"abcd"
    with pytest.raises(OutsideLinearMap):
        a.read_phys(top, 4)
    with pytest.raises(OutsideLinearMap):
        a.write_phys(top - 2, b"abcd")                                 # starts inside but runs out of it


def test_the_linear_map_end_has_no_default():
    with pytest.raises(TypeError):
        ArmACores([], page_offset=0xC0000000, phys_offset=0)


def test_open_sets_the_parts_jtag_idle_cycles_on_the_link_after_each_connect():
    _, _, link = make()
    assert link.transfer_idle_cycles == [0, 0]                        # 1 回目は IDCODE を探す接続、2 回目は見つけた位置での接続
    assert arm_a_part("bcm2836").jtag_idle_cycles == 0


# --- FPGA の停止の見張り（docs/superpowers/specs/2026-10-29-halt-watch-design.md）---------------------------------

class SimWatch:
    """FPGA の見張り（サンプラ + 値の一致の once + 割り込み書き込み）の模型。fprobe.halt_watch.HaltWatch の口。
    サンプラは sample() で 1 巡回（各コアの DSCR の HALTED を読む）。既定では poll のたびに 1 巡回読む（往復の間に巡回が進む）。
    試験は auto を False にして、FPGA がいつ読むかを決める。値の一致: 番号ごとに前の標本が「止まっていない」で今が「止まった」で
    発火（once: 最初の 1 回だけ）、有効にした後の最初の標本では出さない。発火すると全コアに停止要求を書く（halt_others）。"""

    can_restart = False                          # 古い回路（再開をホストが書く）。SimRestartWatch が新しい回路

    def __init__(self, cores, events=None):
        self.cores, self.log = cores, events if events is not None else []
        self.enabled, self.auto, self.halt_others, self.result = False, True, True, "ok"
        self.sample_on_disarm = False                # 外す往復が届く前に、もう 1 巡回読む（外す前の発火の試験）
        self._clear()

    def _clear(self):
        n = len(self.cores)
        self.prev, self.unmatched, self.count, self.index, self.written = [None] * n, [False] * n, 0, 0, 0

    def configure(self, **kw):
        self.log.append(("configure", kw))

    def arm(self):
        self.log.append(("arm", tuple(c.halted for c in self.cores)))
        self.enabled = True
        self._clear()

    def disarm(self):
        self.log.append("disarm")
        if self.sample_on_disarm:
            self.sample()
        self.enabled = False

    def release(self):
        self.log.append("release")
        self.enabled = False

    def sample(self, cores=None):
        if not self.enabled:
            return
        for k in range(len(self.cores)) if cores is None else cores:
            halted = self.cores[k].halted
            if not halted:
                self.unmatched[k] = True
            if halted and self.prev[k] is False and self.count == 0:
                self.count, self.index = 1, 16 * k
                if self.halt_others:
                    for c in self.cores:
                        c.halt_request()
                    self.written = len(self.cores)
                else:
                    self.written = 1
            self.prev[k] = halted

    def poll(self):
        from fprobe.halt_watch import HaltWatchPoll
        if self.auto:
            self.sample()
        return HaltWatchPoll(self.count > 0, self.index, 1000, 900, tuple(self.unmatched))

    def status(self, poll=None):
        from fprobe.halt_watch import HaltWatchStatus
        p = self.poll() if poll is None else poll
        if not p.matched:
            return HaltWatchStatus(False, None, "ok", 0, 900, 1000, 0, 0, p.seen_running)
        return HaltWatchStatus(True, p.index // 16, self.result, self.written, 900, 1000, 1100, 1200, p.seen_running)


class SimRestartWatch(SimWatch):
    """再開を FPGA が書く回路（ケイパビリティ watch_start。docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md）の
    模型: arm_and_restart で全コアを再開させ（始動の列）、その後の読みで HALTED と RESTARTED が両方 1 なら発火する（最初の
    標本でも。once）。restart_result が "ok" でなければ、restart_written 語だけ再開して門を開けない。"""
    can_restart = True

    def __init__(self, cores, events=None):
        super().__init__(cores, events)
        self.restart_result, self.restart_written, self.gate, self.first = "ok", len(cores), False, False

    def arm(self):
        raise AssertionError("the restart path must use arm_and_restart")

    def arm_and_restart(self):
        self.log.append(("arm_and_restart", tuple(c.halted for c in self.cores)))
        self.enabled, self.gate = True, False
        self._clear()
        for c in self.cores[:self.restart_written]:
            c.restart()
        self.gate = self.restart_result == "ok"

    def restart_status(self):
        from fprobe.halt_watch import HaltWatchRestart
        return HaltWatchRestart(False, self.restart_result, self.restart_written, 1, 500, 600)

    def sample(self, cores=None):
        if not (self.enabled and self.gate):
            return
        for k in range(len(self.cores)) if cores is None else cores:
            c = self.cores[k]
            if not c.halted:
                self.unmatched[k] = True
            if c.halted and c.restarted and self.count == 0:
                self.count, self.index, self.first = 1, 16 * k, self.prev[k] is None
                for other in self.cores:
                    other.halt_request()
                self.written = len(self.cores)
            self.prev[k] = c.halted

    def poll(self):
        from fprobe.halt_watch import HaltWatchPoll
        if self.auto:
            self.sample()
        return HaltWatchPoll(self.count > 0, self.index, 1000, 500 if self.first else 900, tuple(self.unmatched),
                             fire_first=self.first)

    def status(self, poll=None):
        from fprobe.halt_watch import HaltWatchStatus
        p = self.poll() if poll is None else poll
        if not p.matched:
            return HaltWatchStatus(False, None, "ok", 0, p.t_prev48, 1000, 0, 0, p.seen_running)
        return HaltWatchStatus(True, p.index // 16, self.result, self.written, p.t_prev48, 1000, 1100, 1200,
                               p.seen_running, fire_first=p.fire_first)


def with_restart_watch(event_bound=None, events=None):
    a, cores, link = make()
    w = SimRestartWatch(cores, events)
    a.use_fpga_halt_watch(w, event_skew_upper_bound_ns=event_bound, poll_s=0.0)
    return a, cores, link, w


def test_with_the_restart_path_the_fpga_is_given_the_restart_requests_and_the_halted_after_restart_condition():
    a, cores, link, w = with_restart_watch()
    (what, kw), = w.log
    bases = [c.port.base for c in a._cores]
    assert what == "configure" and kw == dict(
        ap=a._cores[0].port.ap, watch=[b + 0x88 for b in bases], halted_mask=3, halted_value=3,
        halt_writes=[(b + 0x90, 1) for b in bases], restart_writes=[(b + 0x90, 0b110) for b in bases])


def test_with_the_restart_path_the_fpga_restarts_every_core_and_the_host_does_not_write_the_restart():
    events = []
    a, cores, link, w = with_restart_watch(events=events)
    a.halt_all()
    import fprobe.linux_halt.arma_run as backend  # resume の持ち主（restart_request_all を差し替える）
    called, restart_all = [], backend.restart_request_all
    backend.restart_request_all = lambda ports: called.append(ports) or restart_all(ports)
    try:
        a.resume_all()
    finally:
        backend.restart_request_all = restart_all
    assert called == [] and [e for e in events if e[0] == "arm_and_restart"] == [("arm_and_restart", (True,) * 4)]
    assert not any(c.halted for c in cores)


def test_a_core_halting_at_once_after_the_fpgas_restart_is_halted_by_the_fpga_without_the_debug_link():
    """窓が無い: 再開と同時にブレークポイントで止まったコアでも、FPGA が発火して全コアを止める（旧い回路ではホストの方式に
    落ちた）。待つ間、JTAG を使わない。報告に、FPGA が再開を書いたこと・RESTARTED・最初の読みだったことを事実で書く。"""
    a, cores, link, w = with_restart_watch(event_bound=40_000)
    a.halt_all()
    cores[1].match_on_restart = True
    a._cores[1].set_breakpoint(0, cores[1].regs[15])
    a.resume_all()
    seen = watch_flushes(link, cores)
    assert a.wait_event(1.0) == (1,)
    assert seen == []
    assert a.last_wait["by"] == "fpga" and "fpga_missed" not in a.last_wait
    f = a.last_wait["fpga"]
    assert f["fire_first"] and f["restarted_by_fpga"] == 4 and (f["t_restart_first"], f["t_restart_last"]) == (500, 600)
    assert f["t_prev"] == 500                                          # 最初の読みの発火: 前の時刻 = 始動の列の最初の語
    report = a.halt_all()
    assert (report.method, report.cores_halted, report.skew_upper_bound_ns) == ("sequential", (0, 1, 2, 3), 40_000)
    assert "HALTED = RESTARTED = 1" in report.basis and "first read after the restart writes" in report.basis


def test_a_later_stop_with_the_restart_path_is_also_the_fpgas():
    a, cores, link, w = with_restart_watch()
    a.halt_all()
    a.resume_all()
    seen = watch_flushes(link, cores)
    assert a.wait_event(0.01) == ()
    cores[3].halted, cores[3].moe = True, MOE_BKPT_EVT
    assert a.wait_event(1.0) == (3,)
    assert seen == [] and a.last_wait["by"] == "fpga" and not a.last_wait["fpga"]["fire_first"]
    assert all(c.halted for c in cores)


def test_a_failed_fpga_restart_is_an_error_with_its_facts_and_leaves_the_watch_off():
    from fprobe.arm_debug import ArmDebugError
    events = []
    a, cores, link, w = with_restart_watch(events=events)
    a.halt_all()
    w.restart_result, w.restart_written = "swd_error", 2
    with pytest.raises(ArmDebugError, match="2 / 4"):
        a.resume_all()
    assert events[-1] == "disarm" and not w.enabled and a.parked == {2, 3}


def with_watch(event_bound=None, events=None):
    a, cores, link = make()
    w = SimWatch(cores, events)
    a.use_fpga_halt_watch(w, event_skew_upper_bound_ns=event_bound, poll_s=0.0)
    return a, cores, link, w


def arms(events):
    return [e for e in events if isinstance(e, tuple) and e[0] == "arm"]


def test_the_fpga_watch_is_given_every_cores_dscr_and_drcr_and_the_bound_is_required():
    a, cores, link, w = with_watch()
    (what, kw), = w.log
    bases = [c.port.base for c in a._cores]
    assert what == "configure" and kw == dict(ap=a._cores[0].port.ap, watch=[b + 0x88 for b in bases],
                                              halted_mask=1, halted_value=1, halt_writes=[(b + 0x90, 1) for b in bases])
    with pytest.raises(TypeError):
        a.use_fpga_halt_watch(w, poll_s=0.0)


def test_the_watch_is_armed_while_every_core_is_still_halted_just_before_the_restart_requests():
    """2026-11-03 の穴 (a): 再開の後に見張りを始めると、始めるまでの往復の間に止まったコアで発火しない。全コアが止まっている
    間（レジスタを戻した後、再開の要求の前）に始める: 止まったままの標本は「入った」にならず、走っている値を読んだ後の停止で発火する。"""
    events = []
    a, cores, link, w = with_watch(events=events)
    a.halt_all()
    assert arms(events) == []                                         # 止めている間は見張らない
    import fprobe.linux_halt.arma_run as backend  # resume の持ち主（restart_request_all を差し替える）
    seen, restart_all = [], backend.restart_request_all

    def recording(ports):
        seen.append(list(events))
        return restart_all(ports)
    backend.restart_request_all = recording
    try:
        a.resume_all()
    finally:
        backend.restart_request_all = restart_all
    assert arms(seen[0]) == [("arm", (True,) * 4)]                    # 要求の前、全コアが止まっている間に
    assert not any(c.halted for c in cores)
    a.halt_all()
    assert events[-1] == "disarm"                                     # ホストが止める前に外す


def test_resuming_some_cores_does_not_arm_the_watch():
    events = []
    a, cores, link, w = with_watch(events=events)
    a.halt_all()
    a.resume([0, 1])
    assert arms(events) == []


def test_a_failed_restart_leaves_the_watch_off():
    from fprobe.arm_debug import ArmDebugError
    events = []
    a, cores, link, w = with_watch(events=events)
    a.halt_all()
    cores[2].restart = lambda: setattr(cores[2], "restarted", False)
    a._cores[2].timeout_s = 0.01
    with pytest.raises(ArmDebugError):
        a.resume_all()
    assert events[-1] == "disarm" and not w.enabled


def test_waiting_with_the_watch_does_not_use_the_debug_link_and_returns_the_core_that_stopped():
    a, cores, link, w = with_watch()
    a.halt_all()
    a.resume_all()
    seen = watch_flushes(link, cores)
    assert a.wait_event(0.01) == ()                                   # 何も起きない（FPGA は全コアが走っているのを読んだ）
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    assert a.wait_event(1.0) == (2,)
    assert seen == []                                                 # 待つ間、JTAG を使わない（FPGA の見張りを邪魔しない）
    assert a.last_wait["by"] == "fpga" and a.last_wait["requested"] == (0, 1, 3)
    assert a.last_wait["fpga"]["t_fire"] == 1000 and a.last_wait["fpga"]["written"] == 4
    assert all(c.halted for c in cores)


def test_a_stop_by_the_fpga_is_reported_with_the_bound_the_caller_measured():
    a, cores, link, w = with_watch(event_bound=277_000)
    a.halt_all()
    a.resume_all()
    a.wait_event(0.0)
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT
    a.wait_event(1.0)
    report = a.halt_all()
    assert (report.method, report.cores_halted, report.skew_upper_bound_ns) == ("sequential", (0, 1, 2, 3), 277_000)
    assert "fpga" in report.basis


def test_if_the_fpga_did_not_halt_every_core_the_host_halts_the_rest_and_says_so():
    a, cores, link, w = with_watch(event_bound=277_000)
    w.halt_others, w.result = False, "swd_error"
    a.halt_all()
    a.resume_all()
    a.wait_event(0.0)
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT
    assert a.wait_event(1.0) == (1,)
    report = a.halt_all()
    assert report.cores_halted == (0, 1, 2, 3) and report.skew_upper_bound_ns != 277_000
    assert "fpga" in report.basis and "swd_error" in report.basis


def test_the_fpga_watch_and_the_cti_are_not_used_together():
    a, cores, link, w = with_watch()
    with pytest.raises(ValueError):
        a.use_cross_halt(FakeCross(cores), request_skew_upper_bound_ns=68, event_skew_upper_bound_ns=None,
                         event_trigin=5, hardware_halt_wait_s=0.01)


def test_a_core_that_stops_at_once_after_the_restart_is_found_without_waiting_for_the_timeout():
    """2026-11-03 の穴 (b): 再開してすぐ（FPGA が走っている値を読む前に）止まったコアでは発火しない。再開の確かめの DSCR で
    止まっているのを読み、その後の poll で FPGA がそのコアの走っている値を読んでいない（seen_running が False）ので、発火しないと
    確定し、タイムアウトを待たずにホストの方式に移る。"""
    a, cores, link, w = with_watch()
    a.halt_all()
    cores[1].match_on_restart = True                                  # 再開と同時にブレークポイントで止まる
    a._cores[1].set_breakpoint(0, cores[1].regs[15])
    a.resume_all()
    started = time.monotonic()
    assert a.wait_event(30.0) == (1,)
    assert time.monotonic() - started < 5.0                           # タイムアウト（30 s）を待たない
    assert a.last_wait["by"] == "host" and a.last_wait["fpga_missed"] == (1,)
    assert a.last_wait["requested"] == (0, 2, 3) and all(c.halted for c in cores)
    report = a.halt_all()
    assert report.basis.startswith("fpga halt watch: cores [1]") and "unmatched" in report.basis
    assert report.skew_upper_bound_ns is not None                     # ホストの時計の上限（事実の読みの範囲）


def test_a_core_that_stops_after_the_restart_check_but_before_the_fpga_reads_it_running_is_found_by_the_host():
    """再開の確かめでは走っていたが、FPGA が走っている値を読む前に止まった: 最初の poll でそのコアだけ seen_running が False。
    そのコアの DSCR だけを読み（止まっている）、その後の poll でも False なので、発火しないと確定してホストの方式に移る。"""
    a, cores, link, w = with_watch()
    a.halt_all()
    a.resume_all()
    cores[3].halted, cores[3].moe = True, MOE_BKPT_EVT                # FPGA がまだ 1 度も読んでいないうちに止まった
    assert a.wait_event(30.0) == (3,)
    assert a.last_wait["by"] == "host" and a.last_wait["fpga_missed"] == (3,)
    assert all(c.halted for c in cores)


def test_a_core_seen_running_by_the_fpga_is_left_to_the_fpga_even_if_the_host_reads_it_halted_first():
    """読む順で結論が変わらない: ホストが止まっているのを読んだコアでも、FPGA がその前に走っている値を読んでいれば発火する
    （発火を待つ。見逃しとは言わない）。"""
    a, cores, link, w = with_watch()
    w.auto = False
    a.halt_all()
    a.resume_all()
    w.sample([0, 1, 2])                                               # コア 3 はまだ読んでいない
    scan = a._scan_of

    def fpga_reads_core_3_running_then_it_stops(ns):
        w.sample([3])                                                 # poll の後、ホストの DSCR の読みの前に FPGA が読んだ
        cores[3].halted, cores[3].moe = True, MOE_BKPT_EVT
        return scan(ns)
    a._scan_of = fpga_reads_core_3_running_then_it_stops
    assert a.wait_event(0.0) == ()                                    # ホストは止まっているのを読んだが、その後の poll で
    assert a._watch_halted == {3}                                     # FPGA が走っている値を読んでいた: 見逃しではない
    a._scan_of = scan
    w.sample()                                                        # FPGA が止まった値を読む → 発火
    assert a.wait_event(0.0) == (3,) and a.last_wait["by"] == "fpga"


def test_the_debug_link_is_used_only_until_the_fpga_has_read_every_core_running():
    a, cores, link, w = with_watch()
    w.auto = False
    a.halt_all()
    a.resume_all()
    seen = watch_flushes(link, cores)
    w.sample([0, 1])
    a.wait_event(0.0)
    assert len(seen) == 1                                             # 読んでいない 2 と 3 の DSCR を 1 往復で
    w.sample()
    a.wait_event(0.0)
    a.wait_event(0.0)
    assert len(seen) == 1                                             # そろった後は JTAG を使わない


def test_a_fire_before_the_disarm_is_reported_by_halt_all_as_a_stop_by_the_fpga():
    """2026-11-03 の穴 (c): 待つのを終えた（タイムアウト）後、外す前に FPGA が発火して全コアを止めた。外してから状態を読むので、
    FPGA の停止として報告する（ホストの停止と取り違えない）。"""
    a, cores, link, w = with_watch(event_bound=277_000)
    a.halt_all()
    a.resume_all()
    assert a.wait_event(0.0) == ()
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    w.sample_on_disarm = True                                         # 外す往復が届く前に FPGA が読んで発火する
    report = a.halt_all()
    assert report.skew_upper_bound_ns == 277_000 and "core 2 stopped by itself" in report.basis


def test_a_fire_on_another_core_before_the_disarm_of_a_missed_stop_is_reported_with_it():
    a, cores, link, w = with_watch()
    w.auto = False
    a.halt_all()
    a.resume_all()
    w.sample([0, 1, 2])
    cores[3].halted, cores[3].moe = True, MOE_BKPT_EVT                # FPGA が読む前に止まった（見逃し）
    cores[0].halted, cores[0].moe = True, MOE_BKPT_EVT                # コア 0 も止まり、外す前に FPGA が読んで発火する
    w.sample_on_disarm = True
    assert a.wait_event(1.0) == (0, 3)
    assert a.last_wait["by"] == "host" and a.last_wait["fpga_missed"] == (3,)
    assert "fired on core 0" in a.last_wait["note"]


def test_a_stop_by_the_fpga_also_reports_a_core_that_had_stopped_unseen():
    """FPGA が別のコアで発火したとき、FPGA が走っているのを読む前に止まっていたコア（再開と同時にブレークポイント）も事実として
    報告に添える（発火した停止として扱うが、そのコアは見張りに見られずに止まった）。"""
    a, cores, link, w = with_watch(event_bound=277_000)
    w.auto = False
    a.halt_all()
    cores[3].match_on_restart = True
    a._cores[3].set_breakpoint(0, cores[3].regs[15])
    a.resume_all()                                                    # コア 3 は再開と同時に止まった（確かめの DSCR で読んだ）
    w.sample()
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT                # コア 1 は走っているのを読まれた後に止まった
    w.sample()                                                        # → 発火
    assert a.wait_event(1.0) == (1,) and a.last_wait["fpga_missed"] == (3,)
    report = a.halt_all()
    assert "core 1 stopped by itself" in report.basis and "cores [3] were read halted" in report.basis


def test_close_releases_the_watch_route():
    events = []
    a, cores, link, w = with_watch(events=events)
    a.close()
    assert events[-1] == "release"


def test_open_zynq7000_without_an_apb_ap_reaches_both_cores_through_the_ahb_ap_system_map():
    cores = [ArmCore(mpidr=0x80000000 | i) for i in range(2)]
    ahb = ApbAp(None, {0xF8890000 + 0x2000 * i: V7DebugBlock(c) for i, c in enumerate(cores)})
    ahb.idr = 0x44770001
    target = FakeTarget(dpidr=IDCODE, swd=False, jtag_dp_index=0, aps={0: ahb}, jtag_chain=FakeChain([Tap(4, IDCODE)]))
    a = ArmACores.open(FakeDapLink(target), arm_a_part("zynq7000"), frequency_hz=1_000_000, **LINEAR)
    assert a.n_cores == 2 and a.debug_ap == 0
    a.halt_all()
    assert all(c.halted for c in cores)


def test_disable_halting_debug_clears_hdbgen_on_every_core_so_a_left_over_bkpt_cannot_freeze_the_target():
    a, cores, _ = make()
    assert all(c.ctrl & HDBGEN for c in cores)
    a.disable_halting_debug()
    assert not any(c.ctrl & HDBGEN for c in cores)


def test_rescue_clears_breakpoints_turns_halting_debug_off_and_restarts_every_halted_core():
    a, cores, _ = make()
    a.set_hw_bp(0, 0, 0x8000, 4)
    a.set_hw_watch(1, 0, 0x9000, 4, "w")
    a.halt_all()
    assert all(c.halted for c in cores)
    resumed = a.rescue()
    assert resumed == (0, 1, 2, 3)
    assert not any(c.halted for c in cores) and not any(c.ctrl & HDBGEN for c in cores)
    assert a.programmed()["breakpoints"] == {} or not any(a.programmed()["breakpoints"].values())
    assert not any(a.programmed()["watchpoints"].values())
    assert a.rescue() == ()                                   # もう止まっているコアは無い


def test_reading_registers_and_halting_take_few_round_trips():
    # 往復の数は、Linux を止めている時間と、止めた後の読み出しの速さに直結する（USB・Ethernet の 1 往復は 0.3〜1 ms）。
    # 命令の書き込みと完了の確かめ、DTRRX の書き込みと命令、TAR と DRW と JTAG の誤りの確かめを、それぞれ 1 回の往復にまとめた。
    # 数は偽のデバイスでの値（2026-11-01: halt_all 346 → 118、regs 182 → 52。2026-11-02: resume_all 34 → 17、うち最初のコアが
    # 走り出してから最後のコアが走り出すまで 24 → 0 = 全コアの再開の要求を 1 回の往復で。2026-11-03: 命令の列を確かめながら
    # 1 回の往復に（fprobe.armv7a_debug.run）、全コアの入る・戻すをまとめて: halt_all 59 → 5、regs 26 → 1、resume_all 22 → 3。
    # この版の flush の数え方での前の値。test_armv7a_batch に操作ごとの約束）。増えたら、まとめが崩れていないかを見る
    a, cores, link = make()
    f0 = link.flushes
    a.halt_all()
    assert link.flushes - f0 <= 5
    f0 = link.flushes
    a.regs(0)
    assert link.flushes - f0 <= 1
    f0 = link.flushes
    a.resume_all()
    assert link.flushes - f0 <= 3


# --- CTI の無い「こちらから全コアを止める」の上限を、FPGA が付けた停止要求の書き込みの時刻で
#     （docs/superpowers/specs/2026-11-01-stop-precision-design.md 3.3、docs/linux-halt.md）---------------------------

TICKS = 211                     # 偽のイベント記録: 転送 1 回ごとに進む刻み
HZ = 100_000_000                # 1 刻み 10 ns


def with_request_times(clock=None, ticks=TICKS):
    from event_fakes import RecordingEventLog
    from fprobe.linux_halt.request_times import EventRequestTimes
    a, cores, link = make(clock=clock)
    log = RecordingEventLog(link, ticks_per_transfer=ticks)
    a.use_fpga_request_times(EventRequestTimes(log, timebase_hz=HZ))
    return a, cores, link, log


def test_the_bound_of_a_requested_stop_is_the_span_of_the_fpga_timestamps_of_the_halt_writes():
    a, cores, link, log = with_request_times(clock=Clock(0))
    report = a.halt_all()
    assert (report.method, report.cores_halted) == ("sequential", (0, 1, 2, 3))
    # TAR + DRW がコアごとに 2 転送: 最初の DRW から最後の DRW まで 3 × 2 転送 = 1266 刻み = 12660 ns
    assert report.skew_upper_bound_ns == 3 * 2 * TICKS * 10
    assert report.basis.startswith("fpga-timed")
    assert "1266 ticks at 100000000 Hz" in report.basis
    assert "assumes" in report.basis and "same time" in report.basis       # 仮定は根拠の文に書く（使う側が判断する）
    assert "host clock" in report.basis                                     # PC の時計の上限（仮定なし）も並べる
    assert len(report.assumptions) == 1 and "same time" in report.assumptions[0]   # 仮定は欄でも出す
    assert report.bound_without_assumptions_ns == 0                         # 仮定なしの上限（時計を止めた偽物で 0）


def test_the_host_timed_bound_is_given_beside_the_fpga_one():
    clock = Clock(1000)
    a, cores, link, log = with_request_times(clock=clock)
    scan = a._read_dscr

    def scan_then_time_passes(ns):
        out = scan(ns)
        clock.t += 70_000                                              # USB の往復（走っているのを読む・止まったのを読む）
        return out
    a._read_dscr = scan_then_time_passes
    report = a.halt_all()
    assert report.skew_upper_bound_ns == 12660
    assert "host clock: 140000 ns" in report.basis


def test_without_the_setting_the_bound_stays_host_timed():
    a, cores, link = make(clock=Clock(0))
    report = a.halt_all()
    assert report.basis.startswith("host-timed") and "fpga" not in report.basis.lower()


@pytest.mark.parametrize("spoil, reason", [
    (lambda log: setattr(log, "lost", 1), "lost"),
    (lambda log: setattr(log, "recording", False), "not recording"),
])
def test_if_the_event_log_lost_events_the_bound_is_host_timed_and_says_why(spoil, reason):
    a, cores, link, log = with_request_times(clock=Clock(0))
    write_many = a._dap.mem_write32_many

    def spoiled(ap, writes):
        write_many(ap, writes)
        spoil(log)
    a._dap.mem_write32_many = spoiled
    report = a.halt_all()
    assert report.cores_halted == (0, 1, 2, 3) and report.skew_upper_bound_ns == 0      # the ledger bound (clock fixed)
    assert report.basis.startswith("host-timed") and "FPGA timestamps were not used" in report.basis
    assert reason in report.basis


def test_a_core_that_stopped_by_itself_before_its_request_makes_the_bound_host_timed():
    """走っているのを読んだ後、停止要求が届く前にブレークポイントで止まったコア: 止まった時刻は要求の時刻ではない。"""
    a, cores, link, log = with_request_times(clock=Clock(0))
    write_many = a._dap.mem_write32_many

    def core_2_hits_first(ap, writes):
        cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
        write_many(ap, writes)
    a._dap.mem_write32_many = core_2_hits_first
    report = a.halt_all()
    assert report.basis.startswith("host-timed") and "entry reason" in report.basis and "[2]" in report.basis


def dscr_reads(log, address):
    """偽の記録の中の、番地 address の DRW の読み: [(頼んだ転送の位置, 返った値)]（読みの直後の RDBUFF が値を返す）。"""
    ev = log.events
    return [(i, ev[i + 1].data) for i, e in enumerate(ev) if e.ap and e.read and e.address == address]


def last_write(log, address, value):
    return max(i for i, e in enumerate(log.events) if e.ap and not e.read and e.address == address and e.data == value)


def test_a_stop_by_a_breakpoint_is_fpga_timed_from_the_last_running_read_to_the_last_halt_write():
    """実機の評価（2026-11-03）: ブレークポイントで止めた停止の上限が PC の時計（3.7〜4.8 ms）だった。イベント記録の時刻で出す:
    止まったコアを最後に「走っている」と読んだ読みから、他のコアへの最後の停止要求の書き込みまで（書き込みが届いた瞬間に
    止まる、の仮定つき）。仮定なしの上限は、各コアが止まっているのを読んだ読みまで（これも FPGA の時刻）。"""
    a, cores, link, log = with_request_times(clock=Clock(0))
    a.halt_all()
    a.resume_all()
    assert a.wait_event(0.0) == ()                                    # 走っているのを読む（記録に残る）
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    assert a.wait_event(0.5) == (2,)
    report = a.halt_all()
    assert report.basis.startswith("fpga-timed (stop by a core)") and "cores [2]" in report.basis
    assert report.assumptions and "reaches it" in report.assumptions[0]
    base = [c.port.base for c in a._cores]
    first_write = min(last_write(log, base[n] + 0x90, 1) for n in (0, 1, 3))
    reads = [(i, v) for i, v in dscr_reads(log, base[2] + 0x88) if i < first_write]
    halted_read = max(i for i, v in reads if v & HALTED)                      # 止まったのを見つけた読み
    lower = log.events[max(i for i, v in reads if not v & HALTED and i < halted_read) - 1].time   # 古い回路: 前の転送
    end = max(log.events[last_write(log, base[n] + 0x90, 1)].time for n in (0, 1, 3))
    assert report.skew_upper_bound_ns == (end - lower) * 10                   # 100 MHz: 1 刻み 10 ns
    assert report.bound_without_assumptions_ns >= report.skew_upper_bound_ns
    assert "host clock: 0 ns" in report.basis                                 # PC の時計の上限も並べる


def test_on_a_line_time_circuit_the_read_request_itself_is_the_lower_bound():
    a, cores, link, log = with_request_times(clock=Clock(0))
    a._request_times.line_time = True
    a.halt_all()
    a.resume_all()
    a.wait_event(0.0)
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT
    a.wait_event(0.5)
    report = a.halt_all()
    base = [c.port.base for c in a._cores]
    first_write = min(last_write(log, base[n] + 0x90, 1) for n in (0, 2, 3))
    reads = [(i, v) for i, v in dscr_reads(log, base[1] + 0x88) if i < first_write]
    halted_read = max(i for i, v in reads if v & HALTED)
    lower = log.events[max(i for i, v in reads if not v & HALTED and i < halted_read)].time
    end = max(log.events[last_write(log, base[n] + 0x90, 1)].time for n in (0, 2, 3))
    assert report.skew_upper_bound_ns == (end - lower) * 10


def test_a_core_that_stops_at_once_after_the_restart_is_bounded_from_its_restart_write():
    """再開の要求の直後（再開の後の DSCR の読みより前）に止まったコア: 再開の後に「走っている」と読んだ読みが無いので、
    下限はそのコアの再開の要求の書き込み。"""
    a, cores, link, log = with_request_times(clock=Clock(0))
    a.halt_all()
    restart = cores[1].restart

    def restart_then_core_0_hits():
        restart()
        cores[0].halted, cores[0].moe = True, MOE_BKPT_EVT
    cores[1].restart = restart_then_core_0_hits
    a.resume_all()
    assert a.wait_event(0.5) == (0,)
    report = a.halt_all()
    assert report.basis.startswith("fpga-timed (stop by a core)")
    assert "core 0: the write of its restart request" in report.basis
    base = [c.port.base for c in a._cores]
    restart_write = last_write(log, base[0] + 0x90, 0x2 | 0x4)       # DBGDRCR ← RRQ | CSE
    end = max(log.events[last_write(log, base[n] + 0x90, 1)].time for n in (1, 2, 3))
    assert report.skew_upper_bound_ns == (end - log.events[restart_write - 1].time) * 10


def test_without_the_request_times_a_stop_by_a_breakpoint_says_why_it_is_host_timed():
    a, cores, link = make(clock=Clock(0))
    a.halt_all()
    a.resume_all()
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    a.wait_event(0.5)
    report = a.halt_all()
    assert report.basis.startswith("host-timed") and "fpga_request_times is off" in report.basis


def test_events_lost_since_the_resume_make_a_stop_by_a_breakpoint_host_timed_and_say_so():
    a, cores, link, log = with_request_times(clock=Clock(0))
    a.halt_all()
    a.resume_all()
    log.lost = 3
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    a.wait_event(0.5)
    report = a.halt_all()
    assert report.basis.startswith("host-timed") and "lost 3 events since the resume" in report.basis


def test_a_long_wait_reads_further_back_until_the_last_running_read_is_found():
    from fprobe.linux_halt.request_times import EventRequestTimes
    a, cores, link, log = with_request_times(clock=Clock(0))
    EventRequestTimes.FIRST_READ = 2                                  # 末尾から少しずつ: 足りなければ倍にして読み直す
    try:
        a.halt_all()
        a.resume_all()
        for _ in range(5):
            a.wait_event(0.0)
        cores[0].halted, cores[0].moe = True, MOE_BKPT_EVT
        a.wait_event(0.5)
        report = a.halt_all()
    finally:
        EventRequestTimes.FIRST_READ = 512
    assert report.basis.startswith("fpga-timed (stop by a core)") and log.reads > 1


def test_a_core_that_did_not_halt_makes_the_bound_host_timed():
    a, cores, link, log = with_request_times(clock=Clock(0))
    cores[2].ctrl &= ~HDBGEN                                          # ignores halt requests
    a._cores[2].timeout_s = 0.01
    report = a.halt_all()
    assert report.cores_halted == (0, 1, 3) and "fpga-timed" not in report.basis


def test_the_events_are_read_before_any_later_halt_request():
    """状態の読み出し（`ArmDebugCore.halt` は止まっていなければ要求を書く）の前に読む: 後の要求を取り違えない。"""
    a, cores, link, log = with_request_times(clock=Clock(0))
    order = []
    read = log.read
    log.read = lambda *x, **k: (order.append("events"), read(*x, **k))[1]
    for c in a._cores:
        halt = c.halt
        c.halt = lambda halt=halt: (order.append("confirm"), halt())[1]
    a.halt_all()
    assert order[0] == "events"


def test_the_fpga_request_times_and_the_cti_are_not_used_together():
    from fprobe.linux_halt.request_times import EventRequestTimes
    a, cores, cross = with_cti()
    with pytest.raises(ValueError):
        a.use_fpga_request_times(EventRequestTimes(None, timebase_hz=HZ))
    a, cores, link, log = with_request_times()
    with pytest.raises(ValueError):
        a.use_cross_halt(FakeCross(cores), request_skew_upper_bound_ns=68, event_skew_upper_bound_ns=None,
                         event_trigin=5, hardware_halt_wait_s=0.01)


def test_reconnect_after_the_debug_power_request_was_lost_gives_the_cores_back():
    # 実機（Pi 2B、2026-11-02）: 化けた転送が DP の CTRL/STAT を 0 に書き（デバッグの電源の要求が消え）、その後の AP の転送が全部失敗した
    from fprobe.raw_dap import DapError
    a, _, link = make()
    link.ctrl_stat = 0
    with pytest.raises(DapError):
        a.halted()
    a.reconnect()
    assert a.halted() == (False,) * 4


def test_closing_disarms_the_fpga_watch_it_left_armed():
    # 実機（PYNQ → Pi 2B、2026-11-02）: attach の後始末が全コアを再開すると見張りが有効になり、close は外さずに閉じていた。
    # プロセスが終わった後も FPGA が見張り続け（サンプラが DAP の線を使い続け）、次のプロセスの DAP が応答しなくなった。
    # 外さないと、後でコアが 1 つ止まったとき FPGA が残りのコアを勝手に止めもする
    a, _, _, w = with_watch()
    a.halt_all()
    a.resume_all()
    assert w.log[-1] == ("arm", (True,) * 4) and w.enabled
    a.close()
    assert w.log[-1] == "release" and not w.enabled                 # 見張りを外し、クロストリガの経路も外す


# --- FPGA の見張りが止めた停止の上限を、FPGA の時刻で（実機の評価 2026-11-03: bound unmeasured では null だった）---------------

class WatchDev:
    """HaltWatch.dev の偽物（capabilities だけ）。"""
    def __init__(self, line_time=True, hz=HZ):
        from types import SimpleNamespace
        self.caps = SimpleNamespace(dap_line_time=line_time, timebase_hz=hz)

    def capabilities(self):
        return self.caps


def stopped_by_the_watch(event_bound=None, line_time=True, other_moe=None):
    a, cores, link, w = with_watch(event_bound=event_bound)
    w.dev = WatchDev(line_time=line_time)
    a.halt_all()
    a.resume_all()
    a.wait_event(0.0)
    cores[1].halted, cores[1].moe = True, MOE_BKPT_EVT
    assert a.wait_event(1.0) == (1,) and a.last_wait["by"] == "fpga"
    if other_moe is not None:
        cores[3].moe = other_moe
    return a.halt_all()


def test_a_stop_by_the_fpga_watch_is_bounded_by_its_last_running_sample_and_last_halt_write():
    report = stopped_by_the_watch()                                   # SimWatch: t_prev 900、t_last_write 1200
    assert report.skew_upper_bound_ns == (1200 - 900) * 10
    assert "fpga-timed" in report.basis and "core 1 running" in report.basis and "300 ticks" in report.basis
    assert report.assumptions and "reaches it" in report.assumptions[0]
    assert "caller's bound: none" in report.basis


def test_the_caller_bound_stays_beside_the_fpga_one():
    report = stopped_by_the_watch(event_bound=277_000)
    assert report.skew_upper_bound_ns == 3000 and "caller's bound: 277000 ns" in report.basis


def test_without_line_time_the_watch_stop_keeps_the_callers_bound_and_says_why():
    report = stopped_by_the_watch(event_bound=None, line_time=False)
    assert report.skew_upper_bound_ns is None and "bound not measured" in report.basis
    assert "the FPGA timestamps were not used" in report.basis and "DAP_LINE_TIME" in report.basis


def test_another_core_that_stopped_by_itself_keeps_the_watch_stop_unbounded_and_says_why():
    report = stopped_by_the_watch(other_moe=MOE_BKPT_EVT)
    assert report.skew_upper_bound_ns is None and "cores [3]" in report.basis and "entry reason" in report.basis
