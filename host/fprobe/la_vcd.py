"""ロジックアナライザの VCD 出力（PulseView 等で表示）。fprobe.la が再公開する。

numpy があれば、変化点の列をまとめて VCD の行のバイト列にする（_VcdBlocks・_render。変化点ごとの Python のループを
回さない）。無ければ変化点ごとに行を作る（vcd_blocks 等。同じバイト列で遅いだけ。tests/test_la_vcd_golden.py が両方を固定）。
"""

import heapq
import os
import queue
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from operator import itemgetter
from pathlib import Path

from .caps import legacy_la_layout
from .la_wave import Capture, Transitions
from .la_wave import _np

__all__ = ["DEFAULT_NAMES", "VcdWriter", "default_workers","vcd_blocks", "vcd_blocks_skewed", "vcd_comments", "vcd_unit", "write_vcd"]

# 古い並び（ケイパビリティ LA_LAYOUT の前）の内部の ch の名前。今の並びは channel_names（申告から）
DEFAULT_NAMES = {c.channel: c.name for c in legacy_la_layout(10) if c.kind == "internal"}

# VCD の時刻の単位の候補（粗い順）: (1 単位の ns, (数, 単位))
_UNITS = ((1.0, (1, "ns")), (0.1, (100, "ps")), (0.01, (10, "ps")), (0.001, (1, "ps")))


def vcd_unit(steps_ns) -> tuple[float, tuple[int, str], bool]:
    """時刻がすべて steps_ns（ns）の整数倍の和で表せるとき、それを丸めずに書ける最も粗い VCD の単位: (1 単位の ns,
    (数, 単位), 丸めないか)。1 ps でも割り切れなければ 1 ps で丸める（丸めない = False。書き手は $comment で示す）。
    例: 10 ns の格子は 1 ns、800 MS/s（1.25 ns）は 10 ps。"""
    steps = [abs(v) for v in steps_ns if v]
    for unit, label in _UNITS:
        if all(abs(v / unit - round(v / unit)) < 1e-6 for v in steps):
            return unit, label, True
    return _UNITS[-1][0], _UNITS[-1][1], False


def vcd_comments(exact: bool, unknown: Sequence[int], names: dict[int, str] | None = None) -> list[str]:
    """時刻の事実の $comment（VCD は ASCII なので英語）: 単位に丸めたこと、ずれが分からずずらさずに置いた ch。"""
    out = [] if exact else ["$comment times rounded to the timescale $end"]
    if unknown:
        label = " ".join((names or {}).get(ch, f"ch{ch}") for ch in unknown)
        out.append(f"$comment skew unknown (placed unshifted): {label} $end")
    return out


def write_vcd(capture: "Capture | Transitions", path: str | Path, names: dict[int, str] | None = None,
              timebase_hz: float | None = None) -> None:
    """VCD（PulseView 等で表示）。時刻 0 = 最初のサンプル、単位は ns。トリガ位置にマーカー用の信号を置く。
    timebase_hz を渡せば、ch ごとの時刻のずれ（channel_skew。タイムベースの刻み）で ch を置き直す（ずれの分からない ch は
    ずらさずに置き、$comment に名前を書く）。単位は時刻を丸めない最も粗いもの（vcd_unit。10 ns の格子は ns）。"""
    # トリガの信号: (サンプル番号, 値) の昇順。同じ時刻の変化の後に置く
    if isinstance(capture, Transitions):
        tr = capture
        marks = sorted([(0, 0), (tr.trigger, 1), (tr.trigger + 1, 0)], key=itemgetter(0))
    else:                                           # 最初・トリガ・その次のサンプルで、そのサンプルがトリガなら 1
        tr = Transitions.from_capture(capture)
        marks = [(i, int(i == capture.trigger))
                 for i in sorted({0, capture.trigger, capture.trigger + 1}) if 0 <= i < len(capture.samples)]
    shifts = _shifts(tr, timebase_hz)
    unit, ids, trigger_id, lines = _vcd_header(tr.channels, names, trigger=True,
                                               **_timing(tr, shifts, timebase_hz, names))
    with open(path, "wb") as f:
        f.write(("\n".join(lines) + "\n").encode("ascii"))
        if _fast(tr.channels + 1):
            blocks = _VcdBlocks(tr.channels + 1, [ids[ch] for ch in range(tr.channels)] + [trigger_id])
            values, changed = blocks.changes(tr.values, None, tr.channels)
            starts = _np.asarray(tr.starts)
            for group, (shift, gmask) in enumerate(_groups(shifts, tr.channels)):
                blocks.add_changes(starts, values, changed & blocks.dtype(gmask), shift, 0, group)
            at = _np.array([t for t, _ in marks], dtype=_np.float64)
            bits = _np.array([v << tr.channels for _, v in marks], dtype=blocks.dtype)
            blocks.add(at, bits, _np.full(len(marks), 1 << tr.channels, dtype=blocks.dtype), None, 1,
                       _np.arange(len(marks)))
            taken = blocks.take()
            if taken is not None:
                out = _OrderedOutput(f, default_workers())
                try:
                    out.blocks(taken[0], tr.sample_period_s * 1e9 / unit, *taken[1:], blocks.table, None)
                finally:
                    out.close()
            return
        text_marks = [(t, f"{v}{trigger_id}") for t, v in marks]
        body = _vcd_lines(tr.values, tr.starts, ids, tr.sample_period_s, None, text_marks, shifts, unit)[0]
        if body:
            f.write(("\n".join(body) + "\n").encode("ascii"))


def _shifts(tr: "Transitions", timebase_hz: float | None) -> list[float]:
    return tr.skew_samples(tr.sample_period_s * timebase_hz if timebase_hz else None)


def _timing(tr: "Transitions", shifts: Sequence[float], timebase_hz: float | None,
            names: dict[int, str] | None) -> dict:
    """_vcd_header の時刻の引数: サンプルの間隔と ch のずれから単位、ずれの分からない ch の $comment。"""
    period_ns = tr.sample_period_s * 1e9
    unit, label, exact = vcd_unit([period_ns, *(s * period_ns for s in shifts)])
    unknown = tr.skew_unknown() if timebase_hz else []
    return {"unit": (unit, label), "comments": vcd_comments(exact, unknown, {**DEFAULT_NAMES, **(names or {})})}


def _vcd_header(channels: int, names: dict[int, str] | None, trigger: bool,
                unit: tuple[float, tuple[int, str]] = (1.0, (1, "ns")),
                comments: Sequence[str] = ()) -> tuple[float, dict[int, str], str, list[str]]:
    """1 単位の ns・チャンネルの識別子・トリガの識別子・ヘッダの行。"""
    names = {**DEFAULT_NAMES, **(names or {})}
    ids = {ch: chr(33 + ch) for ch in range(channels)}
    trigger_id = chr(33 + channels)
    count, name = unit[1]
    lines = [f"$timescale {count}{name} $end", *comments, "$scope module fprobe $end"]
    lines += [f"$var wire 1 {ids[ch]} {names.get(ch, f'ch{ch}')} $end" for ch in range(channels)]
    if trigger:
        lines.append(f"$var wire 1 {trigger_id} trigger $end")
    lines += ["$upscope $end", "$enddefinitions $end"]
    return unit[0], ids, trigger_id, lines


def _vcd_change_text(ids: tuple[tuple[int, str], ...], value: int, previous: int | None) -> str:
    """値が previous から value に変わったチャンネルの VCD の行（previous が None なら全チャンネル）。"""
    if previous is None:
        return "\n".join(f"{value >> ch & 1}{i}" for ch, i in ids)
    changed, out = value ^ previous, []
    for ch, i in ids:
        if changed >> ch & 1:
            out.append(f"{value >> ch & 1}{i}")
    return "\n".join(out)


_VCD_CACHE_MAX = 1 << 16


def vcd_blocks(values: Sequence[int], starts: Sequence[int], ids: dict[int, str],
               previous: int | None) -> tuple[list[tuple[int, str]], int | None]:
    """変化点（値, 開始位置）のうち、ids のチャンネルが変わるものの (開始位置, VCD の行) と、最後の値（次の呼び出しの
    previous）。previous が None なら最初の値で全チャンネルを書く。"""
    items = tuple(ids.items())
    cache: dict[int, str] = {}                                    # (前の値, 値) → 行。同じ組は何度も出る（クロックなど）
    blocks = []
    for v, t in zip(values, starts):
        if previous is None:
            text = _vcd_change_text(items, v, None)
        else:
            key = previous << 16 | v
            text = cache.get(key)
            if text is None:
                if len(cache) >= _VCD_CACHE_MAX:
                    cache.clear()
                text = cache[key] = _vcd_change_text(items, v, previous)
        previous = v
        if text:
            blocks.append((t, text))
    return blocks, previous


def vcd_blocks_skewed(values: Sequence[int], starts: Sequence[int], ids: dict[int, str], previous: int | None,
                      shifts: Sequence[float] = ()) -> tuple[list[tuple[float, str]], int | None]:
    """vcd_blocks を、ch ごとのずれ（サンプルの単位。Transitions.skew_samples）で置き直したもの。位置の昇順（同じ位置は
    ずれの小さい ch の組が先）。ずれが無ければ vcd_blocks と同じ。"""
    groups: dict[float, dict[int, str]] = {}
    for ch, ident in ids.items():
        groups.setdefault(shifts[ch] if ch < len(shifts) else 0.0, {})[ch] = ident
    if not any(groups):
        return vcd_blocks(values, starts, ids, previous)
    parts, last = [], previous
    for shift in sorted(groups):
        blocks, last = vcd_blocks(values, starts, groups[shift], previous)
        parts.append([(t + shift, text) for t, text in blocks])
    return list(heapq.merge(*parts, key=itemgetter(0))), last


def _vcd_lines(values: Sequence[int], starts: Sequence[int], ids: dict[int, str], period_s: float,
               previous: int | None, marks: Sequence[tuple[int, str]] = (),
               shifts: Sequence[float] = (), unit_ns: float = 1.0) -> tuple[list[str], int | None]:
    """変化点（値, 開始サンプル番号）の VCD の行と、最後の値。marks = (サンプル番号, 行) の昇順は、同じ時刻の変化の後に入れる。
    shifts は ch ごとのずれ（サンプルの単位）。時刻は unit_ns（ns）の単位。"""
    blocks, previous = vcd_blocks_skewed(values, starts, ids, previous, shifts)
    if marks:
        blocks = sorted(blocks + list(marks), key=itemgetter(0))      # 安定: 同じ時刻は変化が先
    lines, current = [], None
    per = period_s * 1e9 / unit_ns
    for t, text in blocks:
        if t != current:
            lines.append(f"#{round(t * per)}")
            current = t
        lines.append(text)
    return lines, previous




# --- 一括の書き出し（numpy）-----------------------------------------------------------------------------------

_MAX_FAST_BITS = 64            # 行を出すビット（ch とトリガ）がこれ以下なら一括の道（numpy の符号なし整数 1 つに収まる）


def _fast(bits: int) -> bool:
    return _np is not None and bits <= _MAX_FAST_BITS


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


_PIECE = 1 << 16               # 1 回に描くブロックの数（並列の単位。キャッシュに収まる大きさ）


def default_workers() -> int:
    """VCD の行を描くスレッドの既定の数（numpy の計算は GIL を離す。2026-11-03 の計測で 8 を超えると速くならない）。"""
    return max(1, min(8, os.cpu_count() or 1))


class _OrderedOutput:
    """描いた VCD の行のバイト列を、頼んだ順にファイルへ書く。workers > 1 なら、描くのはスレッドの組で並列に、ファイルへの
    書き込みは書き手のスレッド 1 本で（頼んだ順に、描き終わるのを待って）。頼んだまま書いていないものは 4 × workers 件まで
    （越えると頼む側が待つ）。描く・書くで起きた例外は、次に頼むときか close で頼んだ側に出す。"""

    def __init__(self, file, workers: int):
        self._file = file
        self._pool = None
        if workers > 1:
            self._pool = ThreadPoolExecutor(workers, thread_name_prefix="vcd")
            self._queue: queue.Queue = queue.Queue(4 * workers)
            self._error: BaseException | None = None
            self._writer = threading.Thread(target=self._write_loop, name="vcd-writer", daemon=True)
            self._writer.start()

    def text(self, data: bytes) -> None:
        if self._pool is None:
            self._file.write(data)
        else:
            self._put(data)

    def pieces(self, n: int, job) -> None:
        """ブロック 0〜n − 1 を _PIECE ずつ job(a, b)（ブロック a〜b − 1 の行のバイト列を返す）で描いて書く。"""
        for a in range(0, n, _PIECE):
            b = min(n, a + _PIECE)
            if self._pool is None:
                self._file.write(job(a, b))
            else:
                self._put(self._pool.submit(job, a, b))

    def blocks(self, t, per: float, v, m, x, table, current: float | None) -> float | None:
        """時刻順のブロックを描いて書く（各かたまりの「前の位置」はその前のブロックの位置）。最後の位置を返す。"""
        def job(a: int, b: int):
            return _render(t[a:b], per, v[a:b], m[a:b], x[a:b], table, current if a == 0 else float(t[a - 1]))[0]
        self.pieces(len(t), job)
        return float(t[-1]) if len(t) else current

    def _put(self, item) -> None:
        if self._error is not None:
            raise self._error
        self._queue.put(item)

    def _write_loop(self) -> None:
        while (item := self._queue.get()) is not None:
            if self._error is not None:
                continue                                       # 誤りの後は読み捨てる（頼む側を止めない）
            try:
                self._file.write(item if isinstance(item, bytes) else item.result())
            except BaseException as e:                         # 頼んだ側に close で出す
                self._error = e

    def close(self) -> None:
        if self._pool is None:
            return
        try:
            self._queue.put(None)
            self._writer.join()
        finally:
            self._pool.shutdown(cancel_futures=True)
        if self._error is not None:
            raise self._error


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


class VcdWriter:
    """VCD を変化点の順に少しずつ書く（ストリーミング用。全体をメモリに持たない）。トリガの信号は無い。
    shifts = ch ごとの時刻のずれ（サンプルの単位。Transitions.skew_samples）、unknown = ずれが分からずずらさない ch
    （$comment に書く）。ずらした ch の変化は、次のかたまりの変化と前後が入れ替わりうる所だけ持っておき、時刻順に書く。
    write の values・starts は array・numpy の配列（StreamChunk.changes）・列のどれでもよい（write の中で写すので、戻った後に
    書き換えてよい）。workers = 行を描くスレッドの数（None = default_workers()。1 = 呼んだスレッドで描く）。numpy が無ければ
    変化点ごとに描く（同じバイト列）。ファイルに書き終わるのは close（with を抜けたとき）。"""

    def __init__(self, path: str | Path, channels: int, sample_period_s: float, names: dict[int, str] | None = None,
                 shifts: Sequence[float] = (), unknown: Sequence[int] = (), workers: int | None = None):
        period_ns = sample_period_s * 1e9
        self._channels = channels
        self._shifts = [shifts[ch] if ch < len(shifts) else 0.0 for ch in range(channels)]
        unit, label, exact = vcd_unit([period_ns, *(s * period_ns for s in self._shifts)])
        merged = {**DEFAULT_NAMES, **(names or {})}
        self._unit, self._ids, _, header = _vcd_header(channels, names, trigger=False, unit=(unit, label),
                                                       comments=vcd_comments(exact, unknown, merged))
        self._per = period_ns / self._unit
        self._per_unshifted = self._per / 1e9 * 1e9 / 1.0       # ずらさない道の時刻の掛け算（_vcd_lines と同じ式の値）
        self._skewed = any(self._shifts)
        self._previous: int | None = None
        self._current: float | None = None
        self._file = open(path, "wb")
        self._file.write(("\n".join(header) + "\n").encode("ascii"))
        if _fast(channels):
            self._blocks = _VcdBlocks(channels, [self._ids[ch] for ch in range(channels)])
            self._groups = _groups(self._shifts, channels)
            self._batch = 0
            self._out = _OrderedOutput(self._file, default_workers() if workers is None else workers)
        else:
            self._blocks = None
            self._held: list[tuple[float, int, str]] = []      # まだ書けない変化 (位置, 通し番号, 行)
            self._seq = 0

    def write(self, values: Sequence[int], starts: Sequence[int]) -> None:
        """変化点（値, 通し番号の開始）を書き足す。starts は前回の続きから昇順。"""
        if not len(values):
            return
        if self._blocks is None:
            self._write_py(values, starts)
            return
        blocks = self._blocks
        v, changed = blocks.changes(values, self._previous, self._channels)   # v・changed は新しい配列（写し）
        self._previous = int(_np.asarray(values)[-1])
        if not self._skewed:                                   # ずらす ch が無い: 持たずにそのまま（速い道）
            keep = _np.flatnonzero(changed)
            starts = _np.array(starts)                          # 写し（描くのは後で、別のスレッドで）
            per, table = self._per_unshifted, blocks.table

            def job(a: int, b: int):
                # 時刻の行は位置が前のブロックと違うとき（前の write とは比べない: _vcd_lines と同じ）
                k = keep[a:b]
                before = None if a == 0 else float(starts[keep[a - 1]])
                return _render(starts[k].astype(_np.float64), per, v[k], changed[k], _np.zeros(len(k), dtype=bool),
                               table, before)[0]
            self._out.pieces(len(keep), job)
            return
        starts = _np.asarray(starts)
        for group, (shift, gmask) in enumerate(self._groups):
            blocks.add_changes(starts, v, changed & blocks.dtype(gmask), shift, self._batch, group)
        self._batch += 1
        # 次のかたまりの変化の開始は starts[-1] より後: その最も早いずれの位置より前の分は、もう前後が入れ替わらない
        self._flush(int(starts[-1]) + 1 + min(self._shifts, default=0.0))

    def gap(self, start: int) -> None:
        """流れの位置 start から値が無い（欠けの区間。fprobe.la.StreamGap）: 全信号を x（分からない）にする。埋めない。
        次の write の最初の変化点で全信号を書き直す（欠けの後の値）。ずらす ch は、その ch のずれの位置に置く。"""
        if not self._skewed:
            text = (f"#{round(start * self._per)}\n" + "\n".join(f"x{i}" for i in self._ids.values()) + "\n").encode("ascii")
            if self._blocks is not None:
                self._out.text(text)
            else:
                self._file.write(text)
        elif self._blocks is not None:
            blocks, n = self._blocks, self._channels
            blocks.add(_np.array([start + s for s in self._shifts], dtype=_np.float64), _np.zeros(n, blocks.dtype),
                       _np.array([1 << ch for ch in range(n)], dtype=blocks.dtype), _np.ones(n, dtype=bool),
                       self._batch, _np.arange(n))
            self._batch += 1
        else:
            for ch, ident in self._ids.items():
                heapq.heappush(self._held, (start + self._shifts[ch], self._seq, f"x{ident}"))
                self._seq += 1
        self._previous = None

    def _flush(self, limit: float | None) -> None:
        if self._blocks is not None:
            taken = self._blocks.take(limit)
            if taken is not None:
                t, v, m, x = taken
                self._current = self._out.blocks(t, self._per, v, m, x, self._blocks.table, self._current)
            return
        lines = []
        while self._held and (limit is None or self._held[0][0] < limit):
            t, _, text = heapq.heappop(self._held)
            if t != self._current:
                lines.append(f"#{round(t * self._per)}")
                self._current = t
            lines.append(text)
        if lines:
            self._file.write(("\n".join(lines) + "\n").encode("ascii"))

    def _write_py(self, values: Sequence[int], starts: Sequence[int]) -> None:
        """numpy の無い道: 変化点ごとに行を作る。"""
        if not self._skewed:
            lines, self._previous = _vcd_lines(values, starts, self._ids, self._per / 1e9, self._previous)
            if lines:
                self._file.write(("\n".join(lines) + "\n").encode("ascii"))
            return
        blocks, self._previous = vcd_blocks_skewed(values, starts, self._ids, self._previous, self._shifts)
        for t, text in blocks:
            heapq.heappush(self._held, (t, self._seq, text))
            self._seq += 1
        self._flush(starts[-1] + 1 + min(self._shifts, default=0.0))

    def close(self) -> None:
        try:
            try:
                if self._skewed:
                    self._flush(None)
            finally:
                # 描く途中の誤りで _flush が例外を出しても、書き手のスレッドと描くスレッドの組を必ず止める（レビュー 2026-11-03）
                if self._blocks is not None:
                    self._out.close()
        finally:
            self._file.close()

    def __enter__(self) -> "VcdWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
