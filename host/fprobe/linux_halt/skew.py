"""コア間の停止の時間差の上限を、**常に上限になる**形で求めるための記録。

各コアについて「このコアが、この時刻より前には止まっていないと分かっている」時刻を持つ（`NotHaltedBefore`）:
  * 再開した（restart を求める）時刻より前には、止まっていない。
  * 走っているのを読んだ（DSCR の読み）なら、その読みを始めた時刻より前には、止まっていない（一度止まったコアは再開するまで止まったまま）。
全コアが止まったのを確認した時刻 t_end との差が、どのコアの停止も t_end 以前で、記録の時刻以降、という範囲に入る:
    時間差 ≤ t_end − min(各コアの記録)
これは検出が遅れた分（前の読みから検出までの時間）も含む。記録の無いコア（再開されずに止まったままのコア、止まった時刻の分からないコア）が
あれば、上限は分からない（None。0 ではない）。
"""
from __future__ import annotations

from typing import Iterable

from .core_access import StopReport


class NotHaltedBefore:
    def __init__(self):
        self._t: dict[int, int] = {}

    def clear(self) -> None:
        self._t.clear()

    def restarted(self, core: int, t: int) -> None:
        """core の再開を求める前の時刻 t（再開の前には、止まっていない）。"""
        self._t[core] = t

    def seen_running(self, core: int, t: int) -> None:
        """core が走っているのを、時刻 t より後の読みで見た（t は読みを始める前の時刻）。記録は前にしか進めない。"""
        if core in self._t:
            self._t[core] = max(self._t[core], t)
        else:
            self._t[core] = t

    def has(self, core: int) -> bool:
        return core in self._t

    def span_to(self, t_end: int, cores: Iterable[int]) -> int | None:
        """cores の全部が t_end までに止まったときの、時間差の上限（ns）。記録の無いコアがあれば None。"""
        cores = list(cores)
        if not cores or any(c not in self._t for c in cores):
            return None
        return max(0, t_end - min(self._t[c] for c in cores))


def stop_report(ledger: NotHaltedBefore, n_cores: int, method: str, halted: list[int], t_end: int, basis: str,
                running: list[int], explained: set[int], parked: Iterable[int]) -> StopReport:
    """時間差の上限が分かる条件を満たすか調べて、満たさなければ「分からない」(None) と理由を添えた報告を返す。
    running: 停止を求めた時点で走っていたコア。explained: 停止を求める前から止まっていて、デバッガの見た出来事で説明がつくコア。
    parked: 再開されずに止まったままだったコア。"""
    parked = sorted(parked)
    unexplained = [n for n in range(n_cores) if n not in running and n not in explained]
    if parked:
        return StopReport(method, tuple(halted), None,
                          "cores %s were not resumed (another core stopped first): they stopped at the earlier stop" % parked)
    if unexplained:
        return StopReport(method, tuple(halted), None,
                          "cores %s were already halted when the stop was requested, and no event the debugger saw "
                          "explains it: when they stopped is not known" % unexplained)
    if not running and not explained:
        return StopReport(method, tuple(halted), None, "no core needed halting: when each stopped is not known")
    bound = ledger.span_to(t_end, range(n_cores))
    if bound is None:
        return StopReport(method, tuple(halted), None, "a core has no record of when it was last known running: bound not known")
    return StopReport(method, tuple(halted), bound, basis)
