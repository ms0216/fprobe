"""相関の条件の言語: 項（<変数> changed / rose / fell・<変数>=<値>）を and でつないだ条件の解釈（fprobe.correlate が使う）。"""

import re
from dataclasses import dataclass

__all__ = ["EDGES", "Condition", "ConditionError", "Term", "parse_condition", "term_dict"]

EDGES = ("changed", "rose", "fell")


class ConditionError(ValueError):
    """条件が読めない・変数が無い・名前が曖昧。"""


@dataclass(frozen=True)
class Term:
    name: str
    op: str                      # changed / rose / fell / =
    value: str | None = None


@dataclass(frozen=True)
class Condition:
    text: str
    terms: tuple[Term, ...]


def parse_condition(text: str, need_edge: bool = True) -> Condition:
    """「restart changed and state=BUSY」→ 項の並び。瞬間の項（changed / rose / fell）が 1 つも無ければ誤り。
    空白・= を含む名前は "…" で囲む（"Error count" changed）。引用符の中の " and " は区切りにしない。"""
    outside = r'(?=(?:[^"]*"[^"]*")*[^"]*$)'                 # 後ろの引用符が偶数個 = いまは引用符の外
    parts = [p.strip() for p in re.split(rf"(?:\s+(?:and|かつ)\s+|\s*&&\s*){outside}", text.strip(), flags=re.I) if p.strip()]
    terms = []
    for p in parts:
        m = re.fullmatch(r'(?:"([^"]+)"|(\S+))\s+(changed|rose|fell|が変化)', p)
        if m:
            terms.append(Term(m[1] or m[2], "changed" if m[3] == "が変化" else m[3]))
            continue
        m = re.fullmatch(r'(?:"([^"]+)"|([^\s="]+))\s*=\s*(?:"([^"]+)"|(\S+))', p)
        if not m:
            raise ConditionError(f"項 {p!r} が読めない（<変数> changed / rose / fell か <変数>=<値>。空白・= を含む名前は \"…\" で囲む）")
        terms.append(Term(m[1] or m[2], "=", m[3] or m[4]))
    if need_edge and not any(t.op in EDGES for t in terms):
        raise ConditionError(f"条件 {text!r} に瞬間の項（changed / rose / fell）が無い")
    return Condition(text.strip(), tuple(terms))


def term_dict(t: Term) -> dict:
    """項の形（API）: {variable, op, value（= の項だけ）}。"""
    return {"variable": t.name, "op": t.op, **({"value": t.value} if t.value is not None else {})}
