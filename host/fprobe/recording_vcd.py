"""記録の VCD の書き出し（Recording.export_vcd）の一括の道（numpy）: 波形のかたまりの変化点を、ロジアナの流しの VCD と
同じ部品（fprobe.la_vcd の _VcdBlocks・_render・_OrderedOutput）でまとめて行のバイト列にし、FPGA の観測の行を時刻の順に
差し込む。書くバイト列は変化点ごとの道（Recording.export_vcd の中）と同じ（tests/test_recording_vcd_golden.py が固定）。

変化点ごとの道の決まり（ここでも同じにする）:
- 波形のブロック = (かたまり, 変化点, ずれの組) ごとの行のかたまり。時刻 = round((かたまりの t0 + (開始 + ずれ) × 刻み/サンプル)
  × 単位の倍率)。並びは (時刻, かたまりの順, ずらした位置, ずれの組の順)。
- ch のずれで、かたまりの終わりのブロックが次のかたまりの始まりより後になりうるので、次のかたまりの最も早い位置
  （limit）より前のブロックだけを出し、残りは次と合わせて並べる。
- 同じ時刻では波形の行が先、FPGA の行が後。時刻の行（#時刻）は前に書いた行と時刻が違うときだけ。
- 行の中の ch の順は channels の並び（重なりは最初の位置）。改行は OS の既定（os.linesep）。
"""

from __future__ import annotations

import bisect
import os
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from . import la_vcd

__all__ = ["fast_ok", "write_fast"]


def fast_ok(wanted: Sequence[int], ids: dict[int, str]) -> bool:
    """一括の道で書けるか（numpy があり、波形の ch の識別子が 1 文字で、ch がビットの幅に収まる）。"""
    unique = list(dict.fromkeys(wanted))
    return la_vcd._fast(len(unique)) and all(len(ids[ch]) == 1 for ch in unique)


def _popcount(m):
    np = la_vcd._np
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(m).astype(np.int64)
    return np.unpackbits(m.astype(m.dtype.newbyteorder("<"), copy=False).view(np.uint8).reshape(len(m), -1),
                         axis=1).sum(axis=1, dtype=np.int64)


class _Held:
    """まだ出せない波形のブロック（時刻・かたまりの順・位置・組・値・行のビット）を持ち、limit より前を並べて出す。"""

    def __init__(self):
        self._parts: list[tuple] = []
        self._ordered = True                                # 持っているブロックがもう並びの順か

    def add(self, *columns, ordered: bool = False) -> None:
        """ordered = この分だけで並びの順（1 つのかたまりの 1 つの組: 時刻は位置とともに増える）。"""
        if len(columns[0]):
            self._ordered = ordered and not self._parts
            self._parts.append(columns)

    def take(self, limit: float | None):
        np = la_vcd._np
        if not self._parts:
            return None
        t, k, pos, g, v, m = (np.concatenate(c) if len(c) > 1 else c[0] for c in zip(*self._parts))
        ordered = self._ordered
        if limit is None:
            self._parts = []
        else:
            early = t < limit
            take, rest = np.flatnonzero(early), np.flatnonzero(~early)
            self._parts = [tuple(c[rest] for c in (t, k, pos, g, v, m))] if len(rest) else []
            self._ordered = ordered or not self._parts                  # 並びの順の一部は並びの順
            if not len(take):
                return None
            t, k, pos, g, v, m = (c[take] for c in (t, k, pos, g, v, m))
        if ordered:
            return t, v, m
        order = np.lexsort((g, pos, k, t))
        return t[order], v[order], m[order]


def write_fast(path: Path, header: list[str], chunks: list, read: Callable[[object], tuple], wanted: Sequence[int],
               ids: dict[int, str], scale: float, fpga: Iterator[tuple[int, str]], workers: int | None = None) -> None:
    """header（行の並び）、波形のかたまり chunks（時刻順。read(かたまり) = (値, 開始, ch ごとのずれ（サンプル）, 次の
    limit のための channel_skew)）、FPGA の行 fpga（(時刻, 行) の時刻順）を VCD に書く。"""
    np = la_vcd._np
    unique = list(dict.fromkeys(wanted))                    # 行の中の ch の順 = ビットの順（ビット i = ch unique[i]）
    blocks = la_vcd._VcdBlocks(len(unique), [ids[ch] for ch in unique])
    table, dtype = blocks.table, blocks.dtype
    identity = unique == list(range(len(unique)))
    newline = os.linesep.encode("ascii")
    crlf = newline != b"\n"

    def remap(values):
        """値のビット ch unique[i] → ビット i。"""
        v = np.asarray(values)
        if identity:
            return v
        out = np.zeros(len(v), dtype=np.uint64)
        for i, ch in enumerate(unique):
            out |= ((v.astype(np.uint64) >> np.uint64(ch)) & np.uint64(1)) << np.uint64(i)
        return out

    held, previous = _Held(), None
    pending = next(fpga, None)                              # まだ書いていない FPGA の行（先読み 1 つ）
    last: float | None = None                               # 最後に書いた行の時刻（#時刻 を書くかの比べ）
    workers = la_vcd.default_workers() if workers is None else workers

    with Path(path).open("wb") as f:
        out = la_vcd._OrderedOutput(f, workers)
        try:
            out.text(("\n".join(header) + "\n").encode("ascii").replace(b"\n", newline) if crlf
                     else ("\n".join(header) + "\n").encode("ascii"))
            following = read(chunks[0]) if chunks else None
            for k, c in enumerate(chunks):
                values, starts, shifts, _skew = following
                following = read(chunks[k + 1]) if k + 1 < len(chunks) else None
                v, changed = blocks.changes(remap(values), previous, len(unique))
                if len(values):
                    previous = int(v[-1])
                groups: dict[float, int] = {}
                for i, ch in enumerate(unique):
                    s = shifts[ch] if ch < len(shifts) else 0.0
                    groups[s] = groups.get(s, 0) | 1 << i
                skewed = any(groups)
                st = np.asarray(starts)
                for gi, (shift, gmask) in enumerate(sorted(groups.items())):
                    keep = np.flatnonzero(changed & dtype(gmask))
                    if not len(keep):
                        continue
                    pos = st[keep].astype(np.float64)
                    if skewed:
                        pos = pos + shift                   # 変化点ごとの道: t + shift（ずれ 0 の組も浮動小数）
                    times = np.rint((c.t0 + pos * c.ticks_per_sample) * scale)
                    held.add(times, np.full(len(keep), k, dtype=np.int64), pos, np.full(len(keep), gi, dtype=np.int64),
                             v[keep], changed[keep] & dtype(gmask), ordered=len(groups) == 1)
                if following is not None:
                    earliest = chunks[k + 1].t0 + min([0.0, *((s or 0.0) for s in following[3])])
                    limit = round(earliest * scale)
                else:
                    limit = None
                taken = held.take(limit)
                lines = []                                  # この分の FPGA の行: (時刻, 行)
                while pending is not None and (limit is None or pending[0] < limit):
                    lines.append(pending)
                    pending = next(fpga, None)
                last = _emit(out, taken, lines, last, table, newline)
            if not chunks:
                lines = list(fpga) if pending is None else [pending, *fpga]
                _emit(out, None, lines, last, table, newline)
        finally:
            out.close()


def _emit(out, taken, lines: list[tuple[int, str]], last: float | None, table, newline: bytes) -> float | None:
    """時刻順の波形のブロック taken と FPGA の行 lines（どれも taken の後に来るブロックより前）を書く。最後の時刻を返す。"""
    np = la_vcd._np
    crlf = newline != b"\n"
    t, v, m = taken if taken is not None else (np.empty(0, dtype=np.float64), None, None)
    n = len(t)
    # FPGA の行の組: 組 idx はブロック idx − 1 の後・ブロック idx の前（同じ時刻は波形が先: 時刻 ≦ tf のブロックの後）
    where = np.searchsorted(t, np.array([tf for tf, _ in lines], dtype=np.float64), side="right").tolist()
    texts: dict[int, bytes] = {}                            # 組 idx → 行のバイト列（改行は LF。書くときに OS の改行へ）
    j = 0
    while j < len(lines):
        idx = where[j]
        current = float(t[idx - 1]) if idx > 0 else last   # 組の直前に書いた行の時刻
        parts = []
        while j < len(lines) and where[j] == idx:
            tf, text = lines[j]
            if tf != current:
                parts.append(f"#{tf}")
                current = tf
            parts.append(text)
            j += 1
        texts[idx] = ("\n".join(parts) + "\n").encode("ascii")

    def native(data: bytes) -> bytes:
        return data.replace(b"\n", newline) if crlf else data

    if 0 in texts:
        out.text(native(texts[0]))
    marks = sorted(i for i in texts if i > 0)
    if n:
        x = np.zeros(n, dtype=bool)
        before = last

        def job(a: int, b: int) -> bytes:
            current = before if a == 0 else float(t[a - 1])
            rendered = la_vcd._render(t[a:b], 1.0, v[a:b], m[a:b], x[a:b], table, current)[0]
            lo, hi = bisect.bisect_right(marks, a), bisect.bisect_right(marks, b)
            if lo == hi:
                return native(rendered.tobytes())
            # 差し込む位置 = ブロックの行のバイト数の累積（_render と同じ数え方: 時刻の行 "#" + 符号 + 桁 + 改行、
            # ビットごとの "値 識別子 改行"）
            tt = t[a:b]
            new = np.empty(b - a, dtype=bool)
            new[0] = current is None or tt[0] != current
            np.not_equal(tt[1:], tt[:-1], out=new[1:])
            size = 3 * _popcount(m[a:b])
            size += new * (2 + (tt < 0) + la_vcd._digits(np.abs(tt).astype(np.uint64)))
            ends = np.cumsum(size).tolist()
            pieces, start = [], 0
            for idx in marks[lo:hi]:
                end = ends[idx - 1 - a]
                pieces += [rendered[start:end].tobytes(), texts[idx]]
                start = end
            pieces.append(rendered[start:].tobytes())
            return native(b"".join(pieces))

        out.pieces(n, job)
    if lines and where[-1] == n:                            # 最後に書いたのは FPGA の行
        return lines[-1][0]
    return float(t[-1]) if n else last
