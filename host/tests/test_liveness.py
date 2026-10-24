"""fprobe.liveness: 走らせたままの見張り。材料は止めずに読めるもの（DHCSR・DWT_PCSR・ティックの変数）だけで、返すのは
事実（ロックアップのビット・PC の見本が、使う側が明示して渡した関数の中にあるか・ティックが変わっていないか・いつから）。
経験則の閾値で判定しない。「止まって回る関数」「故障の処理」の名前の表を本体に持たない（名前は利用者が変えられる
慣習。CLAUDE.md。慣習の表はプラグイン fprobe.plugins.heuristics）。"""

import pytest

from fprobe import liveness
from fprobe.liveness import Liveness

FUNCS = {0x1000: "arch_system_halt", 0x2000: "HardFault_Handler", 0x3000: "main", 0x4000: "arch_cpu_idle",
         0x5000: "vAssertCalled", 0x6000: "uart_poll", 0x7000: "my_fault_loop"}


def function_at(pc):
    return FUNCS.get(pc & ~0xFFF)


def run(live, seconds, **kw):
    """t = 0, 0.5, … seconds 秒まで同じ材料で見る。最後の結果。"""
    out = None
    for i in range(int(seconds * 2) + 1):
        out = live.observe(now=i * 0.5, **kw)
    return out


def test_no_name_tables_in_the_core():
    assert not hasattr(liveness, "FATAL_FUNCTIONS") and not hasattr(liveness, "FAULT_HANDLERS")


def test_conventional_names_are_not_reported_unless_given():
    # 既定の名前の表を持たない: Error_Handler・vAssertCalled・HardFault_Handler 等の中にいても、渡されなければ言わない
    live = Liveness(function_at)
    assert run(live, 3, lockup=False, pcs=[0x1004, 0x1006], tick=None) is None
    assert run(live, 3, lockup=False, pcs=[0x2010] * 4, tick=None) is None
    assert run(live, 3, lockup=False, pcs=[0x5000], tick=None) is None


def test_normal_execution_is_not_reported():
    live = Liveness(function_at, functions=["arch_system_halt"])
    assert run(live, 5, lockup=False, pcs=[0x3010, 0x3044, 0x6020, 0x3010], tick=None) is None


def test_lockup_is_reported_at_once():
    live = Liveness(function_at)
    r = live.observe(lockup=True, pcs=[], tick=None, now=10.0)
    assert r == {"kind": "lockup", "since": 10.0, "evidence": [{"clue": "lockup"}]}


def test_being_in_a_given_function_is_reported_at_once_with_since_and_the_samples():
    # 「何秒いたら」の待ちをしない: PC の見本がすべて渡された関数の中なら、その事実をすぐに（since = 最初に見た時刻）
    live = Liveness(function_at, functions=["my_fault_loop", "arch_system_halt"])
    r = live.observe(lockup=False, pcs=[0x7004, 0x7006, None], tick=7, now=0.0)
    assert r == {"kind": "pc_in", "function": "my_fault_loop", "since": 0.0,
                 "evidence": [{"clue": "pc_in", "function": "my_fault_loop", "valid": 2, "pc_samples": 3}]}
    r = run(live, 2, lockup=False, pcs=[0x7004, 0x7006, 0x7004], tick=7)
    assert r["since"] == 0.0
    assert r["evidence"] == [{"clue": "pc_in", "function": "my_fault_loop", "valid": 3, "pc_samples": 3},
                             {"clue": "tick_stopped", "tick": 7}]


def test_given_patterns_match_function_names():
    live = Liveness(function_at, patterns=[r"^HardFault"])
    r = live.observe(lockup=False, pcs=[0x2010] * 4, tick=None, now=0.0)
    assert r["kind"] == "pc_in" and r["function"] == "HardFault_Handler"


def test_samples_spread_over_given_functions_name_them_all():
    live = Liveness(function_at, functions=["arch_system_halt", "vAssertCalled"])
    r = live.observe(lockup=False, pcs=[0x1000, 0x5000], tick=None, now=0.0)
    assert r["kind"] == "pc_in" and r["function"] == "arch_system_halt, vAssertCalled"


def test_a_sample_outside_the_given_functions_is_not_pc_in():
    live = Liveness(function_at, functions=["arch_system_halt"])
    assert live.observe(lockup=False, pcs=[0x1000, 0x3000], tick=None, now=0.0) is None


def test_a_bad_pattern_is_an_error():
    with pytest.raises(ValueError):
        Liveness(function_at, patterns=["("])


def test_a_tick_that_did_not_change_is_reported_with_the_pc_range_and_a_running_tick_is_not():
    # PC の範囲が何バイト以内なら、の閾値を使わない: ティックの変数が前の見張りから変わっていないという事実と、
    # そのときの PC の範囲・関数を返す
    live = Liveness(function_at)
    assert live.observe(lockup=False, pcs=[0x3100, 0x3104], tick=500, now=0.0) is None      # 1 回では変化が分からない
    r = live.observe(lockup=False, pcs=[0x3100, 0x6000], tick=500, now=1.0)
    assert r == {"kind": "tick_stopped", "function": "main, uart_poll", "since": 0.0,
                 "evidence": [{"clue": "pc_range", "function": "main, uart_poll", "bytes": 0x6000 - 0x3100 + 2,
                               "valid": 2, "pc_samples": 2},
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
    live = Liveness(function_at, functions=["vAssertCalled"])
    assert run(live, 3, lockup=False, pcs=[0xFFFFFFFF, None], tick=None) is None
    # 0 は規格の「読めない」ではない（読めない値は 0xFFFFFFFF）: 0 番地の見本として数える（?28）
    zero = Liveness(lambda pc: "vectors" if pc == 0 else None, functions=["vectors"])
    assert zero.observe(lockup=False, pcs=[0], tick=None, now=0.0)["kind"] == "pc_in"
    assert live.observe(lockup=False, pcs=[0x5000], tick=None, now=10.0)["kind"] == "pc_in"
    assert live.observe(lockup=False, pcs=[0x3010, 0x6000], tick=None, now=100.0) is None    # 動き出した（リセット等）


def test_reset_forgets_the_history():
    live = Liveness(function_at)
    live.observe(lockup=False, pcs=[0x3100], tick=5, now=0.0)
    live.reset()
    assert live.observe(lockup=False, pcs=[0x3100], tick=5, now=50.0) is None
