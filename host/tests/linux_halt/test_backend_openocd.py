"""The OpenOCD commands the Pi backend sends and how it reads the answers, against a scripted OpenOCD.
(The hardware itself is exercised by hw/test_pi_hw.py.)"""
import pytest

from fprobe.linux_halt.backend_openocd import OcdError, OpenOcdCores
from fprobe.plugins.openocd_boards import RPI2B, ZYNQ7000
from fprobe.linux_halt.core_access import CoreState, MOE_BKPT_INSN, MOE_WATCH_SYNC, OutsideLinearMap

LINEAR = dict(page_offset=0xC0000000, phys_offset=0, linear_end=0xC8000000)


class Clock:
    def __init__(self, t=0):
        self.t = t

    def __call__(self):
        return self.t


class FakeOcd:
    """answers: prefix -> text, or a list of texts used in turn (the last one repeats)."""

    def __init__(self, _clock=None, _takes=None, **answers):
        self.clock, self.takes = _clock, _takes or {}
        self.log, self.cur, self.answers = [], None, {k.replace("_", " ", 1) if k.startswith(("arm_", "adapter_")) else k: v
                                                      for k, v in answers.items()}

    def select(self, n):
        self.cur = n
        self.log.append(("select", n))

    def cmd(self, s):
        self.log.append(("cmd", s))
        for prefix, ns in self.takes.items():                     # this command takes ns nanoseconds of the test clock
            if s.startswith(prefix) and self.clock is not None:
                self.clock.t += ns
        for prefix, a in self.answers.items():
            if s.startswith(prefix):
                if isinstance(a, list):
                    return a.pop(0) if len(a) > 1 else a[0]
                return a
        return ""

    def sent(self):
        return [x[1] for x in self.log if x[0] == "cmd"]


def cores(ocd, **kw):
    return OpenOcdCores(ocd, **{"board": RPI2B, "jtag_khz": 4000, "hw_slots": 5, "watch_slots": 4, **LINEAR, **kw})



def test_open_needs_the_timeout_from_the_caller():
    with pytest.raises(TypeError):
        OpenOcdCores.open("127.0.0.1", 6666, board=RPI2B, jtag_khz=4000, hw_slots=5, watch_slots=4, **LINEAR)


def test_jtag_khz_is_required_and_range_checked():
    with pytest.raises(TypeError):
        OpenOcdCores(FakeOcd(), board=RPI2B, hw_slots=5, watch_slots=4, **LINEAR)
    for bad in (0, -1, 100001):
        with pytest.raises(ValueError):
            OpenOcdCores(FakeOcd(), board=RPI2B, jtag_khz=bad, hw_slots=5, watch_slots=4, **LINEAR)


def test_setup_sets_the_clock_reads_it_back_and_prepares_every_core():
    o = FakeOcd(adapter_speed="adapter speed: 4000 kHz")
    c = cores(o)
    c.setup()
    assert c.actual_jtag_khz == 4000
    sent = o.sent()
    assert sent[0] == "adapter speed 4000" and "poll on" in sent
    assert sent.count("cortex_a maskisr on") == 4
    for name in ("lx_states", "lx_halt", "lx_resume", "lx_wait", "lx_snap"):
        assert any("proc %s " % name in s for s in sent), name


def test_setup_refuses_an_answer_that_does_not_say_the_clock():
    c = cores(FakeOcd(adapter_speed="ok"))
    with pytest.raises(OcdError, match="JTAG clock"):
        c.setup()


def test_halt_all_halts_the_running_cores_and_bounds_the_skew_from_the_scan_to_the_end_of_the_halt():
    clock = Clock(1000)
    o = FakeOcd(_clock=clock, _takes={"lx_halt": 7000},
                lx_states=["running running running running", "halted halted halted halted"], lx_halt="0 0\n1 1\n2 2\n3 3")
    report = cores(o, clock=clock).halt_all()
    assert "lx_halt {0 1 2 3}" in o.sent()
    assert (report.method, report.cores_halted) == ("sequential", (0, 1, 2, 3))
    assert report.skew_upper_bound_ns == 7000 and "known" in report.basis


def test_event_stop_bound_reaches_back_to_the_restart_and_is_used_once():
    clock = Clock(500)
    o = FakeOcd(_clock=clock, lx_resume="0 1 2 3", lx_wait="2 | 3500 1200",
                lx_states=["halted halted halted halted", "halted halted halted halted"])
    c = cores(o, clock=clock)
    c.resume_all()                                                     # restarted at t=500
    clock.t = 600
    assert c.wait_event(0.02) == (2,)
    clock.t = 9600
    first = c.halt_all()
    assert first.skew_upper_bound_ns == 9600 - 500                     # the scan/issue numbers of the Tcl are NOT the bound
    clock.t = 20000
    second = c.halt_all()                                              # nothing to halt now: not a measurement
    assert second.skew_upper_bound_ns is None


def test_a_poll_that_saw_the_cores_running_moves_the_start_of_the_bound_forward():
    clock = Clock(500)
    o = FakeOcd(_clock=clock, lx_resume="0 1 2 3", lx_wait=["", "1 | 10 10"],
                lx_states=["halted halted halted halted"])
    c = cores(o, clock=clock)
    c.resume_all()
    clock.t = 5000
    assert c.wait_event(0.02) == ()                                    # a whole slice with every core still running
    clock.t = 5500
    assert c.wait_event(0.02) == (1,)
    clock.t = 8000
    assert c.halt_all().skew_upper_bound_ns == 8000 - 5000


def test_a_core_found_halted_without_an_event_the_debugger_saw_makes_the_bound_unknown():
    clock = Clock(500)
    o = FakeOcd(_clock=clock, lx_resume="0 1 2 3", lx_states=["halted running running running", "halted halted halted halted"],
                lx_halt="1 0\n2 1\n3 2")
    c = cores(o, clock=clock)
    c.resume_all()
    clock.t = 3000
    report = c.halt_all()
    assert report.skew_upper_bound_ns is None and "already" in report.basis


def test_nothing_to_halt_means_the_bound_is_unknown_not_zero():
    o = FakeOcd(lx_states="halted halted halted halted")
    report = cores(o).halt_all()
    assert report.skew_upper_bound_ns is None and report.cores_halted == (0, 1, 2, 3)


def test_a_core_that_does_not_halt_is_not_reported_as_halted():
    o = FakeOcd(lx_states=["running running running running", "halted halted running halted"], lx_halt="0 0\n1 1\n2 2\n3 3")
    assert cores(o).halt_all().cores_halted == (0, 1, 3)


def test_cores_left_halted_by_a_cut_short_resume_make_the_bound_unknown():
    o = FakeOcd(lx_resume="0 1", lx_states="halted halted halted halted")
    c = cores(o)
    c.resume([0, 1, 2])
    assert c.parked == {2}
    report = c.halt_all()
    assert report.skew_upper_bound_ns is None and "not resumed" in report.basis


def test_wait_event_watches_the_cores_that_are_not_parked_and_forgets_the_target():
    o = FakeOcd(lx_resume="0 1", lx_wait="")
    c = cores(o)
    c.resume([0, 1, 2])
    assert c.parked == {2}
    o.cur = 3
    assert c.wait_event(0.02) == ()
    assert "lx_wait 20 {0 1 3}" in o.sent() and o.cur is None


def test_physical_reads_and_writes_go_through_the_linear_map_of_core_0():
    o = FakeOcd(read_memory="0x11223344 0x55667788")
    c = cores(o)
    assert c.read_phys(0x1000, 8) == bytes.fromhex("4433221188776655")
    assert ("select", 0) in o.log and "read_memory 0xc0001000 32 2" in o.sent()
    c.write_phys(0x1004, bytes.fromhex("01000000"))
    sent = o.sent()
    assert "write_memory 0xc0001004 32 {0x1}" in sent
    assert "arm mcr 15 0 7 11 1 3221229568" in sent                        # DCCMVAU of the line (0xc0001000), cleaned to PoU


def test_unaligned_write_goes_byte_wise():
    o = FakeOcd()
    cores(o).write_phys(0x1001, b"\xAA\xBB")
    assert "write_memory 0xc0001001 8 {0xaa 0xbb}" in o.sent()


def test_short_read_raises():
    with pytest.raises(OcdError, match="short read"):
        cores(FakeOcd(read_memory="0x1")).read_phys(0x1000, 8)


def test_hw_breakpoint_and_watchpoint_commands_per_core():
    o = FakeOcd()
    c = cores(o)
    c.set_hw_bp(2, 0, 0x10004, 4)
    c.set_hw_watch(1, 0, 0x2000, 4, "rw")
    c.set_hw_watch(1, 1, 0x3000, 4, "w")
    c.set_hw_watch(1, 2, 0x4000, 4, "r")
    c.clear_hw_bp(2, 0)
    c.clear_hw_watch(1, 0)
    assert o.log[:2] == [("select", 2), ("cmd", "bp 0x10004 4 hw")]
    sent = o.sent()
    assert "wp 0x2000 4 a" in sent and "wp 0x3000 4 w" in sent and "wp 0x4000 4 r" in sent
    assert "rbp 0x10004" in sent and "rwp 0x2000" in sent


def test_cp15_register_names_map_to_mrc_encodings():
    o = FakeOcd(arm_mrc="0x40004000")
    c = cores(o)
    assert c.read_cp15(1, "ttbr0") == 0x40004000
    for name, enc in [("ttbr1", "2 0 1"), ("ttbcr", "2 0 2"), ("contextidr", "13 0 1"), ("sctlr", "1 0 0")]:
        c.read_cp15(1, name)
        assert "arm mrc 15 0 %s" % enc in o.sent()
    with pytest.raises(KeyError):
        c.read_cp15(1, "dfar")


def test_snapshot_decodes_state_pc_cpsr_entry_reason_and_stack():
    o = FakeOcd(lx_snap="0 halted 10004 60000010 2a00 0xc 80123450\n1 running")
    snap = cores(o).snapshot()
    assert snap[0] == CoreState(True, 0x10004, 0x60000010, 0x2a00, MOE_BKPT_INSN, 0x80123450, 0x2a)
    assert snap[1] == CoreState(False)
    d = FakeOcd(lx_snap="0 halted 10 13 0 0x28 0")
    assert cores(d).snapshot([0])[0].moe == MOE_WATCH_SYNC            # DSCR 0x28 -> MOE bits [5:2] = 0xA


def test_registers_roundtrip_through_get_reg_and_set_reg():
    names = ["r%d" % i for i in range(13)] + ["sp", "lr", "pc", "cpsr"]
    o = FakeOcd(get_reg="\n".join("%s %x" % (n, i) for i, n in enumerate(names)))
    c = cores(o)
    assert c.regs(1) == list(range(17))
    c.set_regs(1, list(range(17)))
    assert any(s.startswith("set_reg {r0 0x0 r1 0x1") for s in o.sent())


def test_sync_caches_invalidates_icache_and_branch_predictor_on_the_named_cores_only():
    o = FakeOcd()
    cores(o).sync_caches([1, 3])
    mcrs = [(x[1]) for x in o.log if x[0] == "cmd" and x[1].startswith("arm mcr")]
    assert mcrs == ["arm mcr 15 0 7 5 0 0", "arm mcr 15 0 7 5 6 0"] * 2
    assert [x for x in o.log if x[0] == "select"] == [("select", 1), ("select", 3)]


def test_release_gives_openocd_its_background_poll_back():
    o = FakeOcd()
    cores(o).release()
    assert o.sent() == ["poll on"]


def test_the_slot_counts_are_required_facts_of_the_cores_debug_unit():
    with pytest.raises(TypeError):
        OpenOcdCores(FakeOcd(), board=RPI2B, jtag_khz=4000, **LINEAR)
    c = cores(FakeOcd())
    assert (c.hw_slots, c.watch_slots) == (5, 4)


def test_read_kernel_and_write_kernel_use_the_address_as_given_through_core_0s_mmu():
    o = FakeOcd(read_memory="0x11223344 0x55667788")
    c = cores(o)
    assert c.read_kernel(0xE0001000, 8) == bytes.fromhex("4433221188776655")
    assert ("select", 0) in o.log and "read_memory 0xe0001000 32 2" in o.sent()
    c.write_kernel(0xE0001004, bytes.fromhex("01000000"))
    assert "write_memory 0xe0001004 32 {0x1}" in o.sent()
    assert any(s.startswith("arm mcr 15 0 7 11 1 ") for s in o.sent())      # cleaned to the PoU


def test_physical_access_is_bounded_by_the_linear_map_end():
    c = cores(FakeOcd(read_memory="0x1"))
    with pytest.raises(OutsideLinearMap):
        c.read_phys(0x08000000, 4)
    with pytest.raises(OutsideLinearMap):
        c.write_phys(0x07FFFFFE, b"abcd")


def test_the_board_is_required():
    with pytest.raises(TypeError):
        OpenOcdCores(FakeOcd(), jtag_khz=4000, hw_slots=5, watch_slots=4, **LINEAR)


def test_a_two_core_board_names_its_own_targets_dap_and_apb_ap_in_the_procedures():
    """Zynq-7000 (PYNQ-Z2): two A9 cores `zynq.cpu<n>`, the debug registers behind AP 1 (APB-AP; AP 0 is the AHB-AP)."""
    o = FakeOcd(adapter_speed="adapter speed: 1000 kHz", lx_states="running running")
    c = cores(o, board=ZYNQ7000, jtag_khz=1000)
    c.setup()
    assert c.n_cores == 2
    sent = o.sent()
    assert sent.count("cortex_a maskisr on") == 2 and [x for x in o.log if x[0] == "select"] == [("select", 0), ("select", 1)]
    assert "set lx_t zynq.cpu; set lx_dap zynq.dap; set lx_ap 1; set lx_cores {0 1}" in sent
    procs = " ".join(s for s in sent if s.startswith("proc "))
    assert "rpi2" not in procs and "{0 1 2 3}" not in procs
    assert c.halted() == (False, False)
    assert o.prefix == "zynq.cpu"
