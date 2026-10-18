"""走らせたままの致命的エラーの見張り: コアがエラーで止まり続けている（無限ループ・ロックアップ）のを、止めずに見つける。

「実行中」のままでも、致命的エラーの後のコアは同じ所を回り続けるだけで、スレッド・CPU 負荷の画面は古い値を
出し続ける。止めずに読める材料を見張りの周期ごとに受け取り、組み合わせて決める:

- ロックアップ（DHCSR.S_LOCKUP）: 故障の処理の中で故障した。それだけで致命的（kind = lockup）。
- PC の見本（DWT_PCSR。読むたびにコアが最近実行した命令の番地）が、RTOS・ランタイムの「止まって回り続ける」関数
  （Zephyr の arch_system_halt・k_fatal_halt、FreeRTOS の assert・フック、newlib の abort・_exit、Rust の panic 等）
  の中に CONFIRM_S 秒いる（kind = fatal_halt）、故障の処理（HardFault_Handler・Default_Handler 等）の中にいる
  （kind = fault_loop）。
- PC が狭い範囲（SPIN_BYTES）を STUCK_S 秒回り続け、その間 RTOS・ランタイムのティックの変数が進まない
  （割り込みを止めて回る。FreeRTOS の configASSERT の既定等。kind = stuck）。ティックが分からなければ決めない
  （ベアメタルの待ちのループと区別できない）。アイドルの関数の中は回っていても正常。

名前は RTOS・ランタイム・CMSIS の共通の名前で、特定の機器のものではない。結果は {"kind", "function"?, "since"
（見つけ始めた時刻）, "evidence"（手がかり: {"clue": "lockup"} / {"clue": "pc_in", "function"} / {"clue": "pc_loop",
"function", "bytes"} / {"clue": "tick_stopped", "tick"}）}。値は状態が続く間は変わらない（見張りが変化だけを送るため、
経過の秒は入れない）。材料が正常に戻ったら None（リセット・再起動で直った）。
"""

import re
from collections.abc import Callable

__all__ = ["CONFIRM_S", "FATAL_FUNCTIONS", "FAULT_HANDLERS", "IDLE_FUNCTIONS", "Liveness", "STUCK_S", "TICK_VARIABLES"]

CONFIRM_S = 1.5                      # 致命的エラーの関数・故障の処理にこれだけ居続けたら決める
STUCK_S = 3.0                        # 細かいループ + ティックの停止がこれだけ続いたら決める
SPIN_BYTES = 64                      # 細かいループとみなす PC の範囲

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
# アイドル（回っていても正常）
IDLE_FUNCTIONS = frozenset({"arch_cpu_idle", "arch_cpu_atomic_idle", "k_cpu_idle", "k_cpu_atomic_idle", "idle",
                            "prvIdleTask", "vApplicationIdleHook", "_tx_thread_schedule", "osRtxIdleThread",
                            "cpu_idle", "__WFI", "__WFE"})
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
        self._first = 0.0                           # その状態を初めて見た時刻
        self._lo = self._hi = 0                     # 細かいループの PC の範囲
        self._tick: int | None = None               # その状態を初めて見たときのティック
        self._since: float | None = None            # 致命的と決めた状態の始まり

    def observe(self, lockup: bool, pcs: list[int | None], tick: int | None, now: float) -> dict | None:
        """見張りの 1 回: ロックアップか、PC の見本（いくつか）、ティック（無ければ None）。致命的なら結果、でなければ
        None。"""
        if lockup:
            return self._decide(("lockup", None), now, now, [{"clue": "lockup"}])
        valid = [pc for pc in pcs if pc is not None and pc not in _INVALID]
        if not valid:
            self.reset()                                                   # 見本が無い（眠っている等）: 決めない
            return None
        names = {self.function_at(pc) for pc in valid}
        name = next(iter(names)) if len(names) == 1 else None
        lo, hi = min(valid), max(valid)
        if name is not None and (name in FATAL_FUNCTIONS or FAULT_HANDLERS.match(name)):
            kind = "fault_loop" if FAULT_HANDLERS.match(name) else "fatal_halt"
            first = self._continue((kind, name), now, lo, hi, tick)
            if now - first < CONFIRM_S:
                return None
            evidence = [{"clue": "pc_in", "function": name}]
            if tick is not None and tick == self._tick:
                evidence.append({"clue": "tick_stopped", "tick": tick})
            return self._decide((kind, name), first, now, evidence)
        if name in IDLE_FUNCTIONS:
            self.reset()
            return None
        if self._condition is not None and self._condition[0] == "stuck":
            lo, hi = min(lo, self._lo), max(hi, self._hi)
        if hi - lo > SPIN_BYTES or tick is None:
            self.reset()
            return None
        first = self._continue(("stuck", None), now, lo, hi, tick)
        if tick != self._tick:                                             # ティックが進んだ: 待っているだけ
            self.reset()
            return None
        if now - first < STUCK_S:
            return None
        where = ", ".join(sorted(n for n in names if n)) or f"0x{lo:08X}"
        return self._decide(("stuck", None), first, now,
                            [{"clue": "pc_loop", "function": where, "bytes": hi - lo + 2},
                             {"clue": "tick_stopped", "tick": tick}], function=where)

    def _continue(self, condition: tuple, now: float, lo: int, hi: int, tick: int | None) -> float:
        """同じ状態が続いていれば初めて見た時刻、新しい状態なら now から数え直す。"""
        if self._condition != condition:
            self._condition, self._first, self._tick, self._since = condition, now, tick, None
        self._lo, self._hi = lo, hi
        return self._first

    def _decide(self, condition: tuple, first: float, now: float, evidence: list[dict],
                function: str | None = None) -> dict:
        if self._condition != condition:
            self._condition, self._first, self._tick, self._since = condition, first, None, None
        if self._since is None:
            self._since = first
        out = {"kind": condition[0], "since": self._since, "evidence": evidence}
        if function or condition[1]:
            out["function"] = function or condition[1]
        return out
