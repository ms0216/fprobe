"""プロファイルの集計（U18）: 時間の区間に限る集計、関数のグループ（名前の型・ファイル・ディレクトリ・名前の指定）、
CPU 使用率とグループの推移、テストポイントの呼び出しの区間の集計。

    w = window(samples, t0, t1)                                   # [t0, t1) のサンプルだけ（時刻はサンプルの単位）
    prof = Profiler(symbols).profile(w)
    group_profile(prof, Grouping("groups", {"Bluetooth": ["bt_*"], "HAL": ["file:Drivers/*"]}))
    timeline(samples, profiler, bucket=1000, grouping=Grouping("prefix", separator="_"))   # 区切りごとの負荷と割合
    call_stats(calls, t0, t1)                                     # テストポイントの呼び出しを区間で切った時間

グループの決め方（by）: function（関数そのもの）/ file（ソースのファイル）/ directory（その上のディレクトリ）/
prefix（関数名の、使う側が明示した区切り separator の最初の出現より前。separator "_" なら "HAL_GPIO_Init" → "HAL"、
"::" なら "ns::Cls::f" → "ns"。区切りの無い名前はそのまま。名前の型の規則は本体に持たない）/ groups（名前 → パターンの
一覧。パターンは関数名の glob、"file:" で始めるとファイルの glob。上から順に最初に合ったもの、どれにも合わなければ
"(other)"）。ファイルで分けるときは行ごと（インライン展開された関数は展開先の行のファイル）。

仕様: docs/superpowers/specs/2026-10-18-u18-u26-profile-export-stack-design.md
"""

import fnmatch
import posixpath
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .pcprofile import Profile, Profiler
from .samples import PcSamples

__all__ = ["BY", "OTHER", "Grouping", "call_stats", "group_profile", "group_rows", "timeline", "window"]

BY = ("function", "file", "directory", "prefix", "groups")
OTHER = "(other)"
UNKNOWN_FILE = "(unknown)"


@dataclass(frozen=True)
class Grouping:
    by: str = "function"
    groups: dict[str, list[str]] | None = None
    separator: str | None = None            # by prefix の区切り（使う側が明示する）
    _patterns: tuple = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.by not in BY:
            raise ValueError(f"by は {', '.join(BY)} のどれか: {self.by!r}")
        if self.by == "groups":
            if not self.groups:
                raise ValueError("by groups には groups（{名前: [パターン]}）が要る")
            compiled = tuple((name, tuple(p for p in patterns)) for name, patterns in self.groups.items())
            object.__setattr__(self, "_patterns", compiled)
        if self.by == "prefix" and not self.separator:
            raise ValueError("by prefix には separator（関数名の区切り。例 \"_\"・\"::\"）が要る")

    @property
    def per_line(self) -> bool:
        """行のファイルで分ける（file / directory、または "file:" のパターンがある）。"""
        return self.by in ("file", "directory") or any(p.startswith("file:") for _, ps in self._patterns for p in ps)

    def key(self, function: str, file: str | None) -> str:
        if self.by == "function":
            return function
        if self.by == "file":
            return _norm(file) or UNKNOWN_FILE
        if self.by == "directory":
            path = _norm(file)
            return (posixpath.dirname(path) or ".") if path else UNKNOWN_FILE
        if self.by == "prefix":
            return prefix_of(function, self.separator)
        path = _norm(file)
        for name, patterns in self._patterns:
            for p in patterns:
                if p.startswith("file:"):
                    if path is not None and fnmatch.fnmatchcase(path, p[5:]):
                        return name
                elif fnmatch.fnmatchcase(function, p):
                    return name
        return OTHER


def _norm(file: str | None) -> str | None:
    return None if file is None else file.replace("\\", "/")


def prefix_of(name: str, separator: str) -> str:
    """name の separator の最初の出現より前（先頭が区切りなら空）。区切りが無ければ name。"""
    return name.split(separator, 1)[0]


# --- 区間 ---------------------------------------------------------------------------------------

def window(samples: PcSamples, t0: int | float | None, t1: int | float | None) -> PcSamples:
    """t0 ≤ t < t1 のサンプル（省いた端は無限）。時刻の無いサンプルがあれば区間で切れない。"""
    if t0 is None and t1 is None:
        return samples
    if any(s.t is None for s in samples.samples):
        raise ValueError("時刻の無いサンプルがある: 区間で切れない（itm はローカルのタイムスタンプ、pcsr は窓の時刻が要る）")
    kept = tuple(s for s in samples.samples if (t0 is None or s.t >= t0) and (t1 is None or s.t < t1))
    return PcSamples(kept, samples.source, samples.unavailable, samples.overflows, samples.exception_trace)


# --- グループ -----------------------------------------------------------------------------------

def _group_counts(profile: Profile, grouping: Grouping) -> tuple[Counter, dict[str, Counter]]:
    counts: Counter = Counter()
    members: dict[str, Counter] = {}
    if grouping.per_line:
        items = (((f, file), n) for (f, file, _line), n in profile.line_counts.items())
    else:
        items = (((f, None), n) for f, n in profile.function_counts.items())
    for (function, file), n in items:
        key = grouping.key(function, file)
        counts[key] += n
        members.setdefault(key, Counter())[function] += n
    return counts, members


def group_profile(profile: Profile, grouping: Grouping, *, top: int = 20, members: int = 5) -> dict:
    """グループごとのサンプル数・割合・眠り以外に対する割合と、多く使った関数（members 個。切る前の
    数は functions_total）。行は top 個（切る前の数は groups_total、使った top・members も返す）。"""
    counts, parts = _group_counts(profile, grouping)
    rows = []
    for key, n in counts.items():
        rows.append({"group": key, "samples": n, "fraction": n / profile.total if profile.total else 0.0,
                     "active_fraction": n / profile.active if profile.active else 0.0,
                     "functions": [{"function": f, "samples": c} for f, c in parts[key].most_common(members)],
                     "functions_total": len(parts[key])})
    rows.sort(key=lambda r: (-r["samples"], r["group"]))
    return {"by": grouping.by, "total": profile.total, "active": profile.active,
            "categories": profile.category_table(), "groups_total": len(rows), "top": top, "members": members,
            "rows": rows[:top],
            "notes": profile.notes()}


def timeline(samples: PcSamples, profiler: Profiler, *, bucket: int | float, grouping: Grouping, top: int = 8,
             t0: int | float | None = None) -> dict:
    """bucket（サンプルの時刻の単位）ごとの CPU 使用率（load = 眠り以外の割合）と、全体で多い top 個のグループの
    割合（その区切りの全サンプルに対する。残りは "(other)"）。"""
    if bucket <= 0:
        raise ValueError("bucket は正")
    timed = window(samples, t0, None)
    if not timed.samples:
        return {"bucket": bucket, "top": top, "groups_total": 0, "groups": [], "buckets": []}
    whole = profiler.profile(timed)
    ranking, _ = _group_counts(whole, grouping)
    chosen = [k for k, _ in sorted(ranking.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]
    origin = timed.samples[0].t if t0 is None else t0
    split: dict[int, list] = {}
    for s in timed.samples:
        split.setdefault(int((s.t - origin) // bucket), []).append(s)
    buckets = []
    for index in sorted(split):
        part = PcSamples(tuple(split[index]), samples.source)
        prof = profiler.profile(part)
        counts, _ = _group_counts(prof, grouping)
        groups = {k: counts[k] / prof.total for k in chosen if counts[k]}
        other = sum(n for k, n in counts.items() if k not in chosen)
        if other:
            groups[OTHER] = other / prof.total
        buckets.append({"t0": origin + index * bucket, "samples": prof.total,
                        "load": prof.active / prof.total if prof.total else 0.0,
                        "sleep": prof.categories["sleep"] / prof.total if prof.total else 0.0, "groups": groups})
    return {"bucket": bucket, "top": top, "groups_total": len(ranking), "groups": chosen, "buckets": buckets}


# --- テストポイントの呼び出し --------------------------------------------------------------------

def call_stats(calls: Iterable, t0: int | None, t1: int | None) -> list[dict]:
    """呼び出し（fprobe.testpoint.Call）を区間 [t0, t1) で切った関数ごとの時間: inclusive（子を含む。再帰は外側だけ）・
    exclusive（区間の中の自分だけの時間）・started（区間で始まった回数）・active（区間に掛かった回数）。時刻の単位のまま。"""
    lo = float("-inf") if t0 is None else t0
    hi = float("inf") if t1 is None else t1
    ordered = sorted(calls, key=lambda c: (c.t0, c.depth, -c.t1))
    inclusive: Counter = Counter()
    exclusive: Counter = Counter()
    started: Counter = Counter()
    active: Counter = Counter()
    stack: list[tuple[object, int]] = []                     # (呼び出し, 子に区間で取られた時間)

    def clip(c) -> int:
        return max(0, min(c.t1, hi) - max(c.t0, lo))

    def close(entry) -> None:
        c, children = entry
        exclusive[c.function] += clip(c) - children

    for c in ordered:
        while stack and not (stack[-1][0].t0 <= c.t0 and c.t1 <= stack[-1][0].t1 and stack[-1][0].depth < c.depth):
            close(stack.pop())
        part = clip(c)
        if stack:
            parent, taken = stack[-1]
            stack[-1] = (parent, taken + part)
        outer = all(entry[0].function != c.function for entry in stack)
        if part and outer:
            inclusive[c.function] += part
        if part:
            active[c.function] += 1
        if lo <= c.t0 < hi:
            started[c.function] += 1
        stack.append((c, 0))
    while stack:
        close(stack.pop())
    names = set(inclusive) | set(started)
    rows = [{"function": f, "inclusive": inclusive[f], "exclusive": exclusive[f], "started": started[f],
             "active": active[f]} for f in names]
    return sorted(rows, key=lambda r: (-r["inclusive"], r["function"]))


def group_rows(rows: list[dict], grouping: Grouping, file_of: Callable[[str], str | None]) -> list[dict]:
    """call_stats の行をグループにまとめる（exclusive と started の合計。inclusive は入れ子で重なるので足さない）。"""
    out: dict[str, dict] = {}
    for r in rows:
        key = grouping.key(r["function"], file_of(r["function"]))
        g = out.setdefault(key, {"group": key, "exclusive": 0, "started": 0, "functions": []})
        g["exclusive"] += r["exclusive"]
        g["started"] += r["started"]
        g["functions"].append(r["function"])
    return sorted(out.values(), key=lambda g: (-g["exclusive"], g["group"]))

