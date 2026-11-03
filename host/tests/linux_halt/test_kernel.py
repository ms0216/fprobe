"""fprobe.linux_halt.kernel against a synthetic kernel memory (task_structs built by hand). Not a hardware test."""
import struct

import pytest

from fprobe.linux_halt.kernel import (Kernel, KernelLayoutError, ProcessNotFound, build_layout, list_threads,
                                      load_layout_json, verify_attach)

BASE = 0xC0000000
INIT = BASE + 0x1000
SZ = 0x200
OFF = {"tasks": 0x10, "pid": 0x20, "tgid": 0x24, "mm": 0x28, "stack": 0x2C, "signal": 0x30, "thread_group": 0x34,
       "comm": 0x40, "pid_links": 0x60, "thread_info": 0x70}


class Memory:
    def __init__(self):
        self.b = bytearray(0x20000)

    def read(self, va, n):
        assert BASE <= va and va + n <= BASE + len(self.b), "read outside the synthetic memory: 0x%x" % va
        return bytes(self.b[va - BASE:va - BASE + n])

    def w32(self, va, v):
        struct.pack_into("<I", self.b, va - BASE, v)

    def task(self, addr, pid, tgid, mm, comm, stack=0):
        self.w32(addr + OFF["pid"], pid)
        self.w32(addr + OFF["tgid"], tgid)
        self.w32(addr + OFF["mm"], mm)
        self.w32(addr + OFF["stack"], stack)
        self.b[addr - BASE + OFF["comm"]:addr - BASE + OFF["comm"] + 16] = comm.encode().ljust(16, b"\0")


def layout(**extra):
    o = dict(OFF, init_task=INIT, sizeof_pt_regs=72, thread_size=8192, page_offset=0x80000000, phys_offset=0,
             linear_end=BASE + 0x1000000)
    o.update(extra)
    return type("L", (), {"o": o})()


def kernel_with(procs, max_tasks=100, max_threads=16, mem=None):
    """procs: [(tgid, [tid, ...], comm)]. init_task first, then the leaders in order."""
    m = mem or Memory()
    m.task(INIT, 0, 0, 0, "swapper/0")
    leaders = [INIT]
    nxt = INIT + SZ
    for tgid, tids, comm in procs:
        addrs = []
        for tid in tids:
            m.task(nxt, tid, tgid, 0x5000 + tgid, comm)
            addrs.append(nxt)
            nxt += SZ
        leaders.append(addrs[0])
        ring = [a + OFF["thread_group"] for a in addrs]            # the leader's member is the head of the list
        for i, a in enumerate(addrs):
            m.w32(a + OFF["thread_group"], ring[(i + 1) % len(ring)])
    for i, a in enumerate(leaders):
        m.w32(a + OFF["tasks"], leaders[(i + 1) % len(leaders)] + OFF["tasks"])
    return Kernel(m.read, layout(), max_tasks=max_tasks, max_threads=max_threads), m


def test_verify_attach_accepts_init_task():
    k, m = kernel_with([(100, [100], "app")])
    verify_attach(layout(), m.read)


def test_verify_attach_rejects_wrong_vmlinux():
    k, m = kernel_with([(100, [100], "app")])
    m.task(INIT, 0, 0, 0, "notswapper")
    with pytest.raises(KernelLayoutError, match="init_task"):
        verify_attach(layout(), m.read)
    m.task(INIT, 7, 0, 0, "swapper/0")                             # right name, wrong pid: other offsets
    with pytest.raises(KernelLayoutError):
        verify_attach(layout(), m.read)


def test_list_threads_finds_all_threads_of_pid_leader_first():
    k, m = kernel_with([(100, [100, 101, 102], "app"), (200, [200], "other")])
    threads = list_threads(layout(), m.read, 100, max_tasks=100, max_threads=16)
    assert [t.pid for t in threads] == [100, 101, 102]
    assert all(t.tgid == 100 and t.comm == "app" for t in threads)


def test_list_threads_exceeding_max_tasks_raises_not_truncates():
    k, m = kernel_with([(100 + i, [100 + i], "p%d" % i) for i in range(8)])
    with pytest.raises(KernelLayoutError, match="init_task"):
        list_threads(layout(), m.read, 107, max_tasks=5, max_threads=16)


def test_unknown_pid_raises_process_not_found():
    k, m = kernel_with([(100, [100], "app")])
    with pytest.raises(ProcessNotFound):
        list_threads(layout(), m.read, 999, max_tasks=100, max_threads=16)


def test_kernel_thread_has_no_address_space():
    k, m = kernel_with([(100, [100], "kthreadd")])
    t = k.find_leader(100)
    m.w32(t.addr + OFF["mm"], 0)
    with pytest.raises(ProcessNotFound, match="kernel thread"):
        list_threads(layout(), m.read, 100, max_tasks=100, max_threads=16)


def test_corrupt_thread_list_raises_after_max_threads():
    k, m = kernel_with([(100, [100, 101], "app")], max_threads=3)
    t = k.find_leader(100)
    other = t.addr + SZ
    m.w32(other + OFF["thread_group"], other + OFF["thread_group"])      # a list that never returns to the head
    with pytest.raises(KernelLayoutError, match="thread list"):
        k.threads(t.addr)


def test_pt_regs_is_at_the_top_of_the_kernel_stack():
    k, m = kernel_with([(100, [100], "app")])
    t = k.find_leader(100)
    m.w32(t.addr + OFF["stack"], BASE + 0x8000)
    assert k.pt_regs_addr(t) == BASE + 0x8000 + 8192 - 8 - 72


def test_find_by_pid_idr_single_entry_and_node():
    """init_pid_ns.idr's xa_head: a single entry only holds pid 0; an internal node (tag 2) is walked by shift."""
    o = dict(init_pid_ns=BASE + 0x2000, pid_idr_head=0, xa_node_shift=0, xa_node_slots=8, pid_tasks=4)
    k, m = kernel_with([(100, [100], "app")])
    k.o.update(o)
    leader = k.find_leader(100)
    pid_struct = BASE + 0x3000
    m.w32(pid_struct + o["pid_tasks"], leader.addr + OFF["pid_links"])
    leaf, root = BASE + 0x4000, BASE + 0x5000
    m.b[leaf - BASE + o["xa_node_shift"]] = 0                       # leaf: slots indexed by pid & 63
    m.w32(leaf + o["xa_node_slots"] + 4 * (100 & 63), pid_struct)
    m.b[root - BASE + o["xa_node_shift"]] = 6                       # root: slot pid >> 6 -> the leaf (pid 100 -> slot 1)
    m.w32(root + o["xa_node_slots"] + 4 * (100 >> 6), leaf + 2)
    m.w32(o["init_pid_ns"] + o["pid_idr_head"], root + 2)
    k2 = Kernel(m.read, type("L", (), {"o": dict(k.o)})(), max_tasks=100, max_threads=16)
    t = k2.find_by_pid(100)
    assert t is not None and t.pid == 100
    assert k2.find_leader(100).pid == 100 and k2.found_by == "idr"


def test_build_layout_reports_every_missing_field():
    def offset_of(struct_name, path):
        return None if path == "pid" else 8
    with pytest.raises(KernelLayoutError) as e:
        build_layout(offset_of, sizeof_pt_regs=72, symbols={"init_task": 1, "init_pid_ns": 2, "do_exit": 3},
                     page_offset=0x80000000, phys_offset=0, thread_size=8192, signal_group_exit=4, linear_end=0xC0000000)
    assert "task_struct.pid" in str(e.value)


def test_build_layout_picks_the_new_thread_list_layout_when_thread_group_is_absent():
    def offset_of(struct_name, path):
        return None if path == "thread_group" else 8
    lay = build_layout(offset_of, sizeof_pt_regs=72, symbols={"init_task": 1, "init_pid_ns": 2, "do_exit": 3},
                       page_offset=0x80000000, phys_offset=0, thread_size=8192, signal_group_exit=4, linear_end=0xC0000000)
    assert "thread_node" in lay.o and "thread_head" in lay.o and "thread_group" not in lay.o


def test_build_layout_requires_symbols():
    with pytest.raises(KernelLayoutError, match="init_task"):
        build_layout(lambda s, p: 8, sizeof_pt_regs=72, symbols={"init_pid_ns": 2, "do_exit": 3},
                     page_offset=0x80000000, phys_offset=0, thread_size=8192, signal_group_exit=4, linear_end=0xC0000000)


# --- the prototype's offsets file, and reading kernel virtual memory through the linear map ---------------------------------

FULL = {k: 8 for k in ("tasks", "pid", "tgid", "comm", "mm", "stack", "thread_info", "pgd", "asid", "saved_auxv", "signal", "live",
                       "pid_idr_head", "xa_node_shift", "xa_node_slots", "pid_tasks", "pid_links", "signal_flags",
                       "group_exit_code", "thread_group")}
FULL.update(init_task=0xC1000000, init_pid_ns=0xC1000100, do_exit=0xC1000200, sizeof_pt_regs=72, thread_size=8192,
            signal_group_exit=4)


def write_json(tmp_path, data):
    import json
    p = tmp_path / "offsets.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_load_layout_json_reads_a_complete_file_and_takes_the_two_missing_settings_as_arguments(tmp_path):
    lay = load_layout_json(write_json(tmp_path, FULL), page_offset=0x80000000, phys_offset=0, linear_end=0xBB400000)
    assert lay.o["linear_end"] == 0xBB400000 and lay.o["init_task"] == 0xC1000000 and lay.o["page_offset"] == 0x80000000 and lay.o["phys_offset"] == 0


def test_load_layout_json_has_no_default_for_the_linear_map(tmp_path):
    with pytest.raises(TypeError):
        load_layout_json(write_json(tmp_path, FULL), page_offset=0x80000000, phys_offset=0)   # no linear_end


def test_load_layout_json_names_every_missing_key(tmp_path):
    data = {k: v for k, v in FULL.items() if k not in ("pgd", "do_exit", "thread_group")}
    with pytest.raises(KernelLayoutError) as e:
        load_layout_json(write_json(tmp_path, data), page_offset=0x80000000, phys_offset=0, linear_end=0xBB400000)
    msg = str(e.value)
    assert "pgd" in msg and "do_exit" in msg and "thread" in msg


def test_load_layout_json_accepts_the_newer_thread_list_layout(tmp_path):
    data = {k: v for k, v in FULL.items() if k != "thread_group"}
    data.update(thread_node=8, thread_head=8)
    lay = load_layout_json(write_json(tmp_path, data), page_offset=0x80000000, phys_offset=0, linear_end=0xBB400000)
    assert "thread_node" in lay.o


def test_v2p_refuses_addresses_outside_the_linear_map_so_a_vmapped_stack_is_not_written_through_a_wrong_physical_address():
    k, m = kernel_with([(100, [100], "app")])
    k.o["linear_end"] = BASE + 0x1000
    assert k.v2p(BASE + 0x10) == BASE + 0x10 - 0x80000000
    with pytest.raises(KernelLayoutError, match="linear map"):
        k.v2p(BASE + 0x1000)
    with pytest.raises(KernelLayoutError, match="linear map"):
        k.v2p(0x7FFFFFFF)


def test_xa_is_node_follows_the_kernel_definition():
    # include/linux/xarray.h: xa_is_node(e) = xa_is_internal(e) && e > 4096。内部の値（xa_mk_internal(v) = v << 2 | 2）のうち
    # 4096 以下は XA_RETRY_ENTRY（0x402）・XA_ZERO_ENTRY（0x406）・兄弟の印などでノードではない（経験則の閾値ではない。C8）
    from fprobe.linux_halt.kernel import xa_is_node
    assert xa_is_node(0xC1234002)
    assert not xa_is_node(0x402) and not xa_is_node(0x406) and not xa_is_node(4094)
    assert not xa_is_node(0xC1234000) and not xa_is_node(0xC1234001)       # 普通の指す値・値の印
