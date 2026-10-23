"""走らせたままの致命的エラーの見張り: 止めずに読める材料（DHCSR・DWT_PCSR・ティックの変数）から、事実を返す。

「実行中」のままでも、致命的エラーの後のコアは同じ所を回り続けるだけで、スレッド・CPU 負荷の画面は古い値を
出し続ける。見張りの周期ごとに材料を受け取り、事実だけを返す（経験則の閾値 = 何秒いたら・PC の範囲が何バイト
以内なら、で判定しない。CLAUDE.md）:

- ロックアップ（DHCSR.S_LOCKUP）: 故障の処理の中で故障した（kind = lockup）。
- PC の見本（DWT_PCSR。読むたびにコアが最近実行した命令の番地）がすべて、RTOS・ランタイムが定めた「止まって
  回り続ける」関数（Zephyr の arch_system_halt・k_fatal_halt、FreeRTOS の assert・フック、newlib の abort・_exit、
  Rust の panic 等）の中にある（kind = fatal_halt）、故障の処理（HardFault_Handler・Default_Handler 等）の中にある
  （kind = fault_loop）。見た時点で返し、since に最初に見た時刻。
- RTOS・ランタイムのティックの変数が、前の見張りから変わっていない（kind = tick_stopped。そのときの PC の範囲と
  関数を添える。アイドルの中かどうかは関数の名前で見せ、正常かどうかは決めない）。ティックが分からなければ返さない。

名前は RTOS・ランタイム・CMSIS の共通の名前で、特定の機器のものではない。結果は {"kind", "function"?, "since"
（見つけ始めた時刻）, "evidence"（手がかり: {"clue": "lockup"} / {"clue": "pc_in", "function"} / {"clue": "pc_range",
"function", "bytes"} / {"clue": "tick_stopped", "tick"}）}。値は状態が続く間は変わらない（見張りが変化だけを送るため、
経過の秒は入れない）。材料が正常に戻ったら None（リセット・再起動で直った）。
"""

import re
from collections.abc import Callable

__all__ = ["FATAL_FUNCTIONS", "FAULT_HANDLERS", "Liveness", "TICK_VARIABLES"]

# 止まって回り続ける関数（RTOS・ランタイム・ツールチェーン）
FATAL_FUNCTIONS = frozenset({
    "arch_system_halt", "k_fatal_halt", "z_fatal_halt",                                   # Zephyr
    "vAssertCalled", "vApplicationStackOverflowHook", "vApplicationMallocFailedHook",     # FreeRTOS の慣習のフック
    "abort", "_exit", "__assert_func", "__assert_fail", "__stack_chk_fail",              # C のランタイム
    "rust_begin_unwind", "panic", "__rust_start_panic",                                   # Rust
    "Error_Handler", "osRtxErrorNotify", "_tx_thread_system_error",                       # CMSIS の生成コード・RTX・ThreadX
})
# 故障の処理（CMSIS の名前・cortex-m-rt・既定の処理）
FAULT_HANDLERS = re.compile(r"^(HardFault|MemManage|BusFault|UsageFault|SecureFault)(_Handler)?$|^Default_?Handler_?$",
                            re.I)
# ティックの変数（RTOS・ランタイム。下位の語を読む）
TICK_VARIABLES = ("xTickCount", "curr_tick", "_tx_timer_system_clock", "OSTickCtr", "uwTick")
_INVALID = (0, 0xFFFFFFFF)           # PCSR が読めない（止まっている・眠っている・DWT が無い）


class Liveness:
    """見張りの履歴。function_at(pc) は番地の関数の名前（ELF。分からなければ None）。"""

    def __init__(self, function_at: Callable[[int], str | None]):
        self.function_at = function_at
        self.reset()

    def reset(self) -> None:
        """履歴を捨てる（止めた・再開した・リセットした・接続し直した）。"""
        self._condition: tuple | None = None        # 続いている状態（種類, 関数）
        self._since = 0.0                           # その状態を初めて見た時刻
        self._tick: int | None = None               # 前の見張りのティック
        self._tick_since = 0.0                      # ティックがその値になったのを初めて見た時刻

    def observe(self, lockup: bool, pcs: list[int | None], tick: int | None, now: float) -> dict | None:
        """見張りの 1 回: ロックアップか、PC の見本（いくつか）、ティック（無ければ None）。事実があれば結果、無ければ
        None。"""
        tick_stopped = tick is not None and tick == self._tick
        if tick is None or tick != self._tick:
            self._tick, self._tick_since = tick, now
        if lockup:
            return self._report(("lockup", None), now, [{"clue": "lockup"}])
        valid = [pc for pc in pcs if pc is not None and pc not in _INVALID]
        if not valid:
            self._condition = None                                         # 見本が無い（眠っている等）: 返さない
            return None
        names = {self.function_at(pc) for pc in valid}
        name = next(iter(names)) if len(names) == 1 else None
        if name is not None and (name in FATAL_FUNCTIONS or FAULT_HANDLERS.match(name)):
            kind = "fault_loop" if FAULT_HANDLERS.match(name) else "fatal_halt"
            evidence = [{"clue": "pc_in", "function": name}]
            if tick_stopped:
                evidence.append({"clue": "tick_stopped", "tick": tick})
            return self._report((kind, name), now, evidence)
        if tick_stopped:
            lo, hi = min(valid), max(valid)
            where = ", ".join(sorted(n for n in names if n)) or f"0x{lo:08X}"
            out = self._report(("tick_stopped", None), self._tick_since,
                               [{"clue": "pc_range", "function": where, "bytes": hi - lo + 2},
                                {"clue": "tick_stopped", "tick": tick}])
            out["function"] = where
            return out
        self._condition = None
        return None

    def _report(self, condition: tuple, now: float, evidence: list[dict]) -> dict:
        if self._condition != condition:
            self._condition, self._since = condition, now
        out = {"kind": condition[0], "since": self._since, "evidence": evidence}
        if condition[1]:
            out["function"] = condition[1]
        return out
