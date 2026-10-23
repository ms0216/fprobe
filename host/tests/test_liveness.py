"""fprobe.liveness: 走らせたままの致命的エラーの見張り。材料は止めずに読めるもの（DHCSR・DWT_PCSR・ティックの変数）
だけで、返すのは事実（ロックアップのビット・PC がどの関数の中にいるか・ティックが変わっていないか・いつから）。
経験則の閾値（何秒いたら・PC の範囲が何バイト以内なら）で判定しない（CLAUDE.md）。"""

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


def test_normal_execution_is_not_reported():
    live = Liveness(function_at)
    assert run(live, 5, lockup=False, pcs=[0x3010, 0x3044, 0x6020, 0x3010], tick=None) is None


def test_lockup_is_reported_at_once():
    live = Liveness(function_at)
    r = live.observe(lockup=True, pcs=[], tick=None, now=10.0)
    assert r == {"kind": "lockup", "since": 10.0, "evidence": [{"clue": "lockup"}]}


def test_being_in_a_fatal_halt_function_is_reported_at_once_with_since():
    # 「何秒いたら」の待ちをしない: PC の見本がすべて致命的エラーの関数の中なら、その事実をすぐに（since = 最初に見た時刻）
    live = Liveness(function_at)
    r = live.observe(lockup=False, pcs=[0x1004, 0x1006], tick=7, now=0.0)
    assert r == {"kind": "fatal_halt", "function": "arch_system_halt", "since": 0.0,
                 "evidence": [{"clue": "pc_in", "function": "arch_system_halt"}]}
    r = run(live, 2, lockup=False, pcs=[0x1004, 0x1006, 0x1004], tick=7)
    assert r["since"] == 0.0
    assert r["evidence"] == [{"clue": "pc_in", "function": "arch_system_halt"}, {"clue": "tick_stopped", "tick": 7}]


def test_being_in_a_fault_handler_is_reported():
    live = Liveness(function_at)
    r = live.observe(lockup=False, pcs=[0x2010] * 4, tick=None, now=0.0)
    assert r["kind"] == "fault_loop" and r["function"] == "HardFault_Handler"


def test_a_tick_that_did_not_change_is_reported_with_the_pc_range_and_a_running_tick_is_not():
    # PC の範囲が何バイト以内なら、の閾値を使わない: ティックの変数が前の見張りから変わっていないという事実と、
    # そのときの PC の範囲・関数を返す
    live = Liveness(function_at)
    assert live.observe(lockup=False, pcs=[0x3100, 0x3104], tick=500, now=0.0) is None      # 1 回では変化が分からない
    r = live.observe(lockup=False, pcs=[0x3100, 0x6000], tick=500, now=1.0)
    assert r == {"kind": "tick_stopped", "function": "main, uart_poll", "since": 0.0,
                 "evidence": [{"clue": "pc_range", "function": "main, uart_poll", "bytes": 0x6000 - 0x3100 + 2},
                              {"clue": "tick_stopped", "tick": 500}]}
    live = Liveness(function_at)
    out = None
    for i in range(11):
        out = live.observe(lockup=False, pcs=[0x3100, 0x3104], tick=500 + i, now=i * 0.5)
    assert out is None


def test_without_a_tick_nothing_is_reported_from_the_pc_alone():
    live = Liveness(function_at)
    assert run(live, 10, lockup=False, pcs=[0x3100, 0x3104], tick=None) is None


def test_a_stopped_tick_in_idle_is_still_reported_as_a_fact():
    # 「アイドルなら正常」と決めない（ティックの止まるアイドルもある）。関数の名前で事実として見せる
    live = Liveness(function_at)
    r = run(live, 2, lockup=False, pcs=[0x4002, 0x4004], tick=1)
    assert r["kind"] == "tick_stopped" and r["function"] == "arch_cpu_idle"


def test_unreadable_pcs_are_no_evidence_and_recovery_clears_the_report():
    live = Liveness(function_at)
    assert run(live, 3, lockup=False, pcs=[0xFFFFFFFF, 0], tick=None) is None
    assert live.observe(lockup=False, pcs=[0x5000], tick=None, now=10.0)["kind"] == "fatal_halt"
    assert live.observe(lockup=False, pcs=[0x3010, 0x6000], tick=None, now=100.0) is None    # 動き出した（リセット等）


def test_reset_forgets_the_history():
    live = Liveness(function_at)
    live.observe(lockup=False, pcs=[0x3100], tick=5, now=0.0)
    live.reset()
    assert live.observe(lockup=False, pcs=[0x3100], tick=5, now=50.0) is None
