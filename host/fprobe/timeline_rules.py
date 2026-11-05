"""共通タイムラインの段の規則（fprobe.timeline_rows が使う）: stream ごとの規則の表。

規則は Python の関数（生データ: 出来事ごとに段・ラベル・値・深さ・色の意味・飛び先の番地）と、同じ意味の SQL の式
（要約: 区間 × 段の件数を 1 回の GROUP BY で数える）の両方で書く。両者が同じ段に分けることは試験で確かめる。
段の型・色の意味・分け方の全体は fprobe.timeline_rows の説明。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .recording import Event

__all__ = ["ROW_TYPES", "RULES", "TONES", "Rule", "hex32", "rule_for", "rules_of"]

ROW_TYPES = ("intervals", "bus", "state", "points", "values")
# 色の意味 = 出来事の事実の種類（良し悪しの判定ではない。画面が種類ごとの色で塗る）: plain = ほか / failure = 失敗・
# 欠け・異常の事実（ACK が OK でない・誤り・取りこぼし・欠け・優先度の逆転・ロックアップ・切断）/ running = ターゲットが
# 走っている / stop = 止める・止まった（HALTED・DHCSR の停止の書き込み）/ mark = 印 / other = 解読の残りの種類
TONES = {"plain": 0, "failure": 1, "running": 2, "stop": 3, "mark": 4, "other": 5}


def _body(e: Event, key: str, default: Any = None) -> Any:
    return e.body.get(key, default) if isinstance(e.body, dict) else default


def _char(value: int) -> str:
    """1 バイトの表示（印字できる ASCII はその文字、改行等は \\n、ほかは \\xHH）。"""
    value &= 0xFF
    return {0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t", 0x20: "␣"}.get(value) or (
        chr(value) if 0x21 <= value < 0x7F else f"\\x{value:02X}")


def hex32(value: Any) -> str:
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
    return (f"{head} {_DEBUG_REGISTERS.get(e.a, hex32(e.a))} = {hex32(e.b)}" + (f" ({action})" if action else "")
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
    dap_label = lambda e: (f"{'R' if 'read' in e.kind else 'W'} {hex32(e.a)}" +           # noqa: E731
                           (f" = {hex32(e.b)}" if e.kind != "fail" else ""))
    dap = [
        Rule("dap", "ap", "points", ("ap_read", "ap_write", "inject"), label=_ap_label, address=lambda e: e.a, jump="memory",
             tone=lambda e: "failure" if _body(e, "ack", 1) != 1 else "stop" if _dhcsr_action(e) == "halt" else "plain"),
        Rule("dap", "dp", "points", ("dp_read", "dp_write"), label=dap_label,
             tone=lambda e: "failure" if _body(e, "ack", 1) != 1 else "plain"),
        # メモリの番地を追えなかった DRW（番地の段 ap には置かない。番地へ飛ばない）
        Rule("dap", "drw", "points", ("drw_read", "drw_write"),
             label=lambda e: f"{'R' if 'read' in e.kind else 'W'} DRW (address not tracked) = {hex32(e.b)}",
             tone=lambda e: "failure" if _body(e, "ack", 1) != 1 else "plain"),
        Rule("dap", "fail", "points", label=lambda e: f"{e.kind} {hex32(e.a)}", tone=lambda e: "failure"),
    ]
    aux = [Rule("aux", "voltage", "values", ("mv",), value=lambda e: e.b / 1000,
                value_sql="b / 1000.0", unit="V"),
           Rule("aux", "other", "points", label=lambda e: e.kind)]
    sample = [Rule("sample", "word", "values", key=lambda e: e.a, key_sql="a", title=lambda e: hex32(e.a),
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
