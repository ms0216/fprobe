"""fprobe.linux_halt.service（CLI と API の linux.halt.* の中身）を、模擬のコア（SimCores）と合成のカーネルのメモリで試す。実機ではない。"""
import contextlib
import socket
import threading
import time

import pytest

from fprobe.gdb import rsp as framing
from fprobe.linux_halt.core_access import MOE_HALT
from fprobe.linux_halt.engine import EngineError
from fprobe.linux_halt.service import Busy, HaltOptions, HaltSession, OptionsError, journal_facts
from fprobe.linux_halt.sim_cores import SimCores
from fprobe.raw_dap import Ack, DapError
from .helpers import map_page
from .test_engine import CODE, EXTRA, MM, PA, PGD_VA, SIG, STACK
from .test_kernel import OFF, kernel_with, layout


def options(tmp_path, **kw):
    base = dict(backend="fprobe", jtag_khz=2000, offsets="offsets.json", page_offset=0xC0000000, phys_offset=0,
                linear_map_end=0xC0100000, max_tasks=100, max_threads=16, mmu_format="short",
                journal=str(tmp_path / "j.json"), fpga_request_times=False, part="zynq7000")
    base.update(kw)
    return HaltOptions(**base)


class FakeCores(SimCores):
    """SimCores + ArmACores だけが持つ口（programmed・rescue・停止デバッグ）。カーネルは合成のメモリから読む。"""

    def __init__(self, kernel_read, **kw):
        super().__init__(4, simultaneous=True, skew_upper_bound_ns=50, **kw)
        self.hw_slots, self.watch_slots = 2, 2
        self._kread = kernel_read
        self.hdbgen = True
        self.cross_released = 0
        self.calls = []

    def read_kernel(self, va, length):
        return self._kread(va, length)

    def programmed(self):
        return {"running": [not h for h in self.halted()],
                "breakpoints": {c: [s] for (c, s) in self.hw_bps}, "watchpoints": {}}

    def rescue(self):
        halted = [n for n, h in enumerate(self.halted()) if h]
        self.hw_bps.clear()
        self.hdbgen = False
        self.resume(halted)
        return tuple(halted)

    def disable_halting_debug(self):
        self.hdbgen = False

    def enable_halting_debug(self):
        self.calls.append("enable")
        self.hdbgen = True

    def release_cross_halt(self):
        self.cross_released += 1


class DyingCores(FakeCores):
    """線の化けで DP の CTRL/STAT が 0 に書かれ（デバッグの電源の要求が消え）、DAP が丸ごと使えなくなった状態（Pi 2B で見た。
    2026-11-02）: dead の間はどの操作も DapError。reconnect（DP をつなぎ直して電源を求め直す）で戻る。"""
    TOUCHES_THE_DAP = ("halted", "halt_all", "resume", "wait_event", "snapshot", "read_phys", "write_phys", "read_kernel",
                       "clear_hw_bp", "set_hw_bp", "clear_hw_watch", "sync_caches", "disable_halting_debug", "programmed")

    def __init__(self, kernel_read, **kw):
        super().__init__(kernel_read, **kw)
        self.dead, self.reconnects = False, 0

    def reconnect(self):
        self.reconnects += 1
        self.dead = False


def _dies(name):
    def method(self, *a, **kw):
        if self.dead:
            raise DapError(Ack.NO_ACK, "%s (simulated dead DAP)" % name)
        return getattr(FakeCores, name)(self, *a, **kw)
    return method


for _name in DyingCores.TOUCHES_THE_DAP:
    setattr(DyingCores, _name, _dies(_name))


class Rig:
    def __init__(self, tmp_path, lock=None, cores_cls=None, **opts):
        _, m = kernel_with([(100, [100, 101], "app"), (200, [200], "kworker")])
        from fprobe.linux_halt.kernel import Kernel
        k = Kernel(m.read, layout(**EXTRA), max_tasks=100, max_threads=16)
        for t in k.threads(k.find_leader(100).addr):
            m.w32(t.addr + OFF["mm"], MM)
            m.w32(t.addr + OFF["stack"], STACK[t.pid])
            m.w32(t.addr + OFF["signal"], SIG)
        m.w32(k.find_leader(200).addr + OFF["mm"], 0)                  # a kernel thread
        m.w32(MM + EXTRA["pgd"], PGD_VA)
        m.w32(SIG + EXTRA["live"], 2)
        self.cores = (cores_cls or FakeCores)(m.read)
        map_page(self.cores, 0x10000, PA)
        self.cores.write_phys(PA, CODE)
        self.closed = 0
        self.opts = options(tmp_path, **opts)
        self.s = HaltSession(self.opts, layout(**EXTRA), self.cores, self._close, (RuntimeError,),
                             {"backend": "fprobe", "n_cores": 4}, lock)

    def _close(self):
        self.closed += 1


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.s.close()


# --- the options: no defaults, the same refusals as the command line -------------------------------------------------

def test_the_options_refuse_an_incomplete_cti_or_watch_and_wrong_combinations(tmp_path):
    options(tmp_path).check()
    with pytest.raises(OptionsError, match="needs --part"):
        options(tmp_path, part=None).check()
    with pytest.raises(OptionsError, match="CTI needs every one"):
        options(tmp_path, cti_board="zynq7000").check()
    with pytest.raises(OptionsError, match="both"):
        options(tmp_path, fpga_halt_watch_bound_ns="unmeasured").check()
    cti = dict(cti_board="zynq7000", cti_request_bound_ns=74, cti_event_bound_ns=277, cti_hw_wait_s=0.05, cti_ack_polls=100)
    assert options(tmp_path, **cti).check().cti
    with pytest.raises(OptionsError, match="not used together"):
        options(tmp_path, **cti, fpga_halt_watch_bound_ns=1, fpga_halt_watch_poll_s=0.0).check()
    with pytest.raises(OptionsError, match="without a CTI"):
        options(tmp_path, **cti, fpga_request_times=True).check()
    with pytest.raises(OptionsError, match="ns or unmeasured"):
        options(tmp_path, fpga_halt_watch_bound_ns="soon", fpga_halt_watch_poll_s=0.0).check()
    with pytest.raises(OptionsError, match="openocd needs --ocd-board"):
        options(tmp_path, backend="openocd").check()


def test_the_options_report_only_what_was_given(tmp_path):
    d = options(tmp_path).as_dict()
    assert "cti_board" not in d and d["fpga_request_times"] is False and d["part"] == "zynq7000"


# --- reading without the gdb ------------------------------------------------------------------------------------------

def test_processes_halt_read_and_resume_and_return_the_stop_report_as_it_is(rig):
    r = rig.s.processes()
    assert r["processes"] == [{"tgid": 100, "comm": "app", "has_mm": True}, {"tgid": 200, "comm": "kworker", "has_mm": False}]
    assert r["stop"] == {"method": "simultaneous", "cores_halted": [0, 1, 2, 3], "skew_upper_bound_ns": 50,
                         "basis": "simulated", "assumptions": [], "bound_without_assumptions_ns": None}
    assert r["resumed"] is True and not any(rig.cores.halted())


def test_reading_while_the_user_holds_every_core_halted_does_not_halt_again_nor_resume(rig):
    held = rig.s.halt_all()
    assert held["stop"]["cores_halted"] == [0, 1, 2, 3]
    r = rig.s.threads(100)
    assert [t["tid"] for t in r["threads"]] == [100, 101] and r["comm"] == "app"
    assert r["stop"] is None and r["resumed"] is False and all(rig.cores.halted())
    rig.s.resume_all()
    assert not any(rig.cores.halted())


def test_a_partial_halt_is_refused_and_the_halted_cores_are_resumed(tmp_path):
    r = Rig(tmp_path)
    r.cores._fail = frozenset({2})
    with pytest.raises(EngineError, match="not every core halted"):
        r.s.processes()
    assert not any(r.cores.halted())


def test_status_reads_the_cores_and_the_journal_without_halting(rig, tmp_path):
    (tmp_path / "j.json").write_text('{"entries": [{"pa": 4096, "width": 4, "orig": "00000000", "owners": [[100, 65536]]}]}')
    s = rig.s.status()
    assert [c["running"] for c in s["cores"]] == [True] * 4
    assert s["journal"] == {"path": str(tmp_path / "j.json"), "exists": True,
                            "entries": [{"pa": 4096, "width": 4, "owners": [[100, 65536]]}]}
    assert s["gdb"] is None and s["options"]["part"] == "zynq7000" and not any(rig.cores.halted())


def test_recover_reports_what_was_and_was_not_restored(rig, tmp_path):
    from fprobe.linux_halt.bp import BKPT
    rig.cores.write_phys(0x1000, BKPT[4])
    (tmp_path / "j.json").write_text('{"entries": [{"pa": 4096, "width": 4, "orig": "11223344", "owners": []},'
                                     ' {"pa": 8192, "width": 4, "orig": "55667788", "owners": []}]}')
    r = rig.s.recover()
    assert (r["restored"], r["stale"]) == (1, 1) and len(r["journal_before"]["entries"]) == 2
    assert rig.cores.read_phys(0x1000, 4) == bytes.fromhex("11223344")
    assert journal_facts(str(tmp_path / "j.json"))["exists"] is False


def test_rescue_releases_the_cti_turns_halting_debug_off_and_the_next_read_turns_it_back_on(tmp_path):
    r = Rig(tmp_path, cti_board="zynq7000", cti_request_bound_ns=74, cti_event_bound_ns="unmeasured", cti_hw_wait_s=0.05,
            cti_ack_polls=100)
    r.cores.halt_all()
    out = r.s.rescue()
    assert out["resumed"] == [0, 1, 2, 3] and out["cti_released"] and r.cores.cross_released == 1
    assert r.s.halting_debug is False and r.cores.hdbgen is False
    r.s.processes()
    assert r.cores.calls == ["enable"] and r.s.halting_debug


def test_every_core_operation_takes_the_lock_given(tmp_path):
    class CountingLock:
        def __init__(self):
            self.n = 0
            self._l = threading.RLock()

        def __enter__(self):
            self.n += 1
            self._l.acquire()

        def __exit__(self, *e):
            self._l.release()
    lock = CountingLock()
    r = Rig(tmp_path, lock=lock)
    r.s.halt_all()
    assert lock.n >= 1


def test_close_does_not_change_the_cores_and_reports_what_it_read(rig):
    rig.s.halt_all()
    out = rig.s.close()
    assert out["halted"] == [True] * 4 and rig.closed == 1 and all(rig.cores.halted())
    with pytest.raises(EngineError, match="closed"):
        rig.s.halt_all()


# --- the gdb server in its own thread ---------------------------------------------------------------------------------

def free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(rig, **kw):
    port = free_port()
    args = dict(pid=100, port=port, skew_limit_ns=68, thread_id="stack", kernel_isa="arm", max_step_retries=8,
                bp_unmapped="error")
    args.update(kw)
    for c in range(4):
        rig.cores.states[c] = rig.cores.states[c].__class__(False, pc=0x10000, cpsr=0x10, moe=MOE_HALT,
                                                            sp_svc=STACK[100] + 8000)
    return rig.s.gdb_serve(**args), port


def packet(sock, payload):
    sock.sendall(framing.frame(payload))
    reader, buf = framing.PacketReader(), []
    sock.settimeout(5)
    while True:
        for e in reader.feed(sock.recv(65536)):
            if isinstance(e, framing.Packet):
                return e.payload
        buf.append(1)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_gdb_serve_recovers_the_journal_waits_running_and_serves_a_gdb_until_it_detaches(rig, tmp_path):
    r, port = serve(rig)
    assert r["state"] == "listening" and r["pid"] == 100 and r["connect"] == "target remote 127.0.0.1:%d" % port
    assert r["recovered"]["restored"] == 0 and r["stop_for_attach"]["method"] == "simultaneous"
    assert not any(rig.cores.halted())                                          # waits for gdb with the system running
    with socket.create_connection(("127.0.0.1", port)) as g:
        assert packet(g, b"?").startswith(b"T05")
        assert wait_for(lambda: rig.s.gdb_status()["state"] == "serving")
        st = rig.s.gdb_status()
        assert st["attach_stop"]["cores_halted"] == [0, 1, 2, 3]
        assert st["last_stop"]["skew_verdict"] == "within" and st["last_stop"]["skew_limit_ns"] == 68
        assert rig.cores.hw_bps                                                 # the do_exit breakpoint of the engine
        assert packet(g, b"D") == b"OK"
    assert wait_for(lambda: rig.s.gdb_status()["state"] == "ended")
    st = rig.s.gdb_status()
    assert st["ended_by"] == "detach" and st["cleanup_errors"] == []
    assert not rig.cores.hw_bps and not any(rig.cores.halted()) and rig.cores.hdbgen is False


def test_when_the_dap_dies_during_the_session_the_cleanup_reconnects_and_still_removes_every_breakpoint(tmp_path):
    # 実機（2026-11-02）: 化けた転送で DAP が使えなくなると、後始末（止める・BKPT と HW の bp を外す・停止デバッグを切る）が全部失敗し、
    # do_exit の HW の bp と停止デバッグが残って、終了するプロセスごとにコアが止まり Pi が固まった。後始末のときだけ DP をつなぎ直す。
    # 最初の失敗は cleanup_errors に残す（隠さない）。つなぎ直した事実も返す
    rig = Rig(tmp_path, cores_cls=DyingCores)
    try:
        _, port = serve(rig)
        with socket.create_connection(("127.0.0.1", port)) as g:
            assert packet(g, b"?").startswith(b"T05")
            assert wait_for(lambda: rig.s.gdb_status()["state"] == "serving")
            assert rig.cores.hw_bps                                             # the do_exit breakpoint of the engine
            rig.cores.dead = True
            g.sendall(framing.frame(b"D"))
            assert wait_for(lambda: rig.s.gdb_status()["state"] == "ended")
        st = rig.s.gdb_status()
        assert st["cleanup_errors"]                                             # the failures before the reconnect are reported
        assert st["cleanup_reconnected"] is True and rig.cores.reconnects == 1
        assert st["cleanup_errors_after_reconnect"] == []
        assert not rig.cores.hw_bps and rig.cores.hdbgen is False and not any(rig.cores.halted())
    finally:
        rig.s.close()


def test_while_gdb_is_served_the_core_operations_are_refused_and_status_does_not_read_the_cores(rig):
    serve(rig)
    for op in (rig.s.halt_all, rig.s.resume_all, rig.s.processes, rig.s.recover, rig.s.rescue, lambda: rig.s.threads(100)):
        with pytest.raises(Busy, match="gdb server"):
            op()
    s = rig.s.status()
    assert s["cores"] is None and "gdb server" in s["cores_unread"] and s["gdb"]["state"] == "listening"
    out = rig.s.gdb_stop()
    assert out["state"] == "ended" and out["ended_by"] == "stop"
    rig.s.halt_all()                                                            # the session is free again


def test_gdb_stop_cuts_a_connected_gdb_and_waits_for_the_cleanup(rig):
    _, port = serve(rig)
    with socket.create_connection(("127.0.0.1", port)) as g:
        assert packet(g, b"?").startswith(b"T05")
        assert wait_for(lambda: rig.s.gdb_status()["state"] == "serving")
        out = rig.s.gdb_stop()
    assert out["state"] == "ended" and out["ended_by"] == "stop"
    assert not rig.cores.hw_bps and not any(rig.cores.halted())


def test_close_stops_the_gdb_server_first(rig):
    serve(rig)
    out = rig.s.close()
    assert out["gdb"]["ended_by"] == "stop" and rig.closed == 1


def test_gdb_serve_refuses_unknown_choices_before_touching_the_cores(rig):
    with pytest.raises(EngineError, match="thread_id"):
        serve(rig, thread_id="tls")
    assert rig.s.gdb_status() is None and not any(rig.cores.halted())
