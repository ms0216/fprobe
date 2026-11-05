"""C の式の字句: 誤りの型と、式の文字列を字句（種別, 値）の並びにする。

構文の解析は cexpr.py、値の演算・型の組み立ては cexpr_values.py。
"""

from __future__ import annotations

import re

__all__ = ["BASE_WORDS", "ExpressionError", "QUALIFIER_WORDS", "TAG_WORDS", "UnknownNameError", "tokens"]


class ExpressionError(ValueError):
    """式が書けていない・型が合わない・名前が無い。"""


class UnknownNameError(ExpressionError):
    """名前が変数にも列挙子にも無い（引いたスコープに無い。PC の無い評価ではローカル変数もこれ）。"""


_TOKEN = re.compile(r"""
    \s*(?:
      (?P<float>\d+\.\d*(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)[fF]?
    | (?P<number>0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)(?P<suffix>[uUlL]*)
    | (?P<char>'(?:\\.|[^\\'])')
    | (?P<file>"[^"]+")
    | (?P<name>[A-Za-z_]\w*)
    | (?P<op>->|::|\|\||&&|==|!=|<=|>=|<<|>>|[.\[\]()*&+\-~!<>|^/%])
    )""", re.VERBOSE)
_ESCAPES = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34, "a": 7, "b": 8, "f": 12, "v": 11}
BASE_WORDS = {"void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "_Bool"}
QUALIFIER_WORDS = {"const", "volatile"}
TAG_WORDS = {"struct", "union", "enum"}


def tokens(text: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise ExpressionError(f"式の {pos + 1} 文字目が読めない: {text[pos:pos + 10]!r}")
        pos = m.end()
        if m["float"]:
            out.append(("number", float(m["float"])))
        elif m["number"]:
            n = m["number"].lower()
            try:
                value = (int(n, 16) if n.startswith("0x") else int(n[2:], 2) if n.startswith("0b")
                         else int(n, 8) if len(n) > 1 and n.startswith("0") else int(n, 10))   # 先頭 0 は C の 8 進
            except ValueError:
                raise ExpressionError(f"整数として読めない: {m['number']}") from None
            out.append(("unumber" if "u" in m["suffix"].lower() else "number", value))   # u 付きは unsigned
        elif m["char"]:
            body = m["char"][1:-1]
            out.append(("number", _ESCAPES.get(body[1], ord(body[1])) if body.startswith("\\") else ord(body)))
        elif m["file"]:
            out.append(("file", m["file"]))
        elif m["name"]:
            out.append(("name", m["name"]))
        else:
            out.append(("op", m["op"]))
    out.append(("end", None))
    return out
