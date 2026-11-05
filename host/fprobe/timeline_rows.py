"""共通タイムラインの段（G01）: 記録の出来事を、画面の段（行）に分ける規則。

仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §2.1（段の規則）

記録の stream は「段のまとまり」（画面で畳める）。まとまりの中の段は、出来事の (kind, a, 本体) から規則で決まる:

| 段の型 | 描き方 | 例 |
|---|---|---|
| intervals | 区間（色の帯。重なりは深さで入れ子） | RTOS のタスクの実行・割り込み（入れ子）・ユーザの区間 |
| bus | 解読した値の箱（文字） | UART の RX / TX・SWO の ITM・解読（UART / SWD / I2C …） |
| state | 次の変化までの状態の帯 | ターゲットの状態（RUNNING / HALTED） |
| points | 印（三角） | 印・操作（AI と人）・DAP の転送・カーネルの出来事・データの欠け |
| values | 値のグラフ（前の値を保つ階段） | 変数のサンプル・補助の電圧・ユーザの値・DWT のデータの読み書き |

規則は Python の関数（生データ: 出来事ごとに段・ラベル・値・深さ・色の意味・飛び先の番地）と、同じ意味の SQL の式
（要約: 区間 × 段の件数を 1 回の GROUP BY で数える）の両方で書く。両者が同じ段に分けることは試験で確かめる。
"""

from __future__ import annotations

import heapq
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .recording import Event
from .timeline_rules import ROW_TYPES, TONES, Rule, hex32, rule_for, rules_of

__all__ = ["ROW_TYPES", "TONES", "CarryIn", "Rule", "Row", "RowTable", "carry_in", "lookback_ranges", "classify", "rule_for", "rules_of",
           "summarize", "summary_sql"]

NO_LABEL = 0xFFFF_FFFF


# --- 段の表 -------------------------------------------------------------------------------------------

@dataclass
class Row:
    id: str
    rule: Rule
    title: str | None

    def to_dict(self) -> dict:
        d = {"id": self.id, "rule": self.rule.name, "type": self.rule.type, "title": self.title}
        for k in ("unit", "jump"):
            if getattr(self.rule, k):
                d[k] = getattr(self.rule, k)
        if self.rule.hidden:
            d["hidden"] = True
        return d


def row_id(rule: Rule, key: Any) -> str:
    """段の名前（記録の中で一意。画面はこれで段の並び・表示を覚える）。"""
    base = f"{rule.stream}.{rule.name}"
    if rule.key is None:
        return base
    return f"{base}:{hex32(key) if isinstance(key, int) else key}"


@dataclass
class RowTable:
    """1 つの stream の段（出てきた順。規則の順 → 名前で並べ直す）。"""
    stream: str
    rows: list[Row] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict)

    def add(self, rule: Rule, key: Any, title: str | None) -> int:
        rid = row_id(rule, key)
        i = self._index.get(rid)
        if i is None:
            i = self._index[rid] = len(self.rows)
            self.rows.append(Row(rid, rule, title))
        elif title and not self.rows[i].title:
            self.rows[i].title = title
        return i

    def ordered(self) -> list[int]:
        """表示の順の並べ替え（old index → new index）: 規則の順、同じ規則の中は名前（無ければ段の名前）の順。"""
        order = {r.name: k for k, r in enumerate(rules_of(self.stream))}
        ranked = sorted(range(len(self.rows)), key=lambda i: (order.get(self.rows[i].rule.name, 99),
                                                              str(self.rows[i].title or self.rows[i].id).casefold()))
        remap = [0] * len(self.rows)
        for new, old in enumerate(ranked):
            remap[old] = new
        self.rows = [self.rows[i] for i in ranked]
        self._index = {r.id: k for k, r in enumerate(self.rows)}
        return remap


@dataclass
class Classified:
    """生データの段の区画の材料（出来事の並びは t の昇順のまま）。"""
    table: RowTable
    row: list[int]
    label: list[int]
    labels: list[str]
    value: list[float]
    depth: list[int]
    tone: list[int]
    address: list[float]


def classify(stream: str, events: Sequence[Event]) -> Classified:
    """生の出来事（t の昇順）を段に分け、ラベル（重ねない表）・値・深さ・色の意味・飛び先の番地を付ける。"""
    table = RowTable(stream)
    labels: list[str] = []
    label_index: dict[str, int] = {}
    rows, label, value, depth, tone, address = [], [], [], [], [], []
    active: dict[str, list[int]] = {}                      # nest の規則ごとの、開いている区間の終わり（ヒープ）
    for e in events:
        rule = rule_for(stream, e)
        key = rule.key(e) if rule.key else None
        rows.append(table.add(rule, key, rule.title(e) if rule.title else None))
        text = rule.label(e) if rule.label else None
        if text is None:
            label.append(NO_LABEL)
        else:
            text = str(text)
            if text not in label_index:
                label_index[text] = len(labels)
                labels.append(text)
            label.append(label_index[text])
        v = rule.value(e) if rule.value else None
        value.append(math.nan if v is None else float(v))
        if rule.depth is not None:
            depth.append(max(0, min(255, rule.depth(e))))
        elif rule.nest:
            ends = active.setdefault(rule.name, [])
            while ends and ends[0] <= e.t:
                heapq.heappop(ends)
            depth.append(min(255, len(ends)))
            heapq.heappush(ends, e.t if e.t_end is None else e.t_end)
        else:
            depth.append(0)
        tone.append(TONES[rule.tone(e)] if rule.tone else 0)
        a = rule.address(e) if rule.address else None
        address.append(math.nan if a is None else float(a))
    remap = table.ordered()
    return Classified(table, [remap[r] for r in rows], label, labels, value, depth, tone, address)


@dataclass
class Summarized:
    """要約の段の区画の材料（段 × 区間）。"""
    table: RowTable
    counts: list[int]                                      # 段 r の区間 b = counts[r * bins + b]
    low: list[float] | None                                # values の段の最小・最大（他の段・空の区間は NaN）
    high: list[float] | None


def summarize(stream: str, db_rows: Iterable[tuple], bins: int) -> Summarized:
    """要約の問い合わせ（summary_sql）の行 (規則の名前, 区間, 段の値, 名前, 件数, 最小, 最大) を段 × 区間の配列に。
    同じ段・区間の行が複数（長期の記録の分割ごと）なら足し合わせる。"""
    table = RowTable(stream)
    by_name = {r.name: r for r in rules_of(stream)}
    cells = []
    for name, b, key, title, n, lo, hi in db_rows:
        rule = by_name[name]
        cells.append((table.add(rule, key, title), b, n, lo, hi))
    remap = table.ordered()
    rows = len(table.rows)
    counts = [0] * (rows * bins)
    valued = any(r.rule.type == "values" for r in table.rows)
    low = [math.nan] * (rows * bins) if valued else None
    high = [math.nan] * (rows * bins) if valued else None
    for r, b, n, lo, hi in cells:
        if not 0 <= b < bins:
            continue
        i = remap[r] * bins + b
        counts[i] += n
        if valued and lo is not None:
            low[i] = lo if math.isnan(low[i]) else min(low[i], lo)
            high[i] = hi if math.isnan(high[i]) else max(high[i], hi)
    return Summarized(table, counts, low, high)


def summary_sql(stream: str, start: int, width: int, count: int) -> list[tuple[str, list]]:
    """stream の規則ごとの要約の問い合わせ: (規則の名前, 区間, 段の値, 名前, 件数, 最小, 最大)。"""
    rules = rules_of(stream)
    out = []
    for rule in rules:
        where, args = rule.where_sql(rules)
        key = rule.key_sql if rule.key is not None else "NULL"
        title = f"MAX({rule.title_sql})" if rule.title_sql else "NULL"
        value = rule.value_sql if rule.value_sql else "NULL"
        sql = (f"SELECT ?, (t - ?) / ?, {key} AS k, {title}, COUNT(*), MIN({value}), MAX({value}) FROM events "
               f"WHERE stream = ? AND t >= ? AND t < ? AND {where} GROUP BY 2, k")
        out.append((sql, [rule.name, start, width, stream, start, start + width * count, *args]))
    return out


CARRY_LOOKBACK = 16                    # 状態・値の段の、t0 の前を探す幅（範囲の幅の倍。見つからなければ広げる）
CARRY_TRIES = 3


@dataclass(frozen=True)
class CarryIn:
    """範囲の前の最後の出来事（段ごと）と、探した範囲の始め searched_from（None = 範囲の前に記録が無く探していない）。
    complete = 記録の始めまで探したか。False なら、searched_from より前に変わったきりの段は返っていない（範囲の始めの値は
    探していないので分からない。無いのではない）。"""
    events: list[Event]
    searched_from: int | None
    complete: bool

    def facts(self) -> dict:
        """時間軸の段に載せる事実（carry_from・carry_complete）。"""
        return {"carry_from": self.searched_from, "carry_complete": self.complete}


def lookback_ranges(span: tuple[int, int] | None, t0: int, t1: int):
    """範囲の前を探す始めの時刻の列: 範囲の幅の 16 倍・256 倍・4096 倍、最後は記録の始め（同じ時刻は 1 回）。"""
    if span is None or span[0] >= t0:
        return []
    width = max(1, t1 - t0)
    out: list[int] = []
    for k in range(CARRY_TRIES + 1):
        lo = max(span[0], t0 - width * CARRY_LOOKBACK ** (k + 1)) if k < CARRY_TRIES else span[0]
        if not out or lo != out[-1]:
            out.append(lo)
    return out


def carry_in(rec: Any, stream: str, t0: int, t1: int) -> CarryIn:
    """状態（state）・値（values）の段の、t0 の前の最後の出来事（段ごと）。範囲の始めでの状態・保たれている値を
    描くため。範囲の幅の 16 倍・256 倍・4096 倍と前を探し（それでも何も無ければ記録の始めまで）、何か見つかった所で
    止める。長い間変わらない段がほかにあっても、そこまでは探さない: 探した始めを searched_from に返す（黙って打ち切らない）。"""
    rules = rules_of(stream)
    held = [r for r in rules if r.type in ("state", "values")]
    span = rec.span()
    starts = lookback_ranges(span, t0, t1) if held else []
    if not starts:
        return CarryIn([], None, True)
    for lo in starts:
        queries = []
        for rule in held:
            where, args = rule.where_sql(rules)
            key = rule.key_sql if rule.key is not None else "''"
            queries.append((f"SELECT t, stream, kind, a, b, body, t_end, unc, MAX(t) FROM events "
                            f"WHERE stream = ? AND t >= ? AND t < ? AND (t_end IS NULL OR t_end <= ?) AND {where} "
                            f"GROUP BY {key}", [stream, lo, t0, t0, *args]))
        latest: dict[str, Event] = {}
        for t, s, kind, a, b, body, t_end, unc, _ in rec.query_events(lo, t0, queries):
            e = Event(t, s, kind, a, b, None if body is None else json.loads(body), t_end, unc)
            rule = rule_for(stream, e)
            rid = row_id(rule, rule.key(e) if rule.key else None)
            if rid not in latest or latest[rid].t < e.t:
                latest[rid] = e
        if latest or lo == span[0]:
            return CarryIn(sorted(latest.values(), key=lambda e: e.t), lo, lo == span[0])
    raise AssertionError("lookback_ranges の最後は記録の始め")
