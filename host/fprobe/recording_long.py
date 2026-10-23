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
import itertools
import json
import shutil
import sqlite3
import threading
import time
from array import array
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .la import Transitions
from .recording import NO_DATA, ClockMap, Event, Recorder, Recording, _Chunk, write_json
from .recording_summary import SummaryGrid, bin_counts

__all__ = ["DEFAULT_DECIMATE", "INDEX_LEVEL", "Policy", "SegmentedRecorder", "SegmentedRecording"]

INDEX_LEVEL = 3                      # この段（約 4 秒）以上の縮図は索引に置く（分割を消しても残る）
DEFAULT_DECIMATE = ("dap", "sample", "swo", "clock", "waveform")    # 間引くときに消す既定（"waveform" = 波形のファイル）
WAVEFORM = "waveform"

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    no INTEGER PRIMARY KEY, path TEXT NOT NULL, state TEXT NOT NULL, t0 INTEGER, t1 INTEGER,
    events INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0, dropped TEXT NOT NULL DEFAULT '[]',
    opened REAL NOT NULL, sealed REAL);
CREATE TABLE IF NOT EXISTS summary (
    level INTEGER NOT NULL, stream TEXT NOT NULL, bin INTEGER NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (level, stream, bin)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS wave (
    segment INTEGER NOT NULL, t0 INTEGER NOT NULL, t1 INTEGER NOT NULL, path TEXT NOT NULL,
    ticks_per_sample REAL NOT NULL, channels INTEGER NOT NULL, length INTEGER NOT NULL,
    first INTEGER NOT NULL, last INTEGER NOT NULL, toggles INTEGER NOT NULL, kept INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS wave_t0 ON wave (t0);
"""
_SEGMENT_SUMMARY = """
CREATE TABLE IF NOT EXISTS summary (
    level INTEGER NOT NULL, stream TEXT NOT NULL, bin INTEGER NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (level, stream, bin)) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class Policy:
    """長期の記録の運用の方針。segment_s: 分割の時間（秒）、segment_bytes: 分割の大きさの上限、max_bytes: 記録全体の
    容量の上限（超えたら古い分割から消す）、decimate_after_s: 閉じてからこの時間が経った分割を間引く、
    decimate_streams: 間引くときに消す stream（"waveform" は波形のファイル）。"""
    segment_s: float = 3600.0
    segment_bytes: int | None = None
    max_bytes: int | None = None
    decimate_after_s: float | None = None
    decimate_streams: tuple[str, ...] = DEFAULT_DECIMATE

    def __post_init__(self):
        if not self.segment_s > 0:
            raise ValueError("segment_s は正の秒数")
        for name in ("segment_bytes", "max_bytes"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} は正のバイト数")
        if self.decimate_after_s is not None and (self.decimate_after_s < 0 or not self.decimate_streams):
            raise ValueError("decimate_after_s は 0 以上で、decimate_streams（間引く stream）が要る")
        object.__setattr__(self, "decimate_streams", tuple(self.decimate_streams))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decimate_streams"] = list(self.decimate_streams)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Policy:
        return cls(**{**d, "decimate_streams": tuple(d.get("decimate_streams", DEFAULT_DECIMATE))})


def _connect(path: Path, schema: str = "") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if schema:
        db.executescript(schema)
    return db


def _bytes_of(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()) if directory.exists() else 0


def _toggles(values) -> int:
    out, previous = 0, values[0] if len(values) else 0
    for v in values:
        out |= v ^ previous
        previous = v
    return out


# --- 書き手 ------------------------------------------------------------------------------------

class SegmentedRecorder:
    """長期の記録の書き手（Recorder と同じ口。スレッド安全）。clock は分割の時間と間引きの年齢に使う壁時計（秒）。"""

    def __init__(self, directory: Path, timebase_hz: float, meta: dict | None = None, policy: Policy | None = None,
                 clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        (self.directory / "segments").mkdir(parents=True, exist_ok=True)
        self.timebase_hz = timebase_hz
        self.policy = policy or Policy()
        self.grid = SummaryGrid.for_timebase(timebase_hz)
        self.clock = ClockMap(timebase_hz)
        self._now = clock
        self._meta = {**(meta or {}), "timebase_hz": timebase_hz, "format": 2, "layout": "segmented",
                      "policy": self.policy.to_dict(), "summary": {**self.grid.to_dict(), "index_level": INDEX_LEVEL}}
        self._write_meta()
        self._index = _connect(self.directory / "index.sqlite", _INDEX_SCHEMA)
        self._lock = threading.RLock()
        self._closed = False
        self._open_segment()

    # --- Recorder と同じ口 -----------------------------------------------------------------------

    def _write_meta(self) -> None:
        write_json(self.directory / "meta.json", self._meta)

    def update_meta(self, **items: Any) -> None:
        with self._lock:
            self._meta.update(items)
            self._write_meta()

    def add(self, stream: str, kind: str, t: int, a: int = 0, b: int = 0, body: Any = None,
            t_end: int | None = None, uncertainty: float | None = None) -> None:
        with self._lock:
            self._current.add(stream, kind, t, a, b, body, t_end, uncertainty)

    def add_many(self, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        with self._lock:
            self._current.add_many(rows)

    def add_host(self, stream: str, kind: str, host_s: float, **fields: Any) -> int:
        t, unc = self.clock.to_ticks(host_s)
        self.add(stream, kind, t, uncertainty=unc, **fields)
        return t

    def add_waveform(self, tr: Transitions, start_ticks: int) -> str:
        tps = tr.sample_period_s * self.timebase_hz
        with self._lock:
            name = self._current.add_waveform(tr, start_ticks)
            values = tr.values
            self._index.execute(
                "INSERT INTO wave VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (self._no, start_ticks, start_ticks + round(tr.length * tps), f"{self._path}/waveform/{name}", tps,
                 tr.channels, tr.length, values[0] if len(values) else 0, values[-1] if len(values) else 0,
                 _toggles(values)))
            self._wave_bytes += 10 * len(values) + 64
            return name

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._current.flush()
            if self._due():
                self._seal()
                self._open_segment()
                self._maintain()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._current.flush()
            self._seal()
            self._maintain()
            self._closed = True
            self._meta["closed"] = True
            self._write_meta()
            self._index.close()

    def _abandon(self) -> None:
        """試験用: 書きかけを捨てて閉じる。"""
        with self._lock:
            self._current._abandon()
            self._index.close()
            self._closed = True

    # --- 分割 -----------------------------------------------------------------------------------

    def _open_segment(self) -> None:
        no = self._index.execute("SELECT COALESCE(MAX(no) + 1, 0) FROM segments").fetchone()[0]
        self._no, self._path = no, f"segments/{no:06d}"
        self._current = Recorder(self.directory / self._path, self.timebase_hz, meta={"segment": no})
        self._opened = self._now()
        self._wave_bytes = 0
        self._index.execute("INSERT INTO segments (no, path, state, opened) VALUES (?, ?, 'open', ?)",
                            (no, self._path, self._opened))

    def _due(self) -> bool:
        if self._now() - self._opened >= self.policy.segment_s:
            return True
        if self.policy.segment_bytes is not None:
            db = self.directory / self._path / "recording.sqlite"
            size = sum(p.stat().st_size for p in (db, db.with_name("recording.sqlite-wal")) if p.exists())
            return size + self._wave_bytes >= self.policy.segment_bytes
        return False

    def _seal(self) -> None:
        """いまの分割を閉じ、縮図（細かい段は分割に、粗い段は索引に）と範囲・件数・大きさを書く。"""
        self._current.close()
        directory = self.directory / self._path
        db = _connect(directory / "recording.sqlite", _SEGMENT_SUMMARY)
        try:
            lo, hi, count = db.execute("SELECT MIN(t), MAX(COALESCE(t_end, t)), COUNT(*) FROM events").fetchone()
            clo, chi = db.execute("SELECT MIN(t0), MAX(t1) FROM chunks").fetchone()
            t0 = min((v for v in (lo, clo) if v is not None), default=None)
            t1 = max((v for v in (hi, chi) if v is not None), default=None)
            table = bin_counts(db, self.grid)
            db.execute("BEGIN")
            db.executemany("INSERT INTO summary VALUES (?, ?, ?, ?)",
                           [(level, s, b, n) for level in range(INDEX_LEVEL) for s, b, n in table.get(level, [])])
            db.execute("COMMIT")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        self._index.execute("BEGIN")
        self._index.executemany(
            "INSERT INTO summary VALUES (?, ?, ?, ?) ON CONFLICT (level, stream, bin) DO UPDATE SET count = count + excluded.count",
            [(level, s, b, n) for level in range(INDEX_LEVEL, self.grid.levels) for s, b, n in table.get(level, [])])
        self._index.execute("UPDATE segments SET state = 'sealed', t0 = ?, t1 = ?, events = ?, bytes = ?, sealed = ? "
                            "WHERE no = ?", (t0, t1, count, _bytes_of(directory), self._now(), self._no))
        self._index.execute("COMMIT")

    # --- 間引き・容量の上限 --------------------------------------------------------------------------

    def _maintain(self) -> None:
        p = self.policy
        newest = self._index.execute("SELECT MAX(no) FROM segments WHERE state != 'deleted'").fetchone()[0]
        if p.decimate_after_s is not None:
            old = self._index.execute("SELECT no, path FROM segments WHERE state = 'sealed' AND dropped = '[]' "
                                      "AND sealed <= ? ORDER BY no", (self._now() - p.decimate_after_s,)).fetchall()
            for no, path in old:
                self._decimate(no, path)
        if p.max_bytes is not None:
            rows = self._index.execute("SELECT no, path, bytes FROM segments WHERE state != 'deleted' ORDER BY no").fetchall()
            total = sum(r[2] for r in rows) + _bytes_of_index(self.directory)
            for no, path, size in rows:
                if total <= p.max_bytes or no == newest:
                    break
                if self._delete(no, path):
                    total -= size

    def _decimate(self, no: int, path: str) -> None:
        directory = self.directory / path
        streams = [s for s in self.policy.decimate_streams if s != WAVEFORM]
        try:
            db = _connect(directory / "recording.sqlite")
            try:
                if streams:
                    db.execute(f"DELETE FROM events WHERE stream IN ({','.join('?' * len(streams))})", streams)
                if WAVEFORM in self.policy.decimate_streams:
                    for name, in db.execute("SELECT path FROM chunks").fetchall():
                        (directory / "waveform" / name).unlink(missing_ok=True)
                    db.execute("DELETE FROM chunks")
                db.execute("VACUUM")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                remaining = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                db.close()
        except (OSError, sqlite3.OperationalError):              # 読み手が開いている等: 次の機会に
            return
        if WAVEFORM in self.policy.decimate_streams:
            self._index.execute("UPDATE wave SET kept = 0 WHERE segment = ?", (no,))
        self._index.execute("UPDATE segments SET dropped = ?, events = ?, bytes = ? WHERE no = ?",
                            (json.dumps(list(self.policy.decimate_streams)), remaining, _bytes_of(directory), no))

    def _delete(self, no: int, path: str) -> bool:
        try:
            shutil.rmtree(self.directory / path)
        except OSError:                                            # 読み手が開いている等（Windows）: 次の機会に
            return False
        self._index.execute("UPDATE segments SET state = 'deleted', bytes = 0 WHERE no = ?", (no,))
        self._index.execute("UPDATE wave SET kept = 0 WHERE segment = ?", (no,))
        return True


def _bytes_of_index(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.glob("index.sqlite*") if p.is_file())


# --- 読み手 ------------------------------------------------------------------------------------

@dataclass
class _Segment:
    no: int
    path: str
    state: str
    t0: int | None
    t1: int | None
    events: int
    bytes: int
    dropped: list[str] = field(default_factory=list)

    def overlaps(self, t0: int | None, t1: int | None) -> bool:
        if self.t0 is None:
            return False
        return (t0 is None or self.t1 >= t0) and (t1 is None or self.t0 < t1)

    def to_dict(self) -> dict:
        return asdict(self)


class _Reads:
    """1 回の読みの間だけ分割の接続を開いておく（Windows で書き手が古い分割を消せるよう、読み終えたら閉じる）。"""

    def __init__(self, root: Path):
        self.root = root
        self._dbs: dict[int, sqlite3.Connection] = {}

    def db(self, seg: _Segment) -> sqlite3.Connection:
        if seg.no not in self._dbs:
            self._dbs[seg.no] = _connect(self.root / seg.path / "recording.sqlite")
        return self._dbs[seg.no]

    def close(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()


class SegmentedRecording(Recording):
    """長期の記録の読み手（Recording と同じ口）。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
        self.grid = SummaryGrid.from_dict(self.meta["summary"])
        self.index_level = int(self.meta["summary"].get("index_level", INDEX_LEVEL))
        self._index = _connect(self.directory / "index.sqlite", _INDEX_SCHEMA)
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
                db = _connect(self.directory / seg.path / "recording.sqlite")
                try:
                    lo, hi, n = db.execute("SELECT MIN(t), MAX(COALESCE(t_end, t)), COUNT(*) FROM events").fetchone()
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

    def iter_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                    kinds: list[str] | None = None, limit: int | None = None,
                    overlapping: bool = False) -> Iterator[Event]:
        """範囲に重なる分割だけを読み、時刻順に返す（時刻の重なる分割どうしは混ぜて並べる）。"""
        segs = sorted((s for s in self._live() if overlapping and t0 is not None and s.t0 is not None
                       and (t1 is None or s.t0 < t1) or s.overlaps(t0, t1)), key=lambda s: s.t0)
        groups: list[list[_Segment]] = []
        for s in segs:
            if groups and s.t0 <= max(g.t1 for g in groups[-1]):
                groups[-1].append(s)
            else:
                groups.append([s])

        def generate() -> Iterator[Event]:
            for group in groups:
                readers = [Recording(self.directory / s.path) for s in group]
                try:
                    its = [r.iter_events(t0, t1, streams, kinds, None, overlapping) for r in readers]
                    yield from heapq.merge(*its, key=lambda e: e.t)
                finally:
                    for r in readers:
                        r.close()
        it = generate()
        return itertools.islice(it, limit) if limit is not None else it

    def count(self) -> int:
        return sum(s.events for s in self._live())

    def distinct(self, column: str, streams: list[str] | None = None) -> list:
        values: set = set()
        for s in self._live():
            r = Recording(self.directory / s.path)
            try:
                values.update(r.distinct(column, streams))
            finally:
                r.close()
        return sorted(values)

    def span(self) -> tuple[int, int] | None:
        segs = [s for s in self._live() if s.t0 is not None]
        if not segs:
            return None
        return min(s.t0 for s in segs), max(s.t1 for s in segs)

    # --- 件数（縮図） --------------------------------------------------------------------------

    def _counts(self, reads: _Reads, segs: list[_Segment], t0: int, t1: int,
                streams: list[str] | None, inexact: list[bool]) -> dict[str, int]:
        """[t0, t1) の stream ごとの件数。閉じた分割は縮図の和、開いている分割は生で数える。"""
        sealed = [s for s in segs if s.state == "sealed"]
        deleted = [s for s in segs if s.state == "deleted"]
        where_streams = f" AND stream IN ({','.join('?' * len(streams))})" if streams else ""
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
                inexact[0] = True
            return out

        def raw_count(a: int, b: int, _streams) -> dict[str, int]:
            out: dict[str, int] = {}
            for seg in sealed:
                if not seg.overlaps(a, b):
                    continue
                db = reads.db(seg)
                dropped = [s for s in seg.dropped if s != WAVEFORM and (not streams or s in streams)]
                keep = f" AND stream NOT IN ({','.join('?' * len(dropped))})" if dropped else ""
                add(out, db.execute(f"SELECT stream, COUNT(*) FROM events WHERE t >= ? AND t < ?{where_streams}{keep} "
                                    "GROUP BY stream", [a, b, *args_streams, *dropped]))
                if dropped:                                        # 間引いた stream: 段 0 の件数を幅で按分（近似）
                    w0 = self.grid.w0
                    bin_ = a // w0
                    for s, n in db.execute(f"SELECT stream, count FROM summary WHERE level = 0 AND bin = ? AND stream IN "
                                           f"({','.join('?' * len(dropped))})", [bin_, *dropped]):
                        out[s] = out.get(s, 0) + round(n * (b - a) / w0)
                        inexact[0] = True
            if any(seg.overlaps(a, b) for seg in deleted):
                inexact[0] = True
            return out

        out = self.grid.count(t0, t1, bins_count, raw_count, streams)
        for seg in segs:
            if seg.state == "open" and seg.overlaps(t0, t1):
                add(out, reads.db(seg).execute(
                    f"SELECT stream, COUNT(*) FROM events WHERE t >= ? AND t < ?{where_streams} GROUP BY stream",
                    [t0, t1, *args_streams]))
        return out

    def density(self, t0: int, t1: int, width: int, streams: list[str] | None = None) -> list[int]:
        """画素ごとの出来事の件数（縮図の和。正確。間引いた・消した部分に端がかかると近似: coverage で分かる）。"""
        if t1 <= t0 or width < 1:
            raise ValueError("要約の区間が空")
        segs = self._all_with_ranges()
        span, out = t1 - t0, []
        inexact = [False]
        with self._reads() as reads:
            for px in range(width):
                p0, p1 = t0 + px * span // width, t0 + (px + 1) * span // width
                out.append(sum(self._counts(reads, segs, p0, p1, streams, inexact).values()) if p1 > p0 else 0)
        return out

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
        where = f" AND stream IN ({','.join('?' * len(streams))})" if streams else ""
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

    def wave_chunks(self, t0: int, t1: int) -> list[tuple[_Chunk, bool]]:
        return [(_Chunk(*r[:6]), bool(r[9])) for r in self._wave_rows(t0, t1, kept_only=False)]

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
        where = f" AND stream IN ({','.join('?' * len(streams))})" if streams else ""
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

    def _chunks(self, t0: int | None = None, t1: int | None = None) -> list[_Chunk]:
        return [_Chunk(*r[:6]) for r in self._wave_rows(t0, t1, kept_only=True)]

    def _chunk_file(self, chunk: _Chunk) -> Path:
        return self.directory / chunk.path

    def _channel_count(self) -> int:
        return self._index.execute("SELECT COALESCE(MAX(channels), 0) FROM wave").fetchone()[0]

    def waveform_view(self, t0: int, t1: int, width: int) -> list[str]:
        """Recording.waveform_view と同じ（'0' / '1' / 'x' / '-'）。画素が丸ごと含むかたまりは事前計算の要約
        （最初の値・変化したチャンネル）で、端のかかるかたまりはファイルを読んで正確に。ファイルを間引いたかたまりは
        要約から（かたまりの中で変化したチャンネルは 'x'）。"""
        if t1 <= t0 or width < 1:
            raise ValueError("要約の区間が空")
        channels = self._channel_count()
        full = (1 << channels) - 1
        highs, lows = [0] * width, [0] * width
        span = t1 - t0
        for row in self._wave_rows(t0, t1, kept_only=False):
            c = _Chunk(*row[:6])
            first, toggles, kept = row[6], row[8], row[9]
            px_lo = max(0, (max(c.t0, t0) - t0) * width // span)
            px_hi = min(width - 1, (min(c.t1, t1) - t0) * width // span)
            for px in range(px_lo, px_hi + 1):
                p0, p1 = t0 + px * span // width, t0 - (-(px + 1) * span // width)
                if c.t1 <= p0 or c.t0 >= p1:
                    continue
                if (c.t0 >= p0 and c.t1 <= p1) or not kept:
                    highs[px] |= first | toggles
                    lows[px] |= (~first | toggles) & full
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
            for h, l in zip(highs, lows):
                hi, lo = h >> ch & 1, l >> ch & 1
                chars.append("x" if hi and lo else "1" if hi else "0" if lo else NO_DATA)
            rows.append("".join(chars))
        return rows
