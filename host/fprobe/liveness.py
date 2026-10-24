"""走らせたままの見張り: 止めずに読める材料（DHCSR・DWT_PCSR・ティックの変数）から、事実を返す。

「実行中」のままでも、致命的エラーの後のコアは同じ所を回り続けるだけで、スレッド・CPU 負荷の画面は古い値を
出し続ける。見張りの周期ごとに材料を受け取り、事実だけを返す（経験則の閾値 = 何秒いたら・PC の範囲が何バイト
以内なら、で判定しない。名前の慣習 = 「止まって回る関数」「故障の処理」の名前の表も本体に持たない。CLAUDE.md）:

- ロックアップ（DHCSR.S_LOCKUP）: 故障の処理の中で故障した（kind = lockup。アーキテクチャの定義）。
- PC の見本（DWT_PCSR。読むたびにコアが最近実行した命令の番地）の読めたものがすべて、使う側が明示して渡した関数
  （名前の完全一致・正規表現）の中にある（kind = pc_in）。見た時点で返し、since に最初に見た時刻。渡されなければ
  返さない。慣習の名前の表（Error_Handler・HardFault_Handler 等）はプラグイン fprobe.plugins.heuristics の
  liveness_conventions。
- ティックの変数（使う側が名前を渡したもの。名前の慣習で探さない）が、前の見張りから変わっていない（kind =
  tick_stopped。そのときの PC の範囲と関数を添える。正常かどうかは決めない）。ティックが分からなければ返さない。

結果は {"kind", "function"?, "since"（見つけ始めた時刻）, "evidence"（手がかり: {"clue": "lockup"} / {"clue": "pc_in",
"function", "valid", "pc_samples"} / {"clue": "pc_range", "function", "bytes", "valid", "pc_samples"} / {"clue":
"tick_stopped", "tick"}）}。valid = 読めた PC の見本の数、pc_samples = 読んだ数（使った値）。値は状態が続く間は
変わらない（見張りが変化だけを送るため、経過の秒は入れない）。材料が正常に戻ったら None（リセット・再起動で直った）。
"""

from collections.abc import Callable, Iterable

from .halt_risk import compile_patterns

__all__ = ["Liveness"]

_INVALID = (0xFFFFFFFF,)             # PCSR が読めない（止まっている・DWT_PCSR が無いときの規格の値。ARMv7-M ARM の DWT_PCSR）


class Liveness:
    """見張りの履歴。function_at(pc) は番地の関数の名前（ELF。分からなければ None）。functions・patterns は、PC の
    見本がその中にあれば知らせる関数（使う側が明示する。既定は無し）。"""

    def __init__(self, function_at: Callable[[int], str | None], functions: Iterable[str] = (),
                 patterns: Iterable[str] = ()):
        self.function_at = function_at
        self.functions = frozenset(functions)
        self.patterns = compile_patterns(patterns)
        self.reset()

    def reset(self) -> None:
        """履歴を捨てる（止めた・再開した・リセットした・接続し直した）。"""
        self._condition: tuple | None = None        # 続いている状態（種類, 関数）
        self._since = 0.0                           # その状態を初めて見た時刻
        self._tick: int | None = None               # 前の見張りのティック
        self._tick_since = 0.0                      # ティックがその値になったのを初めて見た時刻

    def _watched(self, name: str | None) -> bool:
        return name is not None and (name in self.functions or any(p.search(name) for p in self.patterns))

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
        counts = {"valid": len(valid), "pc_samples": len(pcs)}
        if all(self._watched(n) for n in names):
            where = ", ".join(sorted(names))
            evidence = [{"clue": "pc_in", "function": where, **counts}]
            if tick_stopped:
                evidence.append({"clue": "tick_stopped", "tick": tick})
            return self._report(("pc_in", where), now, evidence)
        if tick_stopped:
            lo, hi = min(valid), max(valid)
            where = ", ".join(sorted(n for n in names if n)) or f"0x{lo:08X}"
            out = self._report(("tick_stopped", None), self._tick_since,
                               [{"clue": "pc_range", "function": where, "bytes": hi - lo + 2, **counts},
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
