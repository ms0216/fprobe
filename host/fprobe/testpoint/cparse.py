"""文の水準の C の構文解析（テストポイントの挿入用）。

ファイルの最上位から関数の定義を探し、その本体を文の木（複文・if・switch・ループ・return・ラベル・単純文・
マクロの塊）にする。式の中は括弧の釣り合いだけを見て飛ばす。節点は字句の番号（`first`〜`last`、両端を含む）を持ち、
挿入はその位置に文字列を差し込む。
仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 2.2・2.3 節。
"""

from dataclasses import dataclass, field

from .clex import Token

__all__ = ["CParseError", "Compound", "Function", "If", "Label", "Loop", "Node", "Opaque", "ParsedFile", "Return",
           "Simple", "Skipped", "Switch", "Unsupported", "parse_file"]

KEYWORDS = frozenset("""
auto break case char const continue default do double else enum extern float for goto if inline int long register
restrict return short signed sizeof static struct switch typedef union unsigned void volatile while _Bool _Complex
_Imaginary _Alignas _Alignof _Atomic _Generic _Noreturn _Static_assert _Thread_local bool true false nullptr alignas
alignof static_assert thread_local typeof typeof_unqual constexpr asm __asm __asm__ __attribute __attribute__
__extension__ __typeof __typeof__ __volatile__ __inline __inline__ __restrict __restrict__ __label__ __auto_type
_Pragma __declspec __builtin_va_arg __builtin_offsetof
""".split())
_SPECIFIERS = frozenset("""static extern inline __inline __inline__ _Noreturn noreturn __forceinline register constexpr
thread_local _Thread_local __extension__""".split())
_ATTRIBUTE_WORDS = frozenset({"__attribute__", "__attribute", "__declspec", "__asm__", "__asm", "asm"})
_OPENERS = {"(": ")", "[": "]", "{": "}"}


class CParseError(ValueError):
    """ファイル全体の構造が読めない（括弧が合わない等）。写しは元のままにする。"""


class Unsupported(Exception):
    """この関数には安全に挿入できない（理由を報告して残す）。"""

    def __init__(self, reason: str, line: int):
        super().__init__(f"{line} 行: {reason}")
        self.reason = reason
        self.line = line


# --- 節点 --------------------------------------------------------------------------------

@dataclass
class Node:
    first: int
    last: int


@dataclass
class Compound(Node):
    items: list[Node] = field(default_factory=list)

    @property
    def open(self) -> int:
        return self.first

    @property
    def close(self) -> int:
        return self.last


@dataclass
class If(Node):
    cond_close: int = 0
    then: Node | None = None
    else_kw: int | None = None
    els: Node | None = None


@dataclass
class Label(Node):
    kind: str = "label"           # "case" / "default" / "label"
    colon: int = 0
    stmt: Node | None = None      # C23 では } の直前のラベルに文が無くてよい


@dataclass
class Switch(Node):
    cond_close: int = 0
    body: Node | None = None
    labels: list[Label] = field(default_factory=list)

    @property
    def has_default(self) -> bool:
        return any(lab.kind == "default" for lab in self.labels)


@dataclass
class Loop(Node):
    kind: str = "while"           # "while" / "for" / "do"
    body: Node | None = None


@dataclass
class Return(Node):
    has_expr: bool = False        # last は ; の番号


@dataclass
class Simple(Node):
    no_semicolon: bool = False    # } の直前で ; が無い（セミコロン無しのマクロ）


@dataclass
class Opaque(Node):
    body: Compound | None = None  # FOREACH(x) { … } / LOOP { … } の本体


@dataclass
class Function:
    name: str
    first: int                    # 宣言の最初の字句
    name_token: int
    body: Compound
    return_type: str | None       # None = 複雑な宣言子で取れない
    attributes: frozenset[str]
    line: int

    @property
    def is_void(self) -> bool:
        return self.return_type == "void"


@dataclass(frozen=True)
class Skipped:
    name: str
    line: int
    reason: str


@dataclass
class ParsedFile:
    functions: list[Function]
    skipped: list[Skipped]
    match: dict[int, int]             # 括弧の組（開き ↔ 閉じの字句の番号）


# --- 解析 -----------------------------------------------------------------------------------

def _match_brackets(tokens: list[Token]) -> dict[int, int]:
    match: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(tokens):
        if t.kind != "punct":
            continue
        if t.text in _OPENERS:
            stack.append(i)
        elif t.text in (")", "]", "}"):
            if not stack or _OPENERS[tokens[stack[-1]].text] != t.text:
                raise CParseError(f"{t.line} 行: 括弧が合わない（{t.text!r}）")
            j = stack.pop()
            match[j] = i
            match[i] = j
    if stack:
        raise CParseError(f"{tokens[stack[-1]].line} 行: 閉じていない {tokens[stack[-1]].text!r}")
    return match


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.t = tokens
        self.n = len(tokens)
        self.match = _match_brackets(tokens)
        self.switches: list[list[Label]] = []
        self.cond: list[int] = []         # 開いている #if 群が始まった複文の開き括弧

    # --- 字句の移動 ---

    def text(self, i: int) -> str:
        return self.t[i].text if i < self.n else ""

    def is_punct(self, i: int, text: str) -> bool:
        return i < self.n and self.t[i].kind == "punct" and self.t[i].text == text

    def next_tok(self, i: int) -> int:
        """i 以降で最初の前処理でない字句。途中に条件の指令があれば Unsupported。"""
        while i < self.n and self.t[i].kind == "pp":
            if self.t[i].conditional:
                raise Unsupported("前処理の条件（#if 等）が文の途中にある", self.t[i].line)
            i += 1
        if i >= self.n:
            raise Unsupported("ファイルの終わりで文が切れている", self.t[-1].line)
        return i

    def peek(self, i: int) -> int | None:
        """i 以降で最初の前処理でない字句（条件の指令を越えない。越えるなら None）。"""
        while i < self.n and self.t[i].kind == "pp":
            if self.t[i].conditional:
                return None
            i += 1
        return i if i < self.n else None

    def prev_tok(self, i: int) -> int:
        i -= 1
        while self.t[i].kind == "pp":
            i -= 1
        return i

    # --- ファイルの最上位 ---

    def run(self) -> ParsedFile:
        functions: list[Function] = []
        skipped: list[Skipped] = []
        decl: list[int] = []              # 最上位の宣言の並び（括弧の組は開きの番号だけ）
        decl_cond = False
        linkage: set[int] = set()         # extern "C" { の閉じ括弧
        i = 0
        while i < self.n:
            tok = self.t[i]
            if tok.kind == "pp":
                decl_cond |= bool(decl) and tok.conditional
                i += 1
                continue
            x = tok.text if tok.kind == "punct" else None
            if x == ";":
                decl, decl_cond = [], False
                i += 1
            elif x == "{":
                if [self.text(k) for k in decl] == ["extern", '"C"']:
                    linkage.add(self.match[i])
                    decl, decl_cond = [], False
                    i += 1
                    continue
                found = self._function_head(decl)
                if found is None:
                    decl.append(i)
                    i = self.match[i] + 1
                    continue
                name, name_tok, ret, attrs = found
                if decl_cond:
                    skipped.append(Skipped(name, self.t[name_tok].line, "前処理の条件が関数の宣言の中にある"))
                else:
                    self.switches, self.cond = [], []
                    try:
                        body = self.compound(i)
                        if self.cond:
                            raise Unsupported("関数の中で始まった #if 群が関数の外で終わる", self.t[self.cond[-1]].line)
                        functions.append(Function(name, decl[0], name_tok, body, ret, attrs, self.t[name_tok].line))
                    except Unsupported as e:
                        skipped.append(Skipped(name, e.line, e.reason))
                decl, decl_cond = [], False
                i = self.match[i] + 1
            elif x == "}":
                if i not in linkage:
                    raise CParseError(f"{tok.line} 行: 対応の無い }}")
                decl, decl_cond = [], False
                i += 1
            elif x in ("(", "["):
                decl.append(i)
                i = self.match[i] + 1
            else:
                decl.append(i)
                i += 1
        return ParsedFile(functions, skipped, self.match)

    def _strip_trailing_attributes(self, decl: list[int]) -> list[int]:
        d = list(decl)
        while len(d) >= 2 and self.is_punct(d[-1], "(") and self.text(d[-2]) in _ATTRIBUTE_WORDS:
            del d[-2:]
        while len(d) >= 1 and self.is_punct(d[-1], "[") and self.is_punct(d[-1] + 1, "["):
            del d[-1]
        return d

    def _function_head(self, decl: list[int]):
        """宣言の並びが関数の定義の頭なら (名前, 名前の字句, 戻り値の型, 属性)。"""
        d = self._strip_trailing_attributes(decl)
        if not d or not self.is_punct(d[-1], "("):
            return None
        if any(self.is_punct(k, "=") for k in d) or self.text(d[0]) == "typedef":
            return None
        if len(d) < 2:
            return None
        before = d[-2]
        attrs = self._attributes(d)
        if self.t[before].kind == "id" and self.t[before].text not in KEYWORDS:
            return self.t[before].text, before, self._return_type(d[:-2]), attrs
        if self.is_punct(before, "("):                 # (*f(void)) のような複雑な宣言子
            for k in range(before + 1, self.match[before]):
                if self.t[k].kind == "id" and self.t[k].text not in KEYWORDS and self.is_punct(k + 1, "("):
                    return self.t[k].text, k, None, attrs
        return None

    def _attributes(self, d: list[int]) -> frozenset[str]:
        words: set[str] = set()
        for a, b in zip(d, d[1:]):
            if self.text(a) in ("__attribute__", "__attribute", "__declspec") and self.is_punct(b, "("):
                words.update(self.t[k].text for k in range(b, self.match[b]) if self.t[k].kind == "id")
        return frozenset(w.strip("_") for w in words) | frozenset(words)

    def _return_type(self, d: list[int]) -> str:
        parts: list[str] = []
        skip_group = False
        for k in d:
            text = self.text(k)
            if skip_group:
                skip_group = False
                if self.is_punct(k, "("):
                    continue
            if text in _ATTRIBUTE_WORDS:
                skip_group = True
                continue
            if text in _SPECIFIERS:
                continue
            if self.is_punct(k, "[") and self.is_punct(k + 1, "["):      # [[attribute]]
                continue
            if self.t[k].kind == "punct" and text in _OPENERS:
                parts.append(" ".join(tt.text for tt in self.t[k:self.match[k] + 1] if tt.kind != "pp"))
                continue
            if text == "*" and parts and parts[-1].endswith("*"):
                parts[-1] += "*"
            else:
                parts.append(text)
        return " ".join(parts) or "int"

    # --- 文 ---

    def compound(self, open_: int) -> Compound:
        close = self.match[open_]
        items: list[Node] = []
        i = open_ + 1
        while i < close:
            tok = self.t[i]
            if tok.kind == "pp":
                if tok.conditional:
                    self._conditional(tok, open_)
                i += 1
                continue
            s = self.statement(i)
            items.append(s)
            i = s.last + 1
        return Compound(open_, close, items)

    def _conditional(self, tok: Token, open_: int) -> None:
        if tok.directive in ("if", "ifdef", "ifndef"):
            self.cond.append(open_)
            return
        if not self.cond or self.cond[-1] != open_:
            raise Unsupported(f"#{tok.directive} の #if 群が別の複文（括弧の中・外）にまたがる", tok.line)
        if tok.directive == "endif":
            self.cond.pop()

    def statement(self, i: int) -> Node:
        i = self.next_tok(i)
        tok = self.t[i]
        x = tok.text
        if tok.kind == "punct":
            if x == "{":
                return self.compound(i)
            if x == ";":
                return Simple(i, i)
            if x == "}":
                raise Unsupported("文があるべき所に }", tok.line)
            return self.simple(i)
        if tok.kind != "id":
            return self.simple(i)
        if x == "if":
            cc = self._paren(i + 1, x)
            then = self.statement(cc + 1)
            k = self.peek(then.last + 1)
            if k is not None and self.text(k) == "else" and self.t[k].kind == "id":
                els = self.statement(k + 1)
                return If(i, els.last, cc, then, k, els)
            return If(i, then.last, cc, then)
        if x == "switch":
            cc = self._paren(i + 1, x)
            self.switches.append([])
            body = self.statement(cc + 1)
            return Switch(i, body.last, cc, body, self.switches.pop())
        if x in ("while", "for"):
            cc = self._paren(i + 1, x)
            body = self.statement(cc + 1)
            return Loop(i, body.last, x, body)
        if x == "do":
            body = self.statement(i + 1)
            w = self.next_tok(body.last + 1)
            if self.text(w) != "while":
                raise Unsupported("do の後に while が無い", self.t[w].line)
            cc = self._paren(w + 1, "while")
            semi = self.next_tok(cc + 1)
            if not self.is_punct(semi, ";"):
                raise Unsupported("do-while の後に ; が無い", self.t[semi].line)
            return Loop(i, semi, "do", body)
        if x == "return":
            s = self.simple(i)
            if s.no_semicolon:
                raise Unsupported("return の後に ; が無い", tok.line)
            return Return(i, s.last, has_expr=self.next_tok(i + 1) != s.last)
        if x in ("case", "default"):
            if not self.switches:
                raise Unsupported(f"switch の外の {x}", tok.line)
            colon = self._label_colon(i)
            label = Label(i, colon, x, colon)
            self.switches[-1].append(label)
            return self._label_body(label)
        if x == "else":
            raise Unsupported("対応する if の無い else", tok.line)
        if x not in KEYWORDS:
            j = self.peek(i + 1)
            if j is not None and self.is_punct(j, ":") and not self.is_punct(j + 1, ":"):
                return self._label_body(Label(i, j, "label", j))
            if j is not None and self.is_punct(j, "("):
                j = self.peek(self.match[j] + 1)
            if j is not None and self.is_punct(j, "{"):
                body = self.compound(j)
                return Opaque(i, body.close, body)
        return self.simple(i)

    def _label_body(self, label: Label) -> Label:
        k = self.peek(label.colon + 1)
        if k is not None and self.is_punct(k, "}"):
            return label
        label.stmt = self.statement(label.colon + 1)
        label.last = label.stmt.last
        return label

    def _paren(self, i: int, keyword: str) -> int:
        j = self.next_tok(i)
        if not self.is_punct(j, "("):
            raise Unsupported(f"{keyword} の後に ( が無い", self.t[j].line)
        return self.match[j]

    def _label_colon(self, i: int) -> int:
        questions = 0
        j = i + 1
        while True:
            j = self.next_tok(j)
            tok = self.t[j]
            if tok.kind == "punct":
                if tok.text in _OPENERS:
                    j = self.match[j] + 1
                    continue
                if tok.text == "?":
                    questions += 1
                elif tok.text == ":":
                    if questions == 0:
                        return j
                    questions -= 1
                elif tok.text in (";", "}", ")", "]"):
                    raise Unsupported("case のラベルに : が無い", tok.line)
            j += 1

    def simple(self, i: int) -> Simple:
        """; まで（括弧の組は飛ばす）。} に当たったらセミコロン無しの文として、その手前で終える。"""
        j = i
        while True:
            j = self.next_tok(j)
            tok = self.t[j]
            if tok.kind == "punct":
                if tok.text == ";":
                    return Simple(i, j)
                if tok.text in _OPENERS:
                    j = self.match[j] + 1
                    continue
                if tok.text in (")", "]", "}"):
                    if j == i:
                        raise Unsupported(f"文があるべき所に {tok.text}", tok.line)
                    return Simple(i, self.prev_tok(j), no_semicolon=True)
            j += 1


def parse_file(tokens: list[Token]) -> ParsedFile:
    """関数の定義を探して本体を文の木にする。挿入できない関数は skipped に理由と共に入れる。
    ファイル全体の括弧が合わないときは CParseError。"""
    return _Parser(tokens).run()
