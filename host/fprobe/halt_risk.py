"""止める前の手がかり（ELF のシンボルだけで。機器には触らない）。返すのは事実だけ: 使う側が明示して渡した名前
（完全一致）・正規表現（re.search）に合うシンボルが ELF にあるか、と、ELF に fprobe のモニタがあるか。

コアを止める操作（中断・ブレークポイント・ステップ・関数の呼び出し）の間も、ターゲットの外の相手（無線・USB の
ホスト・ウォッチドッグ等）は待たないことがある。どの名前がそれに当たるかは本体では決めない（CLAUDE.md「fprobe 本体に
ヒューリスティックを入れない」。関数名の接頭辞・部分一致で無線・USB・ウォッチドッグの種類を付けるのは、名前の慣習に
頼る推定で、別のターゲットでは外れうる）。慣習の表で種類を付けるなら、プラグイン fprobe.plugins.heuristics の
halt_risk_conventions（結果に heuristic・assumption が付く）。

止めずにデバッグする方式（モニタ方式。target.debug_mode）なら、DebugMon より優先度の高い割り込みは止まっている間も
動く。ELF にモニタ（host/firmware/monitor の fprobe_monitor_init。fprobe 自身のファームの名前）があるかも返す。

    scan(Symbols("app.elf").names(), names=["RADIO_IRQHandler"], patterns=["^ll_"])
    # {"names": [...], "patterns": [...], "matched": True,
    #  "matches": [{"query": "RADIO_IRQHandler", "by": "name", "count": 1,
    #               "symbols": [{"name": "RADIO_IRQHandler", "kind": "function", "address": 0x...}]}, ...],
    #  "monitor_in_elf": False}
"""

import re
from collections.abc import Iterable

from .symbols import Symbol

__all__ = ["MONITOR", "compile_patterns", "scan"]

MONITOR = "fprobe_monitor_init"


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern]:
    """正規表現の並びを読む（読めなければ ValueError）。"""
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error as e:
            raise ValueError(f"正規表現 {p!r} が読めない: {e}") from None
    return out


def scan(symbols: Iterable[Symbol], names: Iterable[str] = (), patterns: Iterable[str] = ()) -> dict:
    """シンボルの並び → {"names", "patterns"（渡された条件）, "matched"（どれかに合った）, "matches"（合った条件ごと。
    渡した順で、名前が先: {"query", "by" = name / pattern, "count", "symbols" = [{"name", "kind", "address"}] を名前の順に
    全部}）, "monitor_in_elf"}。合わなかった条件は matches に入れない（names・patterns との差が「無い」の事実）。"""
    names, patterns = list(names), list(patterns)
    compiled = compile_patterns(patterns)
    wanted = set(names)
    by_name: dict[str, list[Symbol]] = {n: [] for n in names}
    by_pattern: list[list[Symbol]] = [[] for _ in patterns]
    monitor = False
    for s in symbols:
        if s.name == MONITOR:
            monitor = True
        if s.name in wanted:
            by_name[s.name].append(s)
        for hits, p in zip(by_pattern, compiled):
            if p.search(s.name):
                hits.append(s)
    queries = [(n, "name", by_name[n]) for n in dict.fromkeys(names)] + \
              [(p, "pattern", hits) for p, hits in zip(patterns, by_pattern)]
    matches = []
    for query, by, hits in queries:
        if hits:
            found = sorted({(s.name, s.kind, s.address) for s in hits})
            matches.append({"query": query, "by": by, "count": len(found),
                            "symbols": [{"name": n, "kind": k, "address": a} for n, k, a in found]})
    return {"names": names, "patterns": patterns, "matched": bool(matches), "matches": matches,
            "monitor_in_elf": monitor}
