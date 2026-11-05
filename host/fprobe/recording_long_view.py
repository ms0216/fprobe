"""長期の記録の読み手の集計と波形: 件数の縮図の和（_counts）、密度・イベントの束・概観・カバレッジ、波形のかたまりと
表示用の列。SegmentedRecording に混ぜて使う（self._live()・self._reads() 等は SegmentedRecording が持つ）。"""
from __future__ import annotations

from array import array
from pathlib import Path

from .recording_long_types import _Reads, _Segment
from .recording_model import NO_DATA, Chunk, in_list
from .recording_segments import WAVEFORM


class ViewMixin:
    # --- 件数（縮図） --------------------------------------------------------------------------

    def _counts(self, reads: _Reads, segs: list[_Segment], t0: int, t1: int,
                streams: list[str] | None, unknown: dict) -> dict[str, int]:
        """[t0, t1) の stream ごとの件数（残っている出来事。下限）。閉じた分割は縮図の和、開いている分割は生で数える。
        数えられない所（間引いた stream の段 0 の bin に端がかかる・消した分割の細かい段）があれば unknown["any"] =
        True。間引いた bin はその bin の件数を unknown["extra"] に足す（[t0, t1) の中の件数はその数以下）。消した分割は
        上限も分からない（unknown["bounded"] = False）。按分等で推定しない。"""
        sealed = [s for s in segs if s.state == "sealed"]
        deleted = [s for s in segs if s.state == "deleted"]
        where_streams = f" AND {in_list('stream', streams)}" if streams else ""
        args_streams = list(streams or [])

        def add(out: dict, rows) -> None:
            for s, n in rows:
                out[s] = out.get(s, 0) + n

        def bins_count(level: int, lo: int, hi: int, _streams) -> dict[str, int]:
            out: dict[str, int] = {}
            if level >= self.index_level:
                add(out, self._index.execute(
                    f"SELECT stream, SUM(count) FROM summary WHERE level = ? AND bin >= ? AND bin < ?{where_streams} "
                    "GROUP BY stream", [level, lo, hi, *args_streams]))
                return out
            w = self.grid.width(level)
            for seg in sealed:
                if seg.overlaps(lo * w, hi * w):
                    add(out, reads.db(seg).execute(
                        f"SELECT stream, SUM(count) FROM summary WHERE level = ? AND bin >= ? AND bin < ?{where_streams} "
                        "GROUP BY stream", [level, lo, hi, *args_streams]))
            if any(seg.overlaps(lo * w, hi * w) for seg in deleted):
                unknown["any"], unknown["bounded"] = True, False
            return out

        def raw_count(a: int, b: int, _streams) -> dict[str, int]:
            out: dict[str, int] = {}
            for seg in sealed:
                if not seg.overlaps(a, b):
                    continue
                db = reads.db(seg)
                dropped = [s for s in seg.dropped if s != WAVEFORM and (not streams or s in streams)]
                keep = f" AND {in_list('stream', dropped, negate=True)}" if dropped else ""
                add(out, db.execute(f"SELECT stream, COUNT(*) FROM events WHERE t >= ? AND t < ?{where_streams}{keep} "
                                    "GROUP BY stream", [a, b, *args_streams, *dropped]))
                if dropped:                                        # 間引いた stream: [a, b) の件数は分からない（bin の件数以下）
                    w0 = self.grid.w0
                    bin_ = a // w0
                    for _s, n in db.execute("SELECT stream, count FROM summary WHERE level = 0 AND bin = ? AND "
                                            + in_list("stream", dropped), [bin_, *dropped]):
                        if n:
                            unknown["any"] = True
                            unknown["extra"] += n
            if any(seg.overlaps(a, b) for seg in deleted):
                unknown["any"], unknown["bounded"] = True, False
            return out

        out = self.grid.count(t0, t1, bins_count, raw_count, streams)
        for seg in segs:
            if seg.state == "open" and seg.overlaps(t0, t1):
                add(out, reads.db(seg).execute(
                    f"SELECT stream, COUNT(*) FROM events WHERE t >= ? AND t < ?{where_streams} GROUP BY stream",
                    [t0, t1, *args_streams]))
        return out

    def density(self, t0: int, t1: int, width: int, streams: list[str] | None = None) -> list[int]:
        """画素ごとの出来事の件数（density_bounds の counts。数えられない画素は残っている出来事だけの下限）。"""
        return self.density_bounds(t0, t1, width, streams)["counts"]

    def density_bounds(self, t0: int, t1: int, width: int, streams: list[str] | None = None) -> dict:
        """画素ごとの出来事の件数と、数えられない画素。counts: 縮図の和と残っている出来事（間引いた・消した部分に
        端がかかる画素は、残っている出来事だけの下限）。inexact: そういう画素の [{"pixel", "max"}]（max = 件数の上限。
        間引いた bin の件数を足したもの。消した分割にかかれば None = 上限も分からない）。按分等で推定しない。"""
        if t1 <= t0 or width < 1:
            raise ValueError("要約の区間が空")
        segs = self._all_with_ranges()
        span, counts, inexact = t1 - t0, [], []
        with self._reads() as reads:
            for px in range(width):
                p0, p1 = t0 + px * span // width, t0 + (px + 1) * span // width
                unknown = {"any": False, "extra": 0, "bounded": True}
                n = sum(self._counts(reads, segs, p0, p1, streams, unknown).values()) if p1 > p0 else 0
                counts.append(n)
                if unknown["any"]:
                    inexact.append({"pixel": px, "max": n + unknown["extra"] if unknown["bounded"] else None})
        return {"counts": counts, "inexact": inexact}

    def event_bins(self, start: int, width: int, count: int, streams: list[str] | None = None) -> dict[str, array]:
        """Recording.event_bins と同じ。幅が縮図の段の幅で境がそろっていれば、閉じた分割は事前計算の縮図から
        （間引いた・消した分割の件数も残る）、開いている分割は生で数える。そうでなければ残っている出来事を生で数える
        （間引いた・消した部分は数に入らない: coverage で分かる）。"""
        if width < 1 or count < 1:
            raise ValueError("区間の幅と数は 1 以上")
        level = next((lv for lv in range(self.grid.levels) if self.grid.width(lv) == width), None)
        aligned = level is not None and start % width == 0
        end = start + width * count
        first = start // width
        where = f" AND {in_list('stream', streams)}" if streams else ""
        out: dict[str, array] = {}

        def add(rows, offset: int) -> None:
            for stream, b, n in rows:
                if 0 <= b - offset < count:
                    out.setdefault(stream, array("I", [0]) * count)[b - offset] += n

        def summary(db, lv: int) -> None:
            add(db.execute(f"SELECT stream, bin, count FROM summary WHERE level = ? AND bin >= ? AND bin < ?{where}",
                           [lv, first, first + count, *(streams or [])]), first)

        def raw(db) -> None:
            add(db.execute(f"SELECT stream, (t - ?) / ?, COUNT(*) FROM events WHERE t >= ? AND t < ?{where} "
                           "GROUP BY 1, 2", [start, width, start, end, *(streams or [])]), 0)

        segs = self._all_with_ranges()
        with self._reads() as reads:
            if aligned and level >= self.index_level:
                summary(self._index, level)
            for seg in segs:
                if not seg.overlaps(start, end) or seg.state == "deleted":
                    continue
                if seg.state == "open" or not aligned:
                    raw(reads.db(seg))
                elif level < self.index_level:
                    summary(reads.db(seg), level)
        return out

    def query_events(self, t0: int, t1: int, queries: list[tuple[str, list]]) -> list[tuple]:
        """Recording.query_events と同じ。[t0, t1) に重なる、消していない分割ごとに問い合わせる（間引いた分割は
        残っている出来事だけ。coverage で分かる）。"""
        out: list[tuple] = []
        with self._reads() as reads:
            for seg in self._live():
                if seg.overlaps(t0, t1):
                    db = reads.db(seg)
                    out.extend(row for sql, args in queries for row in db.execute(sql, args))
        return out

    def wave_chunks(self, t0: int, t1: int) -> list[tuple[Chunk, bool]]:
        return [(Chunk(*r[:6]), bool(r[9])) for r in self._wave_rows(t0, t1, kept_only=False)]

    def _all_with_ranges(self) -> list[_Segment]:
        live = {s.no: s for s in self._live()}
        return [live.get(s.no, s) for s in self._segment_rows()]

    def overview(self, width: int = 1000, streams: list[str] | None = None) -> dict:
        """記録の全体（消した分割も含む）の縮図: 画素ごと・stream ごとの件数と波形。画素の境は粗い段（INDEX_LEVEL
        以上）の区間にそろえ、事前計算した索引だけで出す（巨大な記録でも速い）。"""
        if width < 1:
            raise ValueError("width は 1 以上")
        segs = self._all_with_ranges()
        ranged = [s for s in segs if s.t0 is not None]
        if not ranged:
            return {"span": None, "edges": [], "total": [], "streams": {}, "waveform": [], "segments": []}
        lo, hi = min(s.t0 for s in ranged), max(s.t1 for s in ranged) + 1
        level = max(self.index_level, self.grid.level_for((hi - lo) // width))
        w = self.grid.width(level)
        first, last = lo // w, -(-hi // w)
        per_pixel = -(-(last - first) // width)
        edges = [(first + i * per_pixel) * w for i in range(width + 1)]
        where = f" AND {in_list('stream', streams)}" if streams else ""
        table: dict[str, list[int]] = {}
        for stream, b, n in self._index.execute(
                f"SELECT stream, bin, count FROM summary WHERE level = ? AND bin >= ? AND bin < ?{where}",
                [level, first, first + per_pixel * width, *(streams or [])]):
            table.setdefault(stream, [0] * width)[(b - first) // per_pixel] += n
        with self._reads() as reads:                              # 開いている分割は縮図が無い: 生で数える
            for seg in segs:
                if seg.state != "open" or seg.t0 is None:
                    continue
                for px in range(width):
                    if edges[px + 1] <= seg.t0 or edges[px] > seg.t1:
                        continue
                    rows = reads.db(seg).execute(
                        f"SELECT stream, COUNT(*) FROM events WHERE t >= ? AND t < ?{where} GROUP BY stream",
                        [edges[px], edges[px + 1], *(streams or [])])
                    for stream, n in rows:
                        table.setdefault(stream, [0] * width)[px] += n
        total = [sum(col) for col in zip(*table.values())] if table else [0] * width
        return {"span": [lo, hi - 1], "edges": edges, "pixel_ticks": per_pixel * w, "total": total,
                "streams": dict(sorted(table.items())), "waveform": self.waveform_view(edges[0], edges[-1], width),
                "segments": [{"no": s.no, "state": s.state, "t0": s.t0, "t1": s.t1, "dropped": s.dropped}
                             for s in segs]}

    def coverage(self, t0: int | None = None, t1: int | None = None) -> dict:
        """範囲に重なる分割のうち、消した（state deleted）・間引いた（dropped）もの。"""
        gaps = [{"segment": s.no, "t0": s.t0, "t1": s.t1, "state": s.state, "dropped": s.dropped}
                for s in self._all_with_ranges() if s.overlaps(t0, t1) and (s.state == "deleted" or s.dropped)]
        return {"complete": not gaps, "gaps": gaps}

    # --- 波形 --------------------------------------------------------------------------------

    def _wave_rows(self, t0: int | None, t1: int | None, kept_only: bool) -> list[tuple]:
        clauses, args = [], []
        if t1 is not None:
            clauses.append("t0 < ?")
            args.append(t1)
        if t0 is not None:
            longest = self._index.execute("SELECT COALESCE(MAX(t1 - t0), 0) FROM wave").fetchone()[0]
            clauses.append("t0 >= ? AND t1 > ?")                    # 索引 t0 の範囲で引く（かたまりの長さで抑える）
            args.extend([t0 - longest, t0])
        if kept_only:
            clauses.append("kept = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._index.execute("SELECT t0, t1, path, ticks_per_sample, channels, length, first, last, toggles, kept "
                                   f"FROM wave{where} ORDER BY t0", args).fetchall()

    def _chunks(self, t0: int | None = None, t1: int | None = None) -> list[Chunk]:
        return [Chunk(*r[:6]) for r in self._wave_rows(t0, t1, kept_only=True)]

    def _chunk_file(self, chunk: Chunk) -> Path:
        return self.directory / chunk.path

    def _channel_count(self) -> int:
        return self._index.execute("SELECT COALESCE(MAX(channels), 0) FROM wave").fetchone()[0]

    def waveform_view(self, t0: int, t1: int, width: int) -> list[str]:
        """Recording.waveform_view と同じ（'0' / '1' / 'x' / '-'）に、'?' = ファイルを間引いたかたまりが画素より長く、
        そのかたまりの中で変化したチャンネル（この画素の中で変わったかは分からない）。画素が丸ごと含むかたまりは事前計算の
        要約（最初の値・変化したチャンネル）で、端のかかるかたまりはファイルを読んで正確に。"""
        if t1 <= t0 or width < 1:
            raise ValueError("要約の区間が空")
        channels = self._channel_count()
        full = (1 << channels) - 1
        highs, lows, unknown = [0] * width, [0] * width, [0] * width
        span = t1 - t0
        for row in self._wave_rows(t0, t1, kept_only=False):
            c = Chunk(*row[:6])
            first, toggles, kept = row[6], row[8], row[9]
            px_lo = max(0, (max(c.t0, t0) - t0) * width // span)
            px_hi = min(width - 1, (min(c.t1, t1) - t0) * width // span)
            for px in range(px_lo, px_hi + 1):
                p0, p1 = t0 + px * span // width, t0 - (-(px + 1) * span // width)
                if c.t1 <= p0 or c.t0 >= p1:
                    continue
                if c.t0 >= p0 and c.t1 <= p1:                    # 画素の中のかたまり: 変化は画素の中
                    highs[px] |= first | toggles
                    lows[px] |= (~first | toggles) & full
                    continue
                if not kept:                                     # 間引いた: 変化したチャンネルはこの画素で分からない
                    highs[px] |= first & ~toggles
                    lows[px] |= ~first & ~toggles & full
                    unknown[px] |= toggles
                    continue
                s0 = max(0, int((p0 - c.t0) / c.ticks_per_sample))
                s1 = min(c.length, max(s0 + 1, int(-(-(p1 - c.t0) // c.ticks_per_sample))))
                for ch, level in enumerate(self._wave(c).view(s0, s1, 1)):
                    if level[0] in "1x":
                        highs[px] |= 1 << ch
                    if level[0] in "0x":
                        lows[px] |= 1 << ch
        rows = []
        for ch in range(channels):
            chars = []
            for h, l, u in zip(highs, lows, unknown):
                hi, lo = h >> ch & 1, l >> ch & 1
                chars.append("x" if hi and lo else "?" if u >> ch & 1 else "1" if hi else "0" if lo else NO_DATA)
            rows.append("".join(chars))
        return rows
