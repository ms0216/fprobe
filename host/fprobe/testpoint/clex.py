"""C の字句解析（テストポイントの挿入用）。元のソースの文字の位置と行を保ったまま字句に分ける。

コメント・空白・行の継続は字句にしない（位置で元のソースに戻れる）。前処理の指令は論理行（継続・中のコメントを
含む）で 1 つの字句（kind = "pp"）にし、指令の名前（`directive`）と条件の指令か（`conditional`）を持たせる。
仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 2.1 節。
"""

from dataclasses import dataclass

__all__ = ["CLexError", "Token", "tokenize"]

CONDITIONALS = frozenset({"if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "else", "endif"})

_PUNCTUATORS = sorted(
    ["...", "<<=", ">>=", "->", "++", "--", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "*=", "/=", "%=", "+=",
     "-=", "&=", "^=", "|=", "##", "::", "[", "]", "(", ")", "{", "}", ".", "&", "*", "+", "-", "~", "!", "/", "%",
     "<", ">", "^", "|", "?", ":", ";", "=", ",", "#"], key=len, reverse=True)
_STRING_PREFIXES = ("u8", "L", "u", "U")


class CLexError(ValueError):
    def __init__(self, message: str, line: int):
        super().__init__(f"{line} 行: {message}")
        self.line = line


@dataclass(frozen=True)
class Token:
    kind: str            # "id" / "num" / "str" / "chr" / "punct" / "pp"
    text: str
    start: int           # 元のソースの文字の位置 [start, end)
    end: int
    line: int            # 先頭の行（1 から）
    directive: str = ""  # kind = "pp" のとき指令の名前（"if"・"define" など。空の指令は ""）

    @property
    def conditional(self) -> bool:
        return self.kind == "pp" and self.directive in CONDITIONALS


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c in "_$" or ord(c) > 127


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c in "_$" or ord(c) > 127


class _Lexer:
    def __init__(self, src: str):
        self.s = src
        self.n = len(src)
        self.i = 0
        self.line = 1
        self.out: list[Token] = []

    def _advance_to(self, j: int) -> None:
        self.line += self.s.count("\n", self.i, j)
        self.i = j

    def _skip_block_comment(self, j: int) -> int:
        """j は "/*" の位置。終わりの "*/" の次を返す。"""
        end = self.s.find("*/", j + 2)
        if end < 0:
            raise CLexError("閉じていないコメント", self.line + self.s.count("\n", self.i, j))
        return end + 2

    def _skip_line_comment(self, j: int) -> int:
        """j は "//" の位置。改行の位置（改行は含まない）を返す。行末の \\ で次の行に続く。"""
        while True:
            nl = self.s.find("\n", j)
            if nl < 0:
                return self.n
            k = nl - 1
            if k >= 0 and self.s[k] == "\r":
                k -= 1
            if k >= 0 and self.s[k] == "\\":
                j = nl + 1
                continue
            return nl

    def _quoted_end(self, j: int, quote: str, *, strict: bool) -> int | None:
        """j は開きの引用符。閉じの次の位置。改行までに閉じなければ strict なら例外、そうでなければ None。"""
        k = j + 1
        while k < self.n:
            c = self.s[k]
            if c == "\\":
                k += 2
                continue
            if c == quote:
                return k + 1
            if c == "\n":
                break
            k += 1
        if strict:
            raise CLexError("閉じていない文字列・文字", self.line + self.s.count("\n", self.i, j))
        return None

    def _line_start(self) -> bool:
        """今の位置より前の、同じ物理行（継続でつながった行を含む）に空白・コメント以外が無いか。"""
        return not self.out or self.out[-1].line < self.line and self._only_space_since_newline()

    def _only_space_since_newline(self) -> bool:
        k = self.i - 1
        while k >= 0 and self.s[k] in " \t\f\v\r":
            k -= 1
        return k < 0 or self.s[k] == "\n" or self._after_comment_on_new_line(k)

    def _after_comment_on_new_line(self, k: int) -> bool:
        # 行頭のコメントの後の # も指令（/* c */ #define X）。コメントの直前までさかのぼって確かめる
        if k >= 1 and self.s[k - 1:k + 1] == "*/":
            begin = self.s.rfind("/*", 0, k - 1)
            if begin >= 0:
                j = begin - 1
                while j >= 0 and self.s[j] in " \t\f\v\r":
                    j -= 1
                return j < 0 or self.s[j] == "\n"
        return False

    def _directive(self) -> None:
        start, line = self.i, self.line
        j = self.i + 1
        while j < self.n:
            c = self.s[j]
            if c == "\n":
                k = j - 1
                if k >= 0 and self.s[k] == "\r":
                    k -= 1
                if self.s[k] == "\\":
                    j += 1
                    continue
                break
            if self.s.startswith("/*", j):
                j = self._skip_block_comment(j)
                continue
            if self.s.startswith("//", j):
                j = self._skip_line_comment(j)
                continue
            if c in "\"'":
                end = self._quoted_end(j, c, strict=False)
                j = end if end is not None else j + 1
                continue
            j += 1
        text = self.s[start:j]
        k = 1
        while k < len(text) and (text[k] in " \t" or text.startswith("\\\n", k) or text.startswith("\\\r\n", k)):
            k += 1 if text[k] in " \t" else (2 if text[k + 1] == "\n" else 3)
        m = k
        while m < len(text) and _is_ident_char(text[m]):
            m += 1
        self.out.append(Token("pp", text.rstrip("\r"), start, start + len(text.rstrip("\r")), line, text[k:m]))
        self._advance_to(j)

    def run(self) -> list[Token]:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c in " \t\f\v\r\n":
                self._advance_to(self.i + 1)
                continue
            if c == "\\" and (s.startswith("\\\n", self.i) or s.startswith("\\\r\n", self.i)):
                self._advance_to(self.i + (2 if s[self.i + 1] == "\n" else 3))
                continue
            if s.startswith("/*", self.i):
                self._advance_to(self._skip_block_comment(self.i))
                continue
            if s.startswith("//", self.i):
                self._advance_to(self._skip_line_comment(self.i))
                continue
            if c == "#" and self._line_start():
                self._directive()
                continue
            start = self.i
            if _is_ident_start(c):
                j = start + 1
                while j < self.n and _is_ident_char(s[j]):
                    j += 1
                word = s[start:j]
                if word in _STRING_PREFIXES and j < self.n and s[j] in "\"'":
                    end = self._quoted_end(j, s[j], strict=True)
                    self._emit("str" if s[j] == '"' else "chr", start, end)
                else:
                    self._emit("id", start, j)
                continue
            if c.isdigit() or (c == "." and self.i + 1 < self.n and s[self.i + 1].isdigit()):
                j = start + 1
                while j < self.n:
                    d = s[j]
                    if d in "eEpP" and j + 1 < self.n and s[j + 1] in "+-":
                        j += 2
                    elif _is_ident_char(d) or d == ".":
                        j += 1
                    elif d == "'" and j + 1 < self.n and s[j + 1].isalnum():      # C23 の桁区切り
                        j += 1
                    else:
                        break
                self._emit("num", start, j)
                continue
            if c in "\"'":
                self._emit("str" if c == '"' else "chr", start, self._quoted_end(start, c, strict=True))
                continue
            for p in _PUNCTUATORS:
                if s.startswith(p, start):
                    self._emit("punct", start, start + len(p))
                    break
            else:
                self._emit("punct", start, start + 1)          # @ ` など（C では来ないが止まらない）
        return self.out

    def _emit(self, kind: str, start: int, end: int) -> None:
        self.out.append(Token(kind, self.s[start:end], start, end, self.line))
        self._advance_to(end)


def tokenize(src: str) -> list[Token]:
    """ソース全体を字句に分ける。閉じていないコメント・文字列は CLexError。"""
    return _Lexer(src).run()
