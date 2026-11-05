"""defmt の書式文字列の構文（`{0=u8:x}` 等の引数・ヒント）と、表の 1 項目（DefmtEntry）。

解読の本体は fprobe.logfmt.defmt（表と流れ）、引数の読みと表示は fprobe.logfmt.defmt_value。ここは ELF にもバイト列にも依らない
書式の文字列だけの部品（単独で試験できる）。fprobe.logfmt.defmt が再公開する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LOG_TAGS = ("trace", "debug", "info", "warn", "error", "println")
VERSIONS = ("3", "4")
_INTS = {f"{s}{b}": (b, s == "i") for s in "ui" for b in (8, 16, 32, 64, 128)}
# usize・isize はターゲットの大きさによらず 4 バイトで送る（ワイヤ形式。decoder/src/decoder.rs の Type::Usize = read_u32）
_INTS.update(usize=(32, False), isize=(32, True))
_HINT_KINDS = ("x", "X", "b", "o", "a", "?", "cbor", "us", "ms", "tus", "tms", "ts", "iso8601ms", "iso8601s")


class DefmtError(Exception):
    """かたまりが表・形式に合わない（Malformed）。"""


class _Incomplete(DefmtError):
    """かたまりが途中で終わった（raw の流れでは続きを待つ）。"""


# --- 書式 ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Hint:
    kind: str | None = None             # x X b o a ? cbor us ms tus tms ts iso8601ms iso8601s bitflags unknown
    alternate: bool = False             # '#'
    zero_pad: int | None = None         # '0N'
    bitflags: str | None = None         # __internal_bitflags_NAME@… の NAME


@dataclass(frozen=True)
class Param:
    index: int
    type: str                           # u8…i128 usize isize f32 f64 bool char str istr format [?] [u8]
                                        # [u8;N] [?;N] bits
    hint: Hint | None = None            # __internal_Debug __internal_Display __internal_FormatSequence
    bits: tuple[int, int] | None = None
    length: int | None = None           # [u8;N] / [?;N] の N


_PARAM = re.compile(r"\{(\d+)?(?:=([^:}]*))?(?::([^}]*))?\}")


def _hint(text: str | None) -> Hint | None:
    if text is None:
        return None
    alternate = text.startswith("#")
    text = text[1:] if alternate else text
    zero_pad = None
    if m := re.match(r"0(\d+)", text):
        zero_pad, text = int(m[1]), text[m.end():]
    if text.startswith("__internal_bitflags_"):
        return Hint("bitflags", alternate, zero_pad, text[len("__internal_bitflags_"):].split("@")[0])
    if text == "":
        return Hint(None, alternate, zero_pad)
    return Hint(text if text in _HINT_KINDS else "unknown", alternate, zero_pad)


def parse_format(fmt: str) -> list[str | Param]:
    """書式 → 文字と Param の並び。`{{` `}}` は文字の { }。番号の無い Param には出てきた順に 0, 1, … を振る。"""
    out: list[str | Param] = []
    counter = 0
    i = 0
    text: list[str] = []
    while i < len(fmt):
        c = fmt[i]
        if c in "{}" and fmt[i:i + 2] in ("{{", "}}"):
            text.append(c)
            i += 2
            continue
        if c == "{":
            m = _PARAM.match(fmt, i)
            if m is None:
                raise DefmtError(f"書式の誤り: {fmt!r}")
            if text:
                out.append("".join(text))
                text = []
            explicit, kind, hint = m[1], (m[2] or "").replace(" ", ""), m[3]
            if explicit is not None:
                index = int(explicit)
            else:
                index, counter = counter, counter + 1
            out.append(_param(index, kind, _hint(hint)))
            i = m.end()
            continue
        text.append(c)
        i += 1
    if text:
        out.append("".join(text))
    return out


def _param(index: int, kind: str, hint: Hint | None) -> Param:
    if kind in ("", "?"):
        return Param(index, "format", hint)
    if m := re.fullmatch(r"(\d+)\.\.(\d+)", kind):
        start, end = int(m[1]), int(m[2])
        if not (start < end <= 128):
            raise DefmtError(f"ビットの欄の範囲の誤り: {kind}")
        return Param(index, "bits", hint, bits=(start, end))
    if m := re.fullmatch(r"\[(u8|\?);(\d+)\]", kind):
        return Param(index, f"[{m[1]};{m[2]}]", hint, length=int(m[2]))
    if kind in _INTS or kind in ("f32", "f64", "bool", "char", "str", "istr", "[?]", "[u8]", "__internal_Debug",
                                 "__internal_Display", "__internal_FormatSequence"):
        return Param(index, kind, hint)
    raise DefmtError(f"知らない型: {kind}")


# --- 表 -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DefmtEntry:
    tag: str                            # info / derived / prim / str / write / … （defmt_ を除いたもの）
    data: str                           # 書式
    location: dict | None = None        # file・line・module
    package: str | None = None
    crate: str | None = None

    @property
    def fragments(self) -> list[str | Param]:
        cached = _FRAGMENTS.get(self.data)
        if cached is None:
            cached = _FRAGMENTS[self.data] = parse_format(self.data)
        return cached


_FRAGMENTS: dict[str, list[str | Param]] = {}
