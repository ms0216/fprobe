"""fprobe.liveness: 走らせたままの致命的エラーの見張り（ロックアップ・致命的エラーの関数・故障の処理の無限ループ・
ティックの止まった細かいループ）。材料は止めずに読めるもの（DHCSR・DWT_PCSR・ティックの変数）だけ。"""

from fprobe.liveness import Liveness

FUNCS = {0x1000: "arch_system_halt", 0x2000: "HardFault_Handler", 0x3000: "main", 0x4000: "arch_cpu_idle",
         0x5000: "vAssertCalled", 0x6000: "uart_poll"}


def function_at(pc):
    return FUNCS.get(pc & ~0xFFF)


def run(live, seconds, **kw):
    """t = 0, 0.5, … seconds 秒まで同じ材料で見る。最後の結果。"""
    out = None
    for i in range(int(seconds * 2) + 1):
        out = live.observe(now=i * 0.5, **kw)
    return out


def test_normal_execution_is_not_fatal():
    live = Liveness(function_at)
    assert run(live, 5, lockup=False, pcs=[0x3010, 0x3044, 0x6020, 0x3010], tick=None) is None


def test_lockup_is_fatal_at_once():
    live = Liveness(function_at)
    r = live.observe(lockup=True, pcs=[], tick=None, now=10.0)
    assert r == {"kind": "lockup", "since": 10.0, "evidence": [{"clue": "lockup"}]}


def test_staying_in_a_fatal_halt_function_is_fatal_after_it_persists():
    live = Liveness(function_at)
    assert live.observe(lockup=False, pcs=[0x1004, 0x1006], tick=7, now=0.0) is None     # 一瞬では決めない
    r = run(live, 2, lockup=False, pcs=[0x1004, 0x1006, 0x1004], tick=7)
    assert r["kind"] == "fatal_halt" and r["function"] == "arch_system_halt"
    assert r["since"] == 0.0
    assert r["evidence"] == [{"clue": "pc_in", "function": "arch_system_halt"}, {"clue": "tick_stopped", "tick": 7}]


def test_fault_handler_loop_is_a_fault():
    live = Liveness(function_at)
    r = run(live, 2, lockup=False, pcs=[0x2010] * 4, tick=None)
    assert r["kind"] == "fault_loop" and r["function"] == "HardFault_Handler"


def test_a_tight_loop_with_a_stopped_tick_is_stuck_but_not_with_a_running_tick():
    live = Liveness(function_at)
    r = run(live, 5, lockup=False, pcs=[0x3100, 0x3104, 0x3102], tick=500)
    assert r["kind"] == "stuck" and r["function"] == "main"
    assert r["evidence"] == [{"clue": "pc_loop", "function": "main", "bytes": 6}, {"clue": "tick_stopped", "tick": 500}]
    assert run(live, 5, lockup=False, pcs=[0x3100, 0x3104, 0x3102], tick=500) == r          # 続く間は同じ（通知しない）
    live = Liveness(function_at)
    out = None
    for i in range(11):
        out = live.observe(lockup=False, pcs=[0x3100, 0x3104], tick=500 + i, now=i * 0.5)
    assert out is None


def test_a_tight_loop_without_a_tick_is_not_judged():
    live = Liveness(function_at)
    assert run(live, 10, lockup=False, pcs=[0x3100, 0x3104], tick=None) is None


def test_idle_and_sleep_are_not_stuck():
    live = Liveness(function_at)
    assert run(live, 5, lockup=False, pcs=[0x4002, 0x4004], tick=1) is None


def test_unreadable_pcs_are_no_evidence_and_recovery_clears_the_fatal():
    live = Liveness(function_at)
    assert run(live, 3, lockup=False, pcs=[0xFFFFFFFF, 0], tick=None) is None
    assert run(live, 2, lockup=False, pcs=[0x5000], tick=None)["kind"] == "fatal_halt"
    assert live.observe(lockup=False, pcs=[0x3010, 0x6000], tick=None, now=100.0) is None    # 動き出した（リセット等）


def test_reset_forgets_the_history():
    live = Liveness(function_at)
    run(live, 2, lockup=False, pcs=[0x1004], tick=None)
    live.reset()
    assert live.observe(lockup=False, pcs=[0x1004], tick=None, now=50.0) is None
