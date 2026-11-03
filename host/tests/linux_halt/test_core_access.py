from fprobe.linux_halt.core_access import CoreAccess, StopReport
from fprobe.linux_halt.sim_cores import SimCores


def test_halt_all_reports_method_and_bound():
    cores = SimCores(4, simultaneous=True, skew_upper_bound_ns=68)
    report = cores.halt_all()
    assert report.method == "simultaneous"
    assert report.cores_halted == (0, 1, 2, 3)
    assert report.skew_upper_bound_ns == 68
    assert isinstance(report, StopReport)


def test_unknown_bound_is_none_not_zero():
    report = SimCores(2, simultaneous=False, skew_upper_bound_ns=None).halt_all()
    assert report.method == "sequential"
    assert report.skew_upper_bound_ns is None


def test_partial_halt_failure_reports_which_cores():
    cores = SimCores(4, simultaneous=False, skew_upper_bound_ns=None, fail_halt_on=frozenset({2}))
    report = cores.halt_all()
    assert report.cores_halted == (0, 1, 3)
    assert cores.halted() == (True, True, False, True)


def test_resume_all_runs_every_core():
    cores = SimCores(2, simultaneous=True, skew_upper_bound_ns=68)
    cores.halt_all()
    cores.resume_all()
    assert cores.halted() == (False, False)


def test_sim_cores_satisfy_the_interface():
    cores: CoreAccess = SimCores(2, simultaneous=True, skew_upper_bound_ns=68)
    cores.write_phys(0x1000, b"\x01\x02\x03\x04")
    assert cores.read_phys(0x1000, 4) == b"\x01\x02\x03\x04"
    cores.write_reg(1, "r0", 7)
    assert cores.read_reg(1, "r0") == 7


def test_bulk_registers_roundtrip_and_do_not_clash_with_named_registers():
    cores = SimCores(2, simultaneous=True, skew_upper_bound_ns=68)
    cores.write_reg(0, "r0", 5)
    cores.set_regs(0, list(range(17)))
    assert cores.regs(0) == list(range(17))
    assert cores.read_reg(0, "r0") == 5
