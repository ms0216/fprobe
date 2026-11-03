import struct

import pytest

from fprobe.linux_halt.kernel import Kernel, KernelLayoutError, ProcessNotFound
from fprobe.linux_halt.process import Process, ProcessExited
from fprobe.linux_halt.sim_cores import SimCores
from .test_kernel import BASE, OFF, Memory, kernel_with, layout

MM = BASE + 0x9000
PGD_VA = BASE + 0x4000
EXTRA = dict(pgd=0x10, saved_auxv=0x20)


def make(tgid=100, tids=(100, 101), auxv=None):
    k0, m = kernel_with([(tgid, list(tids), "app")])
    k = Kernel(m.read, layout(**EXTRA), max_tasks=100, max_threads=16)
    leader = k.find_leader(tgid)
    for t in k.threads(leader.addr):
        m.w32(t.addr + OFF["mm"], MM)
    m.w32(MM + EXTRA["pgd"], PGD_VA)
    if auxv is not None:
        m.b[MM - BASE + EXTRA["saved_auxv"]:MM - BASE + EXTRA["saved_auxv"] + len(auxv)] = auxv
    return k, m, SimCores(2, simultaneous=False, skew_upper_bound_ns=None)


def test_attach_reads_leader_pgd_and_builds_the_address_space():
    k, m, cores = make()
    p = Process(k, cores, 100, fmt="short")
    assert p.tgid == 100 and p.mm == MM
    assert p.mem.table_pa == PGD_VA - 0x80000000                  # kernel virtual -> physical by the linear map
    assert p.tids() == {100, 101}
    assert p.task(101).pid == 101 and p.task(5) is None
    assert p.comm == "app"


def test_unknown_pid_and_kernel_threads_are_refused():
    k, m, cores = make()
    with pytest.raises(ProcessNotFound):
        Process(k, cores, 999, fmt="short")
    m.w32(k.find_leader(100).addr + OFF["mm"], 0)
    with pytest.raises(ProcessNotFound, match="kernel thread"):
        Process(k, cores, 100, fmt="short")


def test_refresh_raises_process_exited_when_the_task_is_no_longer_ours():
    k, m, cores = make()
    p = Process(k, cores, 100, fmt="short")
    assert p.alive()
    m.w32(p.leader.addr + OFF["pid"], 4242)                       # the task_struct was recycled
    assert not p.alive()
    with pytest.raises(ProcessExited):
        p.refresh_threads()


def test_auxv_is_returned_through_its_terminator():
    aux = struct.pack("<4I", 6, 4096, 0, 0) + b"\xff" * 16
    k, m, cores = make(auxv=aux)
    p = Process(k, cores, 100, fmt="short")
    assert p.auxv() == struct.pack("<4I", 6, 4096, 0, 0)


def test_auxv_without_a_terminator_raises_not_truncates():
    k, m, cores = make(auxv=b"\x01\x00\x00\x00\x02\x00\x00\x00" * 0x40)
    p = Process(k, cores, 100, fmt="short")
    with pytest.raises(KernelLayoutError, match="AT_NULL"):
        p.auxv()


def test_owner_of_stack_address_picks_the_thread_whose_stack_contains_it():
    k, m, cores = make()
    p = Process(k, cores, 100, fmt="short")
    t0, t1 = p.threads
    m.w32(t0.addr + OFF["stack"], BASE + 0x10000)
    m.w32(t1.addr + OFF["stack"], BASE + 0x12000)
    assert k.owner_of_stack_address(p.threads, BASE + 0x10000 + 100).pid == 100
    assert k.owner_of_stack_address(p.threads, BASE + 0x12000 + 8191).pid == 101
    assert k.owner_of_stack_address(p.threads, BASE + 0x14000) is None
