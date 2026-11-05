"""malloc / free の記録からの集計（U27）: 生きている確保・最大・二重の解放・失敗・呼び出し元ごとの生きている確保
（リークかは判定しない）。出来事の取り方は probe_heap（ブレークポイント）。ヒープの管理の形を辿る走査は heap.py。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ISSUES_LIMIT", "POINTERS", "SERIES_POINTS", "AllocationTracker"]

POINTERS = 8                             # 呼び出し元ごとに返す生きている番地の数（表示の量。切ったら pointers_truncated）
ISSUES_LIMIT = 200                       # 記録の誤りを持つ数の既定（AllocationTracker の issues_limit。総数は数える）
SERIES_POINTS = 512                      # 使用量の推移を返す点の数の既定（report の series_points）


@dataclass
class _Live:
    t: float
    size: int
    caller: str


class AllocationTracker:
    """alloc / free の出来事（時刻は秒）から、生きている確保・最大・誤り・呼び出し元ごとの生きている確保を出す。
    誤りは issues_limit 件まで持つ（総数は issues_total に数える）。"""

    def __init__(self, issues_limit: int = ISSUES_LIMIT):
        if issues_limit < 0:
            raise ValueError("issues_limit は 0 以上")
        self.issues_limit = issues_limit
        self.issues_total = 0
        self.live: dict[int, _Live] = {}
        self.freed: set[int] = set()
        self.allocations = self.frees = self.untracked_frees = 0
        self.bytes = self.peak = 0
        self.issues: list[dict] = []
        self.series: list[tuple[float, int]] = []
        self.history: dict[str, list[tuple[float, int]]] = {}      # 呼び出し元 → (時刻, 生きている数)
        self._per_caller: dict[str, int] = {}
        self.first: float | None = None
        self.last: float | None = None

    def _issue(self, kind: str, t: float, pointer: int, caller: str, detail: str) -> None:
        self.issues_total += 1
        if len(self.issues) < self.issues_limit:
            self.issues.append({"kind": kind, "t": t, "pointer": pointer, "caller": caller, "detail": detail})

    def _count(self, caller: str, t: float, delta: int) -> None:
        n = self._per_caller.get(caller, 0) + delta
        self._per_caller[caller] = n
        self.history.setdefault(caller, []).append((t, n))

    def _seen(self, t: float) -> None:
        self.first = t if self.first is None else min(self.first, t)
        self.last = t if self.last is None else max(self.last, t)

    def alloc(self, t: float, pointer: int, size: int, caller: str = "?") -> None:
        self._seen(t)
        self.allocations += 1
        if not pointer:
            self._issue("failed_allocation", t, 0, caller, f"{size} バイトの確保に失敗（NULL）")
            return
        old = self.live.pop(pointer, None)
        if old is not None:
            self._issue("reused_live_pointer", t, pointer, caller,
                        f"解放されていない番地がまた返った（{old.caller} の確保。その間の free は記録に無い）")
            self.bytes -= old.size
            self._count(old.caller, t, -1)
        self.freed.discard(pointer)
        self.live[pointer] = _Live(t, size, caller)
        self.bytes += size
        self.peak = max(self.peak, self.bytes)
        self._count(caller, t, +1)
        self.series.append((t, self.bytes))

    def free(self, t: float, pointer: int, caller: str = "?") -> None:
        self._seen(t)
        if not pointer:
            return                                                  # free(NULL) は何もしない
        self.frees += 1
        entry = self.live.pop(pointer, None)
        if entry is None:
            if pointer in self.freed:
                self._issue("double_free", t, pointer, caller, "二重の解放")
            else:
                self.untracked_frees += 1                           # 記録に無いポインタの解放（いつの確保かは決めない）
            return
        self.freed.add(pointer)
        self.bytes -= entry.size
        self._count(entry.caller, t, -1)
        self.series.append((t, self.bytes))

    def report(self, now: float, min_age_s: float = 0.0, top: int = 20, series_points: int = SERIES_POINTS,
               live_points: int = 5) -> dict:
        """要約・呼び出し元ごとの生きている確保（live_by_caller: min_age_s より古いもの。バイトの多い順に top 個。
        callers_total = 切る前の呼び出し元の数、truncated = 切ったか、top = 使った値。pointers は先頭の POINTERS 個で、
        pointers_truncated = count より少ないか。live_counts = 最初と最後の出来事の間を 4 つに分けた 5 つの時点での、
        その呼び出し元の生きている数。リークかは判定しない）・誤り（issues。issues_total = 総数、issues_truncated =
        持つ上限で切ったか、issues_limit = 上限）・使用量の推移（series。series_points 点まで等間隔に間引く:
        series_total = 元の点の数、series_downsampled = 間引いたか、series_points = 使った点の数）。"""
        if series_points < 2 or live_points < 2:
            raise ValueError("series_points・live_points は 2 以上（最初と最後の点）")
        groups: dict[str, dict] = {}
        for pointer, e in self.live.items():
            if now - e.t < min_age_s:
                continue
            g = groups.setdefault(e.caller, {"caller": e.caller, "count": 0, "bytes": 0, "oldest_t": e.t,
                                             "pointers": []})
            g["count"] += 1
            g["bytes"] += e.size
            g["oldest_t"] = min(g["oldest_t"], e.t)
            if len(g["pointers"]) < POINTERS:
                g["pointers"].append(pointer)
        for g in groups.values():
            g["live_counts"] = self._live_counts(g["caller"], live_points)
            g["pointers_truncated"] = len(g["pointers"]) < g["count"]
        callers = sorted(groups.values(), key=lambda g: (-g["bytes"], g["caller"]))[:top]
        return {"callers_total": len(groups), "truncated": len(callers) < len(groups), "top": top,
                "allocations": self.allocations, "frees": self.frees, "untracked_frees": self.untracked_frees,
                "live": {"count": len(self.live), "bytes": self.bytes}, "peak_bytes": self.peak,
                "issues": list(self.issues), "issues_total": self.issues_total,
                "issues_truncated": self.issues_total > len(self.issues), "issues_limit": self.issues_limit,
                "live_by_caller": callers, "series": self._downsampled(series_points), "series_total": len(self.series),
                "series_downsampled": len(self.series) > series_points, "series_points": series_points,
                "live_points": live_points}                 # live_counts の点の数（最初から最後まで等間隔の時刻）

    def _live_counts(self, caller: str, points: int) -> list[tuple[float, int]]:
        """呼び出し元の生きている数を、最初から最後まで points 個の等間隔の時刻で（その時刻の値。間の山は落ちうる）。"""
        history = self.history.get(caller, [])
        if self.first is None:
            return []
        marks = [self.first + (self.last - self.first) * k / (points - 1) for k in range(points)]
        out, i, n = [], 0, 0
        for m in marks:
            while i < len(history) and history[i][0] <= m:
                n = history[i][1]
                i += 1
            out.append((m, n))
        return out

    def _downsampled(self, points: int) -> list[tuple[float, int]]:
        s = self.series
        if len(s) <= points:
            return list(s)
        step = len(s) / (points - 1)
        return [s[int(i * step)] for i in range(points - 1)] + [s[-1]]
