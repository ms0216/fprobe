"""Engine.run（待つ上限つきの再開）と Engine.halt（走っている全コアを要求で止める）。仕様 2026-11-03-linux-halt-ai-tools-design.md §5.3。"""
import pytest

from fprobe.linux_halt.core_access import MOE_BKPT_INSN, MOE_HALT
from .test_engine import World


class Clock:
    """wait_event を呼ぶたびに進む時計（SimCores の wait_event は待たずに返る）。"""
    def __init__(self, cores, step=0.02):
        self.t, self.step = 0.0, step
        original = cores.wait_event

        def wait_event(timeout_s):
            self.t += self.step
            return original(timeout_s)
        cores.wait_event = wait_event

    def __call__(self):
        return self.t


def test_a_hit_within_the_limit_is_reported_with_every_core_halted(tmp_path):
    w = World(tmp_path)
    clock = Clock(w.cores)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(1, 101, 0x10004)
    w.cores.queue_event(1)
    w.eng.stop()
    stop = w.eng.run(timeout_s=1.0, clock=clock)
    assert (stop.kind, stop.tid, stop.addr) == ("swbreak", 101, 0x10004)
    assert all(w.cores.halted()) and w.eng.running is False


def test_the_time_limit_returns_timeout_and_leaves_every_core_running(tmp_path):
    w = World(tmp_path)
    clock = Clock(w.cores)
    w.eng.stop()
    stop = w.eng.run(timeout_s=0.1, clock=clock)
    assert stop.kind == "timeout"
    assert not any(w.cores.halted()) and w.eng.running is True
    assert 0.1 <= clock() < 0.2                                     # 上限を過ぎた最初の確かめで返った


def test_a_second_run_keeps_waiting_without_resuming_again(tmp_path):
    w = World(tmp_path)
    clock = Clock(w.cores)
    w.eng.stop()
    w.eng.run(timeout_s=0.04, clock=clock)
    resumes = len(w.cores.resumed)
    w.bps.insert(100, 0x10004, kind="hw", width=4)
    w.run(2, 100, 0x10004)
    w.cores.queue_event(2)
    stop = w.eng.run(timeout_s=1.0, clock=clock)
    assert (stop.kind, stop.tid) == ("hwbreak", 100)
    assert len(w.cores.resumed) == resumes                         # 走っていたので再開しなかった


def test_a_zero_limit_still_picks_up_a_hit_that_is_already_there(tmp_path):
    w = World(tmp_path)
    clock = Clock(w.cores)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(0, 100, 0x10004)
    w.cores.queue_event(0)
    w.eng.stop()
    assert w.eng.run(timeout_s=0, clock=clock).kind == "swbreak"


def test_halt_stops_the_running_cores_with_a_request_and_reads_the_world_again(tmp_path):
    w = World(tmp_path)
    clock = Clock(w.cores)
    w.eng.stop()
    w.eng.run(timeout_s=0, clock=clock)
    for c in range(4):
        w.run(c, 100 if c == 3 else 101, 0x10008, moe=MOE_HALT)
    stop = w.eng.halt()
    assert stop.kind == "int" and all(w.cores.halted()) and w.eng.running is False
    assert w.eng.stats().skew["request"]["stops"] == 2               # attach の代わりの stop と、この halt
    assert w.eng.live(100) == 3


def test_halt_when_already_halted_is_refused(tmp_path):
    w = World(tmp_path)
    w.eng.stop()
    with pytest.raises(Exception, match="already halted"):
        w.eng.halt()


def test_the_cont_of_gdb_is_unchanged_and_has_no_limit(tmp_path):
    w = World(tmp_path)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.run(0, 100, 0x10004, moe=MOE_BKPT_INSN)
    w.cores.queue_event(0)
    w.eng.stop()
    assert w.eng.cont().kind == "swbreak" and w.eng.running is False


def test_the_time_limit_holds_while_another_process_keeps_hitting_a_shared_breakpoint(tmp_path):
    """実機（2026-11-03、Haiku）: 対象外と判定した当たりが途切れない（毎秒数千万回通る行・スレッドの見分けの不一致）と、
    踏み越えて再開する道で上限を確かめず、run(wait_s) が永久に返らなかった。その道でも上限で timeout を返す。"""
    from .test_engine import BASE, map_page_for_foreign
    w = World(tmp_path)
    clock = Clock(w.cores)
    map_page_for_foreign(w)
    w.bps.insert(100, 0x10004, kind="sw", width=4)
    w.eng.stop()

    def foreign_hits_again(core=None):
        w.run(1, 100, 0x7004, other_sp=BASE + 0x14000 + 100)        # 別のプロセスが共有の BKPT にまた当たる
        w.cores.queue_event(1)
    foreign_hits_again()
    w.cores.on_step = foreign_hits_again
    stop = w.eng.run(timeout_s=0.1, clock=clock)
    assert stop.kind == "timeout" and w.eng.running is True
    assert clock() < 0.2                                           # 上限を過ぎた最初の確かめで返った
    assert w.eng.stats().counters["foreign_bp_hits"] >= 5           # 当たりは数えて捨てた（隠さない）
