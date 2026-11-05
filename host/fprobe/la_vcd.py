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
from .la_vcd_render import _VcdBlocks, _digits, _groups, _render  # noqa: F401  (_digits は recording_vcd が読む)
from .la_wave import _np

__all__ = ["DEFAULT_NAMES", "VcdWriter", "default_workers","vcd_blocks", "vcd_blocks_skewed", "vcd_comments", "vcd_unit", "write_vcd"]

_MAX_FAST_BITS = 64            # 行を出すビット（ch とトリガ）がこれ以下なら一括の道（numpy の符号なし整数 1 つに収まる）


def _fast(bits: int) -> bool:
    return _np is not None and bits <= _MAX_FAST_BITS


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
