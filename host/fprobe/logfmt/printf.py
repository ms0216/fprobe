"""ホストでの C の printf の整形。ターゲットが書式の番号（番地・ID）と引数の生の値だけを送るログ（Compact logging・
Trice・SystemView の Print・Event Recorder 等）の文字列を、ホストで組み立てる。

引数の出どころは 2 通り:
- `Values`: 型の付いた Python の値の列（解読した側が型を知っているとき）。
- `Words`: 32 ビットの語の列（ターゲットが語で送るとき）。`ll` の整数と double は 2 語（下位が先）、`hh` / `h` は
  語の下位を符号つき・なしで読む。`%s` は語を番地として `string(番地)` で引く（ELF・メモリ）。

long・size_t・ptrdiff_t・ポインタ（`l`・`z`・`t` の整数、`%p`・`%s`・`%n`）の大きさはターゲットの ABI で違う
（ILP32 = 32 ビット、LP64 = 64 ビット）ので、使う側が pointer_bits で渡す（既定を持たない）。int は 32 ビット、
long long・intmax_t は 64 ビット（どちらの ABI も同じ）。

書式の誤り（`%` で終わる・知らない変換）は文字のまま残し、引数が足りないときは `<?>` を置く（ログの解読で例外に
しない。原因の分からない 1 行のために残りのログを失わないため）。
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = ["Conversion", "Values", "Words", "conversions", "format_c"]

MISSING = "<?>"
_SPEC = re.compile(r"%(?P<flags>[-+ #0]*)(?P<width>\*|\d+)?(?:\.(?P<prec>\*|\d*))?"
                   r"(?P<length>hh|h|ll|l|j|z|t|L|q)?(?P<conv>[diouxXeEfFgGaAcspn%])")
_INT_BITS = {"hh": 8, "h": 16, "": 32, "j": 64, "ll": 64, "q": 64}
_POINTER_SIZED = ("l", "z", "t")                    # long・size_t・ptrdiff_t: ポインタと同じ大きさ（ILP32・LP64）


def _int_bits(length: str, pointer_bits: int) -> int:
    return pointer_bits if length in _POINTER_SIZED else _INT_BITS.get(length, 32)


def _check(pointer_bits: int) -> None:
    if pointer_bits not in (32, 64):
        raise ValueError(f"pointer_bits は 32（ILP32）か 64（LP64）: {pointer_bits}")


@dataclass(frozen=True)
class Conversion:
    """書式の中の 1 つの引数（`*` の幅・精度も 1 つの引数）。bits はターゲットでの大きさ（pointer_bits で決まる）。"""
    conversion: str            # d i o u x X e f g a c s p n、または "*"
    length: str
    bits: int


def conversions(fmt: str, *, pointer_bits: int) -> list[Conversion]:
    """書式が取る引数の並び（`%%` は除く）。pointer_bits はターゲットのポインタ・long の大きさ（32 / 64）。"""
    _check(pointer_bits)
    out = []
    for m in _SPEC.finditer(fmt):
        conv, length = m["conv"], m["length"] or ""
        if conv == "%":
            continue
        if m["width"] == "*":
            out.append(Conversion("*", "", 32))
        if m["prec"] == "*":
            out.append(Conversion("*", "", 32))
        if conv in "eEfFgGaA":
            bits = 64
        elif conv in "spn":
            bits = pointer_bits
        else:
            bits = _int_bits(length, pointer_bits)
        out.append(Conversion(conv, length, bits))
    return out


class Values:
    """型の付いた値の列。"""

    def __init__(self, values: Sequence):
        self._values = list(values)
        self._i = 0

    def _next(self):
        if self._i >= len(self._values):
            raise IndexError
        self._i += 1
        return self._values[self._i - 1]

    def integer(self, bits: int, signed: bool) -> int:
        v = self._next()
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            return ord(v[0]) if v else 0
        return _wrap(int(v), bits, signed)

    def real(self, bits: int) -> float:
        return float(self._next())

    def text(self, pointer_bits: int) -> str:
        v = self._next()
        return v if isinstance(v, str) else str(v)


class Words:
    """32 ビットの語の列。double_words は double の語数（既定 2。float を 1 語で送る形式は 1）。"""

    def __init__(self, words: Sequence[int], string: Callable[[int], str | None] | None = None, double_words: int = 2):
        self._words = [w & 0xFFFFFFFF for w in words]
        self._i = 0
        self._string = string
        self._double_words = double_words

    def _take(self, n: int) -> list[int]:
        if self._i + n > len(self._words):
            self._i = len(self._words)
            raise IndexError
        self._i += n
        return self._words[self._i - n:self._i]

    def integer(self, bits: int, signed: bool) -> int:
        words = self._take(2 if bits > 32 else 1)
        value = words[0] | (words[1] << 32 if len(words) > 1 else 0)
        return _wrap(value, bits, signed)

    def real(self, bits: int) -> float:
        if self._double_words == 1:
            return struct.unpack("<f", struct.pack("<I", self._take(1)[0]))[0]
        lo, hi = self._take(2)
        return struct.unpack("<d", struct.pack("<II", lo, hi))[0]

    def text(self, pointer_bits: int) -> str:
        address = self.integer(pointer_bits, False)
        found = self._string(address) if self._string else None
        return found if found is not None else f"<str@0x{address:0{pointer_bits // 4}X}>"


def _wrap(value: int, bits: int, signed: bool) -> int:
    value &= (1 << bits) - 1
    if signed and value >> (bits - 1):
        value -= 1 << bits
    return value


def format_c(fmt: str, args: Values | Words, *, pointer_bits: int) -> str:
    """C の printf と同じ規則で整形する。pointer_bits はターゲットのポインタ・long の大きさ（32 / 64。既定なし）。"""
    _check(pointer_bits)
    out, pos = [], 0
    while (i := fmt.find("%", pos)) >= 0:
        out.append(fmt[pos:i])
        m = _SPEC.match(fmt, i)
        if m is None:                                           # 書式の誤り: 文字のまま
            out.append(fmt[i:i + 1])
            pos = i + 1
            continue
        pos = m.end()
        try:
            out.append(_one(m, args, pointer_bits))
        except IndexError:
            out.append(MISSING)
    out.append(fmt[pos:])
    return "".join(out)


def _one(m: re.Match, args: Values | Words, pointer_bits: int) -> str:
    conv, flags, length = m["conv"], m["flags"], m["length"] or ""
    if conv == "%":
        return "%"
    width = m["width"]
    if width == "*":
        w = args.integer(32, True)
        if w < 0:
            flags, w = flags + "-", -w
        width = w
    else:
        width = int(width) if width else 0
    prec = m["prec"]
    if prec == "*":
        p = args.integer(32, True)
        prec = p if p >= 0 else None
    elif prec is not None:
        prec = int(prec or 0)
    if conv in "di":
        body, sign = _integer(args.integer(_int_bits(length, pointer_bits), True), conv, flags, prec)
    elif conv in "ouxX":
        body, sign = _integer(args.integer(_int_bits(length, pointer_bits), False), conv, flags, prec)
    elif conv in "eEfFgGaA":
        body, sign = _real(args.real(64), conv, flags, prec)
    elif conv == "c":
        body, sign = chr(args.integer(32, False) & 0xFF if length != "l" else args.integer(32, False)), ""
    elif conv == "s":
        text = args.text(pointer_bits)
        body, sign = (text if prec is None else text[:prec]), ""
    elif conv == "p":
        body, sign = f"0x{args.integer(pointer_bits, False):0{pointer_bits // 4}x}", ""
    else:                                                       # %n: 書き込み先の番地を消費するだけ
        args.integer(pointer_bits, False)
        return ""
    return _pad(sign, body, width, flags, conv)


def _integer(value: int, conv: str, flags: str, prec: int | None) -> tuple[str, str]:
    sign = "-" if value < 0 else "+" if "+" in flags else " " if " " in flags else ""
    magnitude = abs(value)
    digits = {"d": str, "i": str, "u": str, "o": lambda v: format(v, "o"), "x": lambda v: format(v, "x"),
              "X": lambda v: format(v, "X")}[conv](magnitude)
    if prec is not None:
        digits = "" if prec == 0 and magnitude == 0 else digits.rjust(prec, "0")
    if "#" in flags:
        if conv == "o" and not digits.startswith("0"):
            digits = "0" + digits
        elif conv in "xX" and magnitude:
            sign += "0x" if conv == "x" else "0X"
    return digits, sign


def _real(value: float, conv: str, flags: str, prec: int | None) -> tuple[str, str]:
    negative = math.copysign(1.0, value) < 0
    sign = "-" if negative else "+" if "+" in flags else " " if " " in flags else ""
    magnitude = abs(value)
    if math.isinf(magnitude) or math.isnan(magnitude):
        body = "inf" if math.isinf(magnitude) else "nan"
        return (body.upper() if conv.isupper() else body), sign
    if conv in "aA":
        body = _hex_float(magnitude, prec)
        return (body.upper() if conv == "A" else body), sign
    p = 6 if prec is None else prec
    alt = "#" if "#" in flags else ""
    return format(magnitude, f"{alt}.{p}{conv}"), sign


def _hex_float(value: float, prec: int | None) -> str:
    """C の %a（正規化した 1.xxxp±e、末尾の 0 を落とす）。"""
    if value == 0:
        return "0x0p+0"
    text = value.hex()                                          # 0x1.8000000000000p+1
    mantissa, exponent = text[2:].split("p")
    head, _, frac = mantissa.partition(".")
    frac = frac.rstrip("0") if prec is None else frac[:prec].ljust(prec, "0")
    return f"0x{head}{'.' + frac if frac else ''}p{int(exponent):+d}"


def _pad(sign: str, body: str, width: int, flags: str, conv: str) -> str:
    total = len(sign) + len(body)
    if total >= width:
        return sign + body
    if "-" in flags:
        return sign + body + " " * (width - total)
    if "0" in flags and conv not in "csp":
        return sign + "0" * (width - total) + body
    return " " * (width - total) + sign + body
