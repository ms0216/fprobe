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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .recording import Event

__all__ = ["ROW_TYPES", "TONES", "CarryIn", "Rule", "Row", "RowTable", "carry_in", "lookback_ranges","classify", "rule_for", "rules_of", "summarize",
           "summary_sql"]

ROW_TYPES = ("intervals", "bus", "state", "points", "values")
# 色の意味 = 出来事の事実の種類（良し悪しの判定ではない。画面が種類ごとの色で塗る）: plain = ほか / failure = 失敗・
# 欠け・異常の事実（ACK が OK でない・誤り・取りこぼし・欠け・優先度の逆転・ロックアップ・切断）/ running = ターゲットが
# 走っている / stop = 止める・止まった（HALTED・DHCSR の停止の書き込み）/ mark = 印 / other = 解読の残りの種類
TONES = {"plain": 0, "failure": 1, "running": 2, "stop": 3, "mark": 4, "other": 5}
NO_LABEL = 0xFFFF_FFFF


def _body(e: Event, key: str, default: Any = None) -> Any:
    return e.body.get(key, default) if isinstance(e.body, dict) else default


def _char(value: int) -> str:
    """1 バイトの表示（印字できる ASCII はその文字、改行等は \\n、ほかは \\xHH）。"""
    value &= 0xFF
    return {0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t", 0x20: "␣"}.get(value) or (
        chr(value) if 0x21 <= value < 0x7F else f"\\x{value:02X}")


def _hex(value: Any) -> str:
    return f"0x{int(value) & 0xFFFF_FFFF:08X}" if isinstance(value, int) else str(value)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# --- 規則 ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """stream の中の段の規則。kinds が None の規則は、その stream の他の規則に当たらない残り。

    key / key_sql: 段を分ける値（None なら 1 段）。title / title_sql: 段の名前（データから。無ければ画面が規則の名前で）。
    value / value_sql: values の段の値。label: 出来事の文字。depth: 入れ子の深さ（無ければ nest = True のとき重なりで数える）。
    tone: 色の意味（TONES の事実の種類）。address: ダブルクリックの飛び先（jump = memory / source）。
    """
    stream: str
    name: str
    type: str
    kinds: tuple[str, ...] | None = None
    dotted: bool = False                                   # kind が "<protocol>.<種類>"（解読の注釈）
    key: Callable[[Event], Any] | None = None
    key_sql: str | None = None
    title: Callable[[Event], str | None] | None = None
    title_sql: str | None = None
    label: Callable[[Event], str | None] | None = None
    value: Callable[[Event], float | None] | None = None
    value_sql: str | None = None
    unit: str | None = None
    depth: Callable[[Event], int] | None = None
    nest: bool = False
    tone: Callable[[Event], str] | None = None
    address: Callable[[Event], int | None] | None = None
    jump: str | None = None
    hidden: bool = False

    def matches(self, e: Event) -> bool:
        if self.dotted:
            return "." in e.kind
        return self.kinds is None or e.kind in self.kinds

    def where_sql(self, others: Sequence[Rule]) -> tuple[str, list]:
        """この規則に当たる出来事の SQL の条件（kinds が None なら、同じ stream の他の規則に当たらないもの）。"""
        if self.dotted:
            return "instr(kind, '.') > 0", []
        if self.kinds is not None:
            return f"kind IN ({','.join('?' * len(self.kinds))})", list(self.kinds)
        clauses, args = [], []
        for r in others:
            if r is self:
                continue
            if r.dotted:
                clauses.append("instr(kind, '.') = 0")
            elif r.kinds is not None:
                clauses.append(f"kind NOT IN ({','.join('?' * len(r.kinds))})")
                args.extend(r.kinds)
        return " AND ".join(clauses) or "1", args


# Cortex-M のデバッグ・システム制御のレジスタ（ARMv6-M / v7-M / v8-M 共通の番地）。DAP の段のラベルに名前で出す
_DEBUG_REGISTERS = {0xE000EDF0: "DHCSR", 0xE000EDF4: "DCRSR", 0xE000EDF8: "DCRDR", 0xE000EDFC: "DEMCR",
                    0xE000ED0C: "AIRCR", 0xE000ED30: "DFSR", 0xE000ED00: "CPUID", 0xE0002000: "FP_CTRL"}


def _dhcsr_action(e: Event) -> str | None:
    """DHCSR への書き込み（DBGKEY つき）の意味: halt（C_HALT）/ step（C_STEP）/ run（どちらも無い）。それ以外は None。"""
    if e.kind not in ("ap_write", "inject") or e.a != 0xE000EDF0 or e.b >> 16 != 0xA05F:
        return None
    return "step" if e.b & 0x4 else "halt" if e.b & 0x2 else "run"


def _ap_label(e: Event) -> str:
    """AP の読み書きのラベル: R / W、番地（デバッグのレジスタは名前）、値、DHCSR の書き込みは意味（halt 等）。"""
    action = _dhcsr_action(e)
    head = "xtrig W" if e.kind == "inject" else "R" if "read" in e.kind else "W"
    transfers = _body(e, "transfers", None) if e.kind == "inject" else None
    return (f"{head} {_DEBUG_REGISTERS.get(e.a, _hex(e.a))} = {_hex(e.b)}" + (f" ({action})" if action else "")
            + (f" [{transfers} transfers]" if transfers is not None else ""))


def _j(path: str) -> str:
    """本体の JSON の欄（SQL）。"""
    return f"json_extract(body, '$.{path}')"


_NUMERIC = "json_type(body, '$.{0}') IN ('integer', 'real')"


def _rules() -> dict[str, list[Rule]]:
    task = lambda e: _body(e, "task")                                      # noqa: E731
    rtos = [
        Rule("rtos", "task", "intervals", ("run",), key=lambda e: e.a, key_sql="a", title=task, title_sql=_j("task"),
             label=task),
        Rule("rtos", "isr", "intervals", ("isr",), key=lambda e: e.a, key_sql="a", title=lambda e: _body(e, "name"),
             title_sql=_j("name"), label=lambda e: _body(e, "name"), nest=True),
        Rule("rtos", "blocked", "intervals", ("blocked",), nest=True,
             label=lambda e: f"{_body(e, 'task', '?')} → {_body(e, 'on', '?')}"),
        Rule("rtos", "inversion", "intervals", ("inversion",), label=lambda e: "inversion", tone=lambda e: "failure"),
        Rule("rtos", "gap", "intervals", ("gap",), label=lambda e: _body(e, "reason"), tone=lambda e: "failure"),
        Rule("rtos", "switch", "points", ("switch",), hidden=True,
             label=lambda e: f"{_body(e, 'from') or '?'} → {_body(e, 'to') or '?'}"),
        Rule("rtos", "kernel", "points", label=lambda e: e.kind),
    ]
    byte = lambda e: _char(e.b)                                            # noqa: E731
    uart = [
        Rule("uart", "rx", "bus", ("rx",), label=byte, value=lambda e: float(e.b & 0xFF)),
        Rule("uart", "tx", "bus", ("tx",), label=byte, value=lambda e: float(e.b & 0xFF)),
        Rule("uart", "other", "points", label=lambda e: e.kind),
    ]
    swo = [
        Rule("swo", "itm", "bus", ("byte",), label=byte),
        Rule("swo", "data", "values", ("data_access",), key=lambda e: e.a, key_sql="a",
             value=lambda e: float(e.b), value_sql="b", address=lambda e: e.a, jump="memory"),
        Rule("swo", "other", "points", label=lambda e: e.kind, tone=lambda e: "other"),
    ]
    dap_label = lambda e: (f"{'R' if 'read' in e.kind else 'W'} {_hex(e.a)}" +           # noqa: E731
                           (f" = {_hex(e.b)}" if e.kind != "fail" else ""))
    dap = [
        Rule("dap", "ap", "points", ("ap_read", "ap_write", "inject"), label=_ap_label, address=lambda e: e.a, jump="memory",
             tone=lambda e: "failure" if _body(e, "ack", 1) != 1 else "stop" if _dhcsr_action(e) == "halt" else "plain"),
        Rule("dap", "dp", "points", ("dp_read", "dp_write"), label=dap_label,
             tone=lambda e: "failure" if _body(e, "ack", 1) != 1 else "plain"),
        # メモリの番地を追えなかった DRW（番地の段 ap には置かない。番地へ飛ばない）
        Rule("dap", "drw", "points", ("drw_read", "drw_write"),
             label=lambda e: f"{'R' if 'read' in e.kind else 'W'} DRW (address not tracked) = {_hex(e.b)}",
             tone=lambda e: "failure" if _body(e, "ack", 1) != 1 else "plain"),
        Rule("dap", "fail", "points", label=lambda e: f"{e.kind} {_hex(e.a)}", tone=lambda e: "failure"),
    ]
    aux = [Rule("aux", "voltage", "values", ("mv",), value=lambda e: e.b / 1000,
                value_sql="b / 1000.0", unit="V"),
           Rule("aux", "other", "points", label=lambda e: e.kind)]
    sample = [Rule("sample", "word", "values", key=lambda e: e.a, key_sql="a", title=lambda e: _hex(e.a),
                   title_sql="printf('0x%08X', a)",
                   value=lambda e: float(e.b) if _body(e, "ack", 1) == 1 else None,          # 読めなかった語は値なし
                   value_sql=f"CASE WHEN COALESCE({_j('ack')}, 1) = 1 THEN b END",
                   address=lambda e: e.a, jump="memory",
                   tone=lambda e: "failure" if _body(e, "ack", 1) != 1 else "plain")]
    name = lambda e: _body(e, "name")                                      # noqa: E731
    user = [
        Rule("user", "value", "values", ("value",), key=lambda e: e.a, key_sql="a", title=name, title_sql=_j("name"),
             value=lambda e: _number(_body(e, "value")) if _number(_body(e, "value")) is not None else float(e.b),
             value_sql=f"CASE WHEN {_NUMERIC.format('value')} THEN {_j('value')} ELSE b END"),
        Rule("user", "interval", "intervals", ("interval",), key=name, key_sql=_j("name"), title=name,
             title_sql=_j("name"), label=name, depth=lambda e: int(_body(e, "depth", 0) or 0)),
        Rule("user", "event", "points", title=None, label=lambda e: _body(e, "name") or e.kind),
    ]
    annotation = [
        Rule("annotation", "mark", "points", ("mark",), label=lambda e: _body(e, "text"), tone=lambda e: "mark"),
        Rule("annotation", "decode", "bus", dotted=True, key=lambda e: e.kind.split(".", 1)[0],
             key_sql="substr(kind, 1, instr(kind, '.') - 1)", title=lambda e: e.kind.split(".", 1)[0].upper(),
             title_sql="upper(substr(kind, 1, instr(kind, '.') - 1))",
             label=lambda e: _body(e, "text") or e.kind.split(".", 1)[1],
             value=lambda e: _number(_body(e, "value")),
             tone=lambda e: "failure" if _body(e, "error") else "plain"),
        Rule("annotation", "loss", "points", label=lambda e: e.kind, tone=lambda e: "failure"),
    ]
    role = lambda e: _body(e, "role") or "other"                           # noqa: E731
    host = [Rule("host", "call", "points", ("call",), key=role, key_sql=f"COALESCE({_j('role')}, 'other')",
                 label=lambda e: _body(e, "method"), tone=lambda e: "failure" if _body(e, "error") else "plain"),
            Rule("host", "other", "points", label=lambda e: e.kind)]

    def state_tone(e: Event) -> str:
        s = _body(e, "state")
        return {"RUNNING": "running", "HALTED": "stop", "DISCONNECTED": "failure", "LOCKUP": "failure"}.get(s, "plain")

    def pc_of(e: Event) -> int | None:
        pc = _body(e, "pc")
        return pc if isinstance(pc, int) else None

    target = [Rule("target", "state", "state", ("state",),
                   label=lambda e: " ".join(str(x) for x in (_body(e, "state"), _body(e, "halt_reason")) if x),
                   tone=state_tone, address=pc_of, jump="source"),
              Rule("target", "other", "points", label=lambda e: e.kind)]
    testpoint = [
        Rule("testpoint", "call", "intervals", ("call",), label=lambda e: _body(e, "name") or f"#{e.a}",
             depth=lambda e: int(e.b)),
        Rule("testpoint", "anomaly", "points", ("anomaly",), label=lambda e: _body(e, "message") or e.kind,
             tone=lambda e: "failure"),
        Rule("testpoint", "hit", "points", label=lambda e: _body(e, "name") or f"#{e.a}"),
    ]
    snapshot = [Rule("snapshot", "save", "points", label=lambda e: e.kind, address=pc_of, jump="source")]
    # FPGA の観測: 段は fprobe.timeline_fpga が記録の信号の定義で展開した仮の出来事から（生の値の SQL は使わない）
    row = lambda e: _body(e, "row")                                      # noqa: E731
    fpga = [Rule("fpga", "state", "state", ("signal_state",), key=row, title=lambda e: _body(e, "title"),
                 label=lambda e: _body(e, "text")),
            Rule("fpga", "number", "values", ("signal_number",), key=row, title=lambda e: _body(e, "title"),
                 label=lambda e: _body(e, "text"), value=lambda e: None if _body(e, "unknown") else float(e.b)),
            Rule("fpga", "gap", "points", ("gap",), label=lambda e: f"-{e.a}", tone=lambda e: "failure"),
            Rule("fpga", "hidden", "points", ("hidden",), label=lambda e: f"+{e.a}"),     # 標本型の標本の間の変化
            Rule("fpga", "group", "points", ("group",), label=lambda e: f"#{e.a}"),
            Rule("fpga", "other", "points", label=lambda e: e.kind)]
    clock = [Rule("clock", "point", "points", hidden=True)]
    out: dict[str, list[Rule]] = {}
    for rules in (rtos, uart, swo, dap, aux, sample, user, annotation, host, target, testpoint, snapshot, clock, fpga):
        out[rules[0].stream] = rules
    return out


RULES = _rules()


def rules_of(stream: str) -> list[Rule]:
    """stream の規則（知らない stream は kind ごとの印の段）。"""
    return RULES.get(stream) or [Rule(stream, "kind", "points", key=lambda e: e.kind, key_sql="kind",
                                      title=lambda e: e.kind, title_sql="kind", label=lambda e: e.kind)]


def rule_for(stream: str, e: Event) -> Rule:
    rules = rules_of(stream)
    specific = next((r for r in rules if (r.kinds is not None or r.dotted) and r.matches(e)), None)
    return specific or next((r for r in rules if r.kinds is None and not r.dotted), rules[-1])


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
    return f"{base}:{_hex(key) if isinstance(key, int) else key}"


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
