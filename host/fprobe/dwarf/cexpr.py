"""C の簡単な式を、DWARF の型付きで評価する。

    expr    := unary (('+' | '-') unary)*
    unary   := ('*' | '&' | '-' | '~' | '!') unary | '(' 型名 ')' unary | postfix
    postfix := primary ('[' expr ']' | '.' 名前 | '->' 名前)*
    primary := 名前 ('::' 名前)? | '"' ファイル '"' '::' 名前 ('::' 名前)? | 整数（10 進・0x・0b・'c'） | '(' expr ')'
    型名    := (const|volatile)* (struct|union|enum 名前 | 基本型の語 | typedef 名) ('*' (const|volatile)*)*

名前は選んだフレームのスコープ → その CU の static → 大域 の順に引く（DebugInfo.lookup）。
`関数名::名前` で関数の中の static。同じ名前の static が複数あれば選ばずに誤り（AmbiguousNameError）、
`"ファイル"::名前`・`"ファイル"::関数名::名前` で選ぶ。結果は Value（型 + 場所）で、読み出し・解読・書き込みは呼ぶ側（Inspector）が行う。
"""

from __future__ import annotations

from .access import AccessError
from .cexpr_lex import (BASE_WORDS as _BASE_WORDS, QUALIFIER_WORDS as _QUALIFIER_WORDS, TAG_WORDS as _TAG_WORDS,
                        ExpressionError, UnknownNameError, tokens as _tokens)
from .cexpr_values import Value, _ValueOps
from .info import AmbiguousNameError, DebugInfo, FrameView
from .location import EvalContext, no_register
from .typesys import CType, PointerType, QualifiedType

__all__ = ["ExpressionError", "UnknownNameError", "Value", "evaluate", "evaluate_number"]


# --- 評価 ------------------------------------------------------------------------------------------

def evaluate(text: str, info: DebugInfo, frame: FrameView | None = None) -> Value:
    """式を評価する。frame はローカル変数を引くフレーム（None なら大域だけ）。"""
    return _Evaluator(info, frame, _tokens(text)).run()


def evaluate_number(text: str, info: DebugInfo, frame: FrameView | None = None) -> tuple[CType, int | float]:
    """式を評価して数にする（整数・列挙・ポインタ・浮動小数。配列は先頭の番地）: (式の型, 値)。関数の引数等に使う。"""
    evaluator = _Evaluator(info, frame, _tokens(text))
    value = evaluator.run()
    return value.type, evaluator.scalar(value)


class _Evaluator(_ValueOps):
    def __init__(self, info: DebugInfo, frame: FrameView | None, tokens):
        self.info = info
        self.frame = frame
        self.tokens = tokens
        self.i = 0
        self.skip = 0                                 # > 0: 短絡評価で値を使わない右辺（メモリを読まない）
        memory = frame.read_memory if frame else _no_memory
        registers = frame.read_register if frame else _no_register
        self.context = EvalContext(memory, registers, load_bias=frame.load_bias if frame else 0)

    # 字句の操作
    def peek(self, k: int = 0):
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def take(self):
        token = self.tokens[self.i]
        self.i += 1
        return token

    def accept(self, op: str) -> bool:
        if self.peek() == ("op", op):
            self.i += 1
            return True
        return False

    def expect(self, op: str) -> None:
        if not self.accept(op):
            raise ExpressionError(f"{op!r} が要る（{self._where()}）")

    def _where(self) -> str:
        kind, value = self.peek()
        return "式の終わり" if kind == "end" else f"{value!r} の前"

    def run(self) -> Value:
        value = self.expression()
        if self.peek()[0] != "end":
            raise ExpressionError(f"式の終わりに余りがある（{self._where()}）")
        return value

    # 構文（二項演算子は C の優先順位。弱い順）
    _LEVELS = (("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("<<", ">>"),
               ("+", "-"), ("*", "/", "%"))

    def expression(self) -> Value:
        return self.binary(0)

    def binary(self, level: int) -> Value:
        if level == len(self._LEVELS):
            return self.unary()
        ops = self._LEVELS[level]
        left = self.binary(level + 1)
        while True:
            op = next((o for o in ops if self.accept(o)), None)
            if op is None:
                return left
            if op in ("&&", "||"):
                lv = bool(self.scalar(left))
                decided = lv if op == "||" else not lv            # 左で決まる: 右は構文だけ読み、値は使わない
                self.skip += decided
                try:
                    right = self.binary(level + 1)
                finally:
                    self.skip -= decided
                left = self.constant(int(lv if decided else bool(self.scalar(right))))
            elif op in ("+", "-"):
                left = self.add(left, self.binary(level + 1), +1 if op == "+" else -1)
            else:
                left = self.apply(op, left, self.binary(level + 1))


    def unary(self) -> Value:
        if self.accept("*"):
            return self.deref(self.unary())
        if self.accept("&"):
            return self.address_of(self.unary())
        if self.accept("-"):
            return self.arith(self.unary(), lambda v: -v, "-")
        if self.accept("~"):
            return self.arith(self.unary(), lambda v: ~v, "~", integer_only=True)
        if self.accept("!"):
            operand = self.scalar(self.unary())
            return self.constant(int(not operand))
        if self.peek() == ("op", "(") and self.is_type_start(1):
            self.take()
            target = self.type_name()
            self.expect(")")
            return self.cast(target, self.unary())
        return self.postfix()

    def postfix(self) -> Value:
        value = self.primary()
        while True:
            if self.accept("["):
                index = self.scalar(self.expression())
                self.expect("]")
                if not isinstance(index, int):
                    raise ExpressionError("添字は整数")
                value = self.index(value, index)
            elif self.accept("."):
                value = self.member(value, self.name())
            elif self.accept("->"):
                value = self.member(self.deref(value), self.name())
            else:
                return value

    def primary(self) -> Value:
        kind, token = self.take()
        if kind == "number":
            return self.constant(token)
        if kind == "unumber":
            return self.rvalue(self._int_type(4 if token < (1 << 32) else 8, False), token)
        if kind == "file":                                  # "ファイル"::名前・"ファイル"::関数::名前（同じ名前の static を選ぶ）
            self.expect("::")
            name = f"{token[1:-1]}::{self.name()}"
            if self.accept("::"):
                name = f"{name}::{self.name()}"
            return self.variable(name)
        if kind == "name":
            name = token
            if self.accept("::"):
                name = f"{name}::{self.name()}"
            return self.variable(name)
        if (kind, token) == ("op", "("):
            value = self.expression()
            self.expect(")")
            return value
        raise ExpressionError(f"値が要る（{token!r}）" if kind != "end" else "式が途中で終わっている")

    def name(self) -> str:
        kind, token = self.take()
        if kind != "name":
            raise ExpressionError(f"名前が要る（{token!r}）")
        return token

    # 型名
    def is_type_start(self, k: int) -> bool:
        kind, token = self.peek(k)
        if kind != "name":
            return False
        if token in _BASE_WORDS or token in _QUALIFIER_WORDS or token in _TAG_WORDS:
            return True
        if self.is_variable(token):
            return False
        try:
            self.info.type_named(token)
            return True
        except KeyError:
            return False

    def is_variable(self, name: str) -> bool:
        try:
            self.info.lookup(name, self.frame.pc if self.frame else None)
            return True
        except AmbiguousNameError:                                  # 変数ではある（どれかは決まらない）
            return True
        except KeyError:
            return False

    def type_name(self) -> CType:
        qualifiers: list[str] = []
        words: list[str] = []
        base: CType | None = None
        while self.peek()[0] == "name":
            token = self.peek()[1]
            if token in _QUALIFIER_WORDS:
                qualifiers.append(self.take()[1])
            elif token in _TAG_WORDS and base is None and not words:
                self.take()
                tag = f"{token} {self.name()}"
                base = self._named_type(tag)
            elif token in _BASE_WORDS and base is None:
                words.append(self.take()[1])
            elif base is None and not words:
                base = self._named_type(self.take()[1])
            else:
                break
        if base is None:
            if not words:
                raise ExpressionError(f"型名が要る（{self._where()}）")
            base = self._base_type(words)
        t: CType = base
        for q in qualifiers:
            t = QualifiedType(q, t)
        while self.accept("*"):
            t = PointerType(t, self.info.address_size)
            while self.peek()[0] == "name" and self.peek()[1] in _QUALIFIER_WORDS:
                t = QualifiedType(self.take()[1], t)
        return t



def _no_memory(address: int, length: int) -> bytes:
    raise AccessError("ターゲットのメモリの読み手が無い")


_no_register = no_register("フレームが無い（レジスタが無い）")
