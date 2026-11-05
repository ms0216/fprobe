"""長期の記録（U23）: 日単位の連続記録を、分割（segment）・索引・縮図の事前計算・間引き・容量の上限で運用する。

共通の時間軸の記録（fprobe.recording）を時間（または大きさ）で分割する。1 つの分割は普通の記録（format 1）の
ディレクトリそのもので、全体の索引（index.sqlite）が分割の一覧・粗い縮図・波形のかたまりの要約を持つ。

    <記録>/meta.json            layout = "segmented"、タイムベース、方針（policy）、縮図の段（summary）
    <記録>/index.sqlite         segments（分割の一覧）・summary（段 INDEX_LEVEL 以上の件数）・wave（波形のかたまりの要約）
    <記録>/segments/000000/     普通の記録（recording.sqlite・waveform/）+ 閉じたときに作る summary（細かい段）

- 書き手 `SegmentedRecorder` は Recorder と同じ口（add・add_many・add_host・add_waveform・flush・close・clock）。
  flush のときに分割の時間・大きさを超えていれば、いまの分割を閉じ（縮図を事前計算し）、次を開く。時計の対応付けは
  全体で 1 つ（分割をまたいでもホストの出来事の時刻はつながる）。
- 閉じた分割は、方針に従って間引く（古い分割から、量の多い stream と波形のファイルを消す。件数の縮図は残る）か、
  容量の上限を超えたら古いものから消す（粗い縮図は索引に残る: 30 日の全体の概観は消した部分も含めて見える）。
  いちばん新しい分割は消さない。
- 読み手 `SegmentedRecording` は Recording と同じ口。範囲に重なる分割だけを開き（部分読み込み）、件数は縮図の和で
  正確に出す（間引いた・消した部分に端がかかるときは近似: coverage が欠けを返す）。
"""
from __future__ import annotations

import contextlib
import heapq
import json
import sqlite3
from collections.abc import Iterator
from operator import itemgetter
from pathlib import Path
from typing import Any

from .recording import Recording
from .recording_long_types import _Reads, _Segment  # noqa: F401
from .recording_long_view import ViewMixin
from .recording_model import Event
from .recording_long_write import SegmentedRecorder
from .recording_segments import DEFAULT_DECIMATE, INDEX_LEVEL, INDEX_SCHEMA, Policy, connect_long
from .recording_summary import SummaryGrid
from .recording_table import ROW_COLUMNS, extent

__all__ = ["DEFAULT_DECIMATE", "INDEX_LEVEL", "Policy", "SegmentedRecorder", "SegmentedRecording"]


# --- 読み手 ------------------------------------------------------------------------------------

class SegmentedRecording(ViewMixin, Recording):
    """長期の記録の読み手（Recording と同じ口）。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
        self.grid = SummaryGrid.from_dict(self.meta["summary"])
        self.index_level = int(self.meta["summary"].get("index_level", INDEX_LEVEL))
        self._index = connect_long(self.directory / "index.sqlite", INDEX_SCHEMA)
        self._init_waves()

    def close(self) -> None:
        self._index.close()

    def add(self, stream: str, kind: str, t: int, a: int = 0, b: int = 0, body: Any = None,
            t_end: int | None = None) -> None:
        # 分割ごとの縮図・件数の索引と合わなくなるので、閉じた長期の記録には足さない
        raise RuntimeError("長期の記録（分割した記録）には後から出来事を足せない（記録中に recording_mark で）")

    # --- 分割 -----------------------------------------------------------------------------------

    def _segment_rows(self) -> list[_Segment]:
        rows = self._index.execute("SELECT no, path, state, t0, t1, events, bytes, dropped FROM segments ORDER BY no")
        return [_Segment(*r[:7], json.loads(r[7])) for r in rows]

    def _live(self) -> list[_Segment]:
        """消していない分割。開いている分割は、いまの範囲と件数を読んで埋める。"""
        out = []
        for seg in self._segment_rows():
            if seg.state == "deleted":
                continue
            if seg.state == "open":
                db = connect_long(self.directory / seg.path / "recording.sqlite")
                try:
                    lo, hi, n = extent(db)
                    clo, chi = db.execute("SELECT MIN(t0), MAX(t1) FROM chunks").fetchone()
                except sqlite3.OperationalError:                   # 作った直後で表がまだ無い
                    lo = hi = clo = chi = None
                    n = 0
                finally:
                    db.close()
                seg.t0 = min((v for v in (lo, clo) if v is not None), default=None)
                seg.t1 = max((v for v in (hi, chi) if v is not None), default=None)
                seg.events = n
            out.append(seg)
        return out

    def segments(self) -> list[dict]:
        """分割の一覧（no・path・state = open / sealed / deleted・範囲・件数・大きさ・間引いた stream）。"""
        live = {s.no: s for s in self._live()}
        return [live.get(s.no, s).to_dict() for s in self._segment_rows()]

    @contextlib.contextmanager
    def _reads(self) -> Iterator[_Reads]:
        reads = _Reads(self.directory)
        try:
            yield reads
        finally:
            reads.close()

    # --- 出来事 --------------------------------------------------------------------------------

    def _rows(self, t0: int | None, t1: int | None, streams: list[str] | None, kinds: list[str] | None,
              overlapping: bool = False, a: int | None = None, columns: str = ROW_COLUMNS) -> Iterator[tuple]:
        """範囲に重なる分割だけを読み、時刻順に返す（時刻の重なる分割どうしは混ぜて並べる。同じ時刻は分割の順）。"""
        segs = sorted((s for s in self._live() if overlapping and t0 is not None and s.t0 is not None
                       and (t1 is None or s.t0 < t1) or s.overlaps(t0, t1)), key=lambda s: s.t0)
        groups: list[list[_Segment]] = []
        for s in segs:
            if groups and s.t0 <= max(g.t1 for g in groups[-1]):
                groups[-1].append(s)
            else:
                groups.append([s])

        for group in groups:
            readers = [Recording(self.directory / s.path) for s in group]
            try:
                its = [r._rows(t0, t1, streams, kinds, overlapping, a, columns) for r in readers]
                yield from its[0] if len(its) == 1 else heapq.merge(*its, key=itemgetter(0))
            finally:
                for r in readers:
                    r.close()

    def _distinct_a(self, stream: str, kind: str) -> list[int]:
        values: set[int] = set()
        for s in self._live():
            r = Recording(self.directory / s.path)
            try:
                values.update(r._distinct_a(stream, kind))
            finally:
                r.close()
        return sorted(values)

    def nearest(self, t: int, before: bool, stream: str, kind: str | None = None, a: int | None = None,
                body: dict[str, Any] | None = None) -> Event | None:
        """Recording.nearest と同じ。分割を t に近い順に読み、見つけた出来事より遠い分割は読まない（分割の時刻は重なりうる
        ので、範囲で比べる）。"""
        segs = [s for s in self._live() if s.t0 is not None and (s.t0 <= t if before else s.t1 > t)]
        segs.sort(key=(lambda s: -s.t1) if before else (lambda s: s.t0))
        best: Event | None = None
        for s in segs:
            if best is not None and (s.t1 < best.t if before else s.t0 > best.t):
                break
            r = Recording(self.directory / s.path)
            try:
                found = r.nearest(t, before, stream, kind, a, body)
            finally:
                r.close()
            if found is not None and (best is None or (found.t > best.t if before else found.t < best.t)):
                best = found
        return best

    def count(self) -> int:
        return sum(s.events for s in self._live())

    def count_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                     kinds: list[str] | None = None) -> int:
        """iter_events（limit なし）が返す出来事の数（範囲に重なる分割ごとに数えて足す）。"""
        total = 0
        for s in self._live():
            if not s.overlaps(t0, t1):
                continue
            r = Recording(self.directory / s.path)
            try:
                total += r.count_events(t0, t1, streams, kinds)
            finally:
                r.close()
        return total

    def distinct(self, column: str, streams: list[str] | None = None) -> list:
        values: set = set()
        for s in self._live():
            r = Recording(self.directory / s.path)
            try:
                values.update(r.distinct(column, streams))
            finally:
                r.close()
        return sorted(values)

    def kind_counts(self, t0: int | None = None, t1: int | None = None) -> list[tuple[str, str, int, int, int]]:
        """範囲に重なるすべての分割の stream・kind ごとの件数を足す（分割の数で切らない）。"""
        total: dict[tuple[str, str], list[int]] = {}
        for s in self._live():
            if not s.overlaps(t0, t1):
                continue
            r = Recording(self.directory / s.path)
            try:
                for stream, kind, n, lo, hi in r.kind_counts(t0, t1):
                    acc = total.get((stream, kind))
                    if acc is None:
                        total[(stream, kind)] = [n, lo, hi]
                    else:
                        acc[0] += n
                        acc[1], acc[2] = min(acc[1], lo), max(acc[2], hi)
            finally:
                r.close()
        return [(stream, kind, n, lo, hi) for (stream, kind), (n, lo, hi) in sorted(total.items())]

    def span(self) -> tuple[int, int] | None:
        segs = [s for s in self._live() if s.t0 is not None]
        if not segs:
            return None
        return min(s.t0 for s in segs), max(s.t1 for s in segs)
