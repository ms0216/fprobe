"""VCD の行の一括描画（numpy）: 変化点のブロック（_VcdBlocks）を時刻順に並べ、VCD の行のバイト列にする（_render）。

変化点ごとの Python のループを回さずに、10 進の時刻・ビットの値・識別子を numpy の配列演算で文字にする。numpy が無いときは
使わない（la_vcd の vcd_blocks 等が同じバイト列を変化点ごとに作る）。ファイルへの書き出し・スレッドは fprobe.la_vcd。
"""

from collections.abc import Sequence

from .la_wave import _np

def _groups(shifts: Sequence[float], channels: int) -> list[tuple[float, int]]:
    """同じずれの ch の組: (ずれ, ch のビットマスク) のずれの昇順（vcd_blocks_skewed の組と同じ並び）。"""
    groups: dict[float, int] = {}
    for ch in range(channels):
        shift = shifts[ch] if ch < len(shifts) else 0.0
        groups[shift] = groups.get(shift, 0) | 1 << ch
    return sorted(groups.items())


def _ident_table(idents: Sequence[str], width: int):
    """ビット → 識別子（1 文字。_vcd_header の chr(33 + ch)）のバイトの表（width ビット分。使わないビットは 0）。"""
    raw = "".join(idents).encode("ascii")
    if len(raw) != len(idents) or len(raw) > width:
        raise ValueError("一括の書き出しの識別子は 1 文字で、ビットの幅までの数")
    table = _np.zeros(width, dtype=_np.uint8)
    table[:len(raw)] = _np.frombuffer(raw, dtype=_np.uint8)
    return table


class _VcdBlocks:
    """VCD に書くブロック（同じ位置の行のかたまり）を numpy の列で集め、時刻順に並べてバイト列にする（_render）。

    ブロック = (位置 t: サンプルの単位, 順の鍵 batch・sub, 値 v, 行を出すビット m, x: 行を x にするか)。位置の昇順、同じ位置は
    (batch, sub, 入れた順)。ビット b の行の識別子は idents[b]、行はビットの昇順。"""

    def __init__(self, bits: int, idents: Sequence[str]):
        self.dtype = next(d for d in (_np.uint8, _np.uint16, _np.uint32, _np.uint64) if bits <= 8 * d().itemsize)
        self.table = _ident_table(idents, 8 * self.dtype().itemsize)
        self._parts: list[tuple] = []

    def changes(self, values: Sequence[int], previous: int | None, channels: int):
        """(値: ch の外のビットを落とす, 変わったビット)。previous が None なら先頭は全 ch が変わったとする。"""
        v = _np.asarray(values)
        if v.dtype.kind not in "ui":
            v = v.astype(_np.uint64)
        v = v.astype(self.dtype)
        full = (1 << channels) - 1
        if channels < 8 * self.dtype().itemsize:
            v &= self.dtype(full)
        changed = _np.empty_like(v)
        if len(v):
            changed[0] = full if previous is None else (int(v[0]) ^ (previous & full))
            _np.bitwise_xor(v[1:], v[:-1], out=changed[1:])
        return v, changed

    def add_changes(self, starts, values, changed, shift: float, batch: int, sub: int) -> None:
        """変わったビットのある変化点を、位置 starts + shift のブロックとして足す。"""
        keep = _np.flatnonzero(changed)
        if len(keep):
            t = starts[keep].astype(_np.float64)
            if shift:
                t += shift
            self.add(t, values[keep], changed[keep], None, batch, sub)

    def add(self, t, v, m, x, batch: int, sub) -> None:
        n = len(t)
        self._parts.append((t, _np.full(n, batch, dtype=_np.int64), _np.broadcast_to(_np.asarray(sub, dtype=_np.int64), n),
                            v, m, _np.zeros(n, dtype=bool) if x is None else x))

    def take(self, limit: float | None = None):
        """位置が limit より前（None = 全部）のブロックを時刻順に取り出す（残りは持っておく）: (t, v, m, x)。"""
        if not self._parts:
            return None
        t, batch, sub, v, m, x = (_np.concatenate(f) for f in zip(*self._parts))
        if limit is None:
            take, self._parts = _np.arange(len(t)), []
        else:
            early = t < limit
            take, rest = _np.flatnonzero(early), _np.flatnonzero(~early)
            self._parts = [tuple(f[rest] for f in (t, batch, sub, v, m, x))] if len(rest) else []
        if not len(take):
            return None
        order = take[_np.lexsort((sub[take], batch[take], t[take]))]            # 安定: 同じ鍵は入れた順
        return t[order], v[order], m[order], x[order]


_POW10 = None


def _digits(a):
    """非負の整数（uint64）の 10 進の桁数。"""
    global _POW10
    if _POW10 is None:
        _POW10 = _np.array([10 ** k for k in range(1, 20)], dtype=_np.uint64)
    return _np.searchsorted(_POW10, a, side="right") + 1


def _bit_columns(words, bits: Sequence[int]):
    """符号なし整数の列の、ビット bits の値の行列（uint8 の 0・1。形 = (列の長さ, len(bits))）。"""
    n = len(words)
    le = words.astype(words.dtype.newbyteorder("<"), copy=False)
    return _np.unpackbits(le.view(_np.uint8).reshape(n, -1), axis=1, bitorder="little")[:, bits]


_TAB4 = None
_UNIFORM_MIN = 64              # 同じ形のブロックがこれ以上続けば、文字の表をそのまま並べる（印で拾わない）。速さの選び方で、出力は同じ


def _put_digits(chars, col: int, width: int, magnitude) -> None:
    """chars の列 col〜col + width − 1 に、magnitude（uint64）の 10 進を右詰めで width 桁（上は 0 で埋める）。
    8 桁ずつ: uint32 で 4 桁の表を 2 回引き、8 文字を 1 つの uint64 として列に写す。"""
    global _TAB4
    if _TAB4 is None:
        _TAB4 = _np.array([f"{k:04d}" for k in range(10000)], dtype="S4").view(_np.uint32)
    n, rest, j = len(magnitude), magnitude, width
    while j > 0:
        if j > 8:
            q = rest // _np.uint64(100_000_000)
            low = (rest - q * _np.uint64(100_000_000)).astype(_np.uint32)
            rest = q
        else:
            low = rest.astype(_np.uint32)
        high4 = low // _np.uint32(10000)
        low4 = low - high4 * _np.uint32(10000)
        word = _TAB4[low4].astype(_np.uint64) << _np.uint64(32)    # リトルエンディアン: 前の 4 文字が下位
        word |= _TAB4[high4]
        w = min(8, j)
        chars[:, col + j - w:col + j] = word.view(_np.uint8).reshape(n, 8)[:, 8 - w:]
        j -= w


def _put_lines(chars, col: int, v, bits: list[int], x, table) -> None:
    """chars の列 col から、ビット bits ごとに "値 識別子 改行" の 3 文字（x の行は値の代わりに x）。"""
    n = len(v)
    body = chars[:, col:].reshape(n, len(bits), 3)
    value = _bit_columns(v, bits)
    value += ord("0")
    if x is not None:
        value[x] = ord("x")
    body[:, :, 0] = value
    body[:, :, 1] = table[bits]
    body[:, :, 2] = ord("\n")


def _bits_of(mask: int) -> list[int]:
    return [b for b in range(mask.bit_length()) if mask >> b & 1]


def _text_uniform(magnitude, width: int, negative: bool, v, mask: int, x: bool, table):
    """どのブロックも時刻の行があり、桁数・符号・行のビット・x が同じ: 1 ブロック = 決まった幅の文字の表の 1 行。"""
    n, bits = len(magnitude), _bits_of(mask)
    cols = 2 + negative + width + 3 * len(bits)
    chars = _np.empty((n, cols), dtype=_np.uint8)
    chars[:, 0] = ord("#")
    if negative:
        chars[:, 1] = ord("-")
    _put_digits(chars, 1 + negative, width, magnitude)
    chars[:, 1 + negative + width] = ord("\n")
    if bits:
        _put_lines(chars, 2 + negative + width, v, bits, _np.full(n, x) if x else None, table)
    return chars.ravel()


def _text_mixed(new, negative, magnitude, digits, v, m, x, table):
    """形のそろわないブロック: 全部の形を覆う文字の表と、使う文字の印の表を作り、印の立った文字だけを行の順に拾う。
    列は、どれかのブロックで立っているビットだけ。"""
    n = len(new)
    sign = bool((negative & new).any())
    width = int(digits[new].max()) if new.any() else 0           # 時刻の行が無い（前と同じ位置だけ）: 0
    bits = _bits_of(int(_np.bitwise_or.reduce(m)))
    cols = 2 + sign + width + 3 * len(bits)
    chars = _np.empty((n, cols), dtype=_np.uint8)
    keep = _np.empty((n, cols), dtype=bool)
    chars[:, 0] = ord("#")
    keep[:, 0] = new
    if sign:
        chars[:, 1] = ord("-")
        _np.logical_and(new, negative, out=keep[:, 1])
    low = sign + width                                            # 一の位の列
    _put_digits(chars, 1 + sign, width, magnitude)
    for j in range(width):                                        # 桁数より上の列は使わない
        _np.logical_and(new, digits > j, out=keep[:, low - j])
    chars[:, low + 1] = ord("\n")
    keep[:, low + 1] = new
    if bits:
        _put_lines(chars, low + 2, v, bits, x if x.any() else None, table)
        keep[:, low + 2:].reshape(n, len(bits), 3)[:] = _bit_columns(m, bits).view(bool)[:, :, None]
    return chars[keep]


def _render(t, per: float, v, m, x, table, current: float | None):
    """ブロックの列（位置の順）を VCD の行のバイト列に: 位置が前のブロックと違えば "#時刻"（round(t × per)）の行、続けて
    m の立ったビットごとに "値 識別子" の行（ビットの昇順。x のブロックは値の代わりに x。識別子は table[ビット]）。
    戻り: (uint8 の配列, 最後の位置)。

    組み立て: 行ごとの番地の計算・散らした書き込みをせず、uint8 の文字の表を列ごとの一括の計算で埋める。同じ形（時刻の行の
    有無・桁数・符号・行のビット・x）のブロックが続く所は表をそのまま並べ（_text_uniform。方形波など）、ほかは印で拾う
    （_text_mixed）。"""
    n = len(t)
    if not n:
        return _np.empty(0, _np.uint8), current
    new = _np.empty(n, dtype=bool)
    new[0] = current is None or t[0] != current
    _np.not_equal(t[1:], t[:-1], out=new[1:])
    times = _np.rint(t * per).astype(_np.int64)
    negative = times < 0
    magnitude = _np.abs(times).astype(_np.uint64)
    digits = _digits(magnitude)
    # 形の変わり目 → 同じ形の続き（区間）。長い区間だけ表をそのまま並べる
    change = (digits[1:] != digits[:-1]) | (m[1:] != m[:-1]) | (new[1:] != new[:-1]) | (negative[1:] != negative[:-1])
    change |= x[1:] != x[:-1]
    edges = _np.concatenate(([0], _np.flatnonzero(change) + 1, [n]))
    lengths = _np.diff(edges)
    long = _np.flatnonzero((lengths >= _UNIFORM_MIN) & new[edges[:-1]])
    parts, done = [], 0
    for k in long.tolist():
        a, b = int(edges[k]), int(edges[k + 1])
        if done < a:
            parts.append(_text_mixed(new[done:a], negative[done:a], magnitude[done:a], digits[done:a], v[done:a],
                                     m[done:a], x[done:a], table))
        parts.append(_text_uniform(magnitude[a:b], int(digits[a]), bool(negative[a]), v[a:b], int(m[a]), bool(x[a]),
                                   table))
        done = b
    if done < n:
        parts.append(_text_mixed(new[done:], negative[done:], magnitude[done:], digits[done:], v[done:], m[done:],
                                 x[done:], table))
    return (parts[0] if len(parts) == 1 else _np.concatenate(parts)), float(t[-1])
