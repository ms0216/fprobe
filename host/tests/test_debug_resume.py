"""fprobe.debug.Debugger.resume: ブレークポイントの上から再開するときの 1 命令の踏み越え（偽のターゲット）。"""

from pyocd.core.target import Target

from fprobe.debug import Debugger


class FakeTarget:
    """Debugger.resume が使うところだけの偽物。step で止まった理由を halt_after_step にする。"""

    def __init__(self, pc: int, halt_after_step=Target.HaltReason.DEBUG):
        self.pc, self.halt_after_step = pc, halt_after_step
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.BREAKPOINT
        self.calls: list[str] = []

    def get_state(self):
        return self.state

    def get_halt_reason(self):
        return self.reason

    def read_core_register(self, name):
        return self.pc

    def read32(self, address):                      # DFSR: ステップ後の理由に対応するビット（HALTED = 1 / DWTTRAP = 4）
        return {Target.HaltReason.DEBUG: 1, Target.HaltReason.WATCHPOINT: 4}[self.reason]

    def remove_breakpoint(self, address):
        self.calls.append("remove")

    def set_breakpoint(self, address, kind=None):
        self.calls.append("set")
        return True

    def step(self):
        self.calls.append("step")
        self.pc += 2
        self.reason = self.halt_after_step

    def resume(self):
        self.calls.append("resume")
        self.state = Target.State.RUNNING


def test_resume_steps_over_the_breakpoint_then_runs():
    t = FakeTarget(0x100)
    Debugger(t, breakpoints={0x100: "f"}).resume()
    assert t.calls == ["remove", "step", "set", "resume"]


def test_a_watchpoint_hit_by_the_stepped_instruction_is_not_lost():
    # 踏み越えた 1 命令でウォッチポイントが成立したら、そこで止まったままにする（再開するとその停止を失う）
    t = FakeTarget(0x100, halt_after_step=Target.HaltReason.WATCHPOINT)
    Debugger(t, breakpoints={0x100: "f"}).resume()
    assert t.calls == ["remove", "step", "set"] and t.state == Target.State.HALTED


def test_resume_without_a_breakpoint_here_just_runs():
    t = FakeTarget(0x100)
    Debugger(t, breakpoints={0x200: "g"}).resume()
    assert t.calls == ["resume"]


class CountingLock:
    """run_until_halt が機器に触る間だけ持ち、待つ間は放すことを確かめるロック。"""

    def __init__(self):
        self.held, self.entries = False, 0

    def __enter__(self):
        self.held, self.entries = True, self.entries + 1

    def __exit__(self, *exc):
        self.held = False


def test_run_until_halt_releases_the_lock_while_waiting(monkeypatch):
    t = FakeTarget(0x100)
    lock = CountingLock()
    sleeps = []

    def sleep(s):
        assert not lock.held                                     # 待つ間はロックを放している（背景の RTT が読める）
        sleeps.append(s)
        if len(sleeps) == 2:
            t.state, t.reason = Target.State.HALTED, Target.HaltReason.BREAKPOINT
    monkeypatch.setattr("fprobe.debug.time.sleep", sleep)
    stop = Debugger(t).run_until_halt(timeout_s=5, lock=lock)
    assert stop.reason == "breakpoint" and lock.entries >= 3


class BpManager:
    def __init__(self):
        self.flushed = 0

    def flush(self):
        self.flushed += 1


def test_breakpoints_set_while_running_are_written_at_once():
    """pyOCD はブレークポイントを溜めて再開の直前に書く。走っている間は再開が来ないので、置いた・外したらすぐ書く
    （実機で、走行中に置いたブレークポイントが効かなかった。2026-10-18）。"""
    t = FakeTarget(0x100)
    t.bp_manager = BpManager()
    t.state = Target.State.RUNNING
    dbg = Debugger(t)
    dbg.breakpoint(0x200)
    assert t.bp_manager.flushed == 1
    dbg.remove_breakpoint(0x200)
    assert t.bp_manager.flushed == 2
    t.state = Target.State.HALTED                      # 止まっていれば再開のときに書かれる（今すぐは書かない）
    dbg.breakpoint(0x300)
    assert t.bp_manager.flushed == 2


def test_the_soc_target_delegates_to_its_core():
    """pyOCD の session.target（SoC）は bp_manager を持たず、selected_core が持つ。"""
    core = FakeTarget(0x100)
    core.bp_manager = BpManager()
    core.state = Target.State.RUNNING
    soc = FakeTarget(0x100)
    soc.selected_core = core
    soc.state = Target.State.RUNNING
    Debugger(soc).breakpoint(0x200)
    assert core.bp_manager.flushed == 1


def test_reset_kinds_are_checked_on_the_core():
    """pyOCD の SoC のターゲットの supported_reset_types は NotImplementedError（コアが知っている。実機で、どの種類の
    リセットも失敗した。2026-10-18）。"""
    from pyocd.core.target import Target as T
    core = FakeTarget(0x100)
    core.supported_reset_types = [T.ResetType.SYSRESETREQ, T.ResetType.VECTRESET]
    calls = []
    core.reset_and_halt = lambda kind=None: calls.append(kind)

    class Soc(FakeTarget):
        @property
        def supported_reset_types(self):
            raise NotImplementedError()
    soc = Soc(0x100)
    soc.selected_core = core
    soc.reset_and_halt = core.reset_and_halt
    Debugger(soc).reset("core", halt=True)
    assert calls == [T.ResetType.VECTRESET]
