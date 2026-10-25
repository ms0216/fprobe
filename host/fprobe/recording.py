"""共通の時間軸の記録（F3）: fprobe が観測する出来事を、デバイスのタイムベースの上の 1 つの記録にまとめる。

仕様: docs/superpowers/specs/2026-10-17-f3-timeline-recording-design.md
- 時刻は整数のタイムベース（デバイスの 64 ビットのカウンタ）。ホストで起きた出来事は `ClockMap` で写し、不確かさを持つ。
- 1 回の記録 = 1 つのディレクトリ。索引と小さな出来事は SQLite（recording.sqlite）、波形は追記だけのかたまりのファイル
  （waveform/）、記録の情報は meta.json。
- 書き手 `Recorder` はまとめて書き（flush）、読み手 `Recording` は記録中でも読める（SQLite の WAL）。
"""

import csv
import heapq
import json
import os
import re
import sqlite3
import statistics
import threading
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .la import Transitions, Waveform, vcd_blocks

__all__ = ["ClockMap", "Event", "Recorder", "Recording"]

CLOCK_WINDOW = 64                 # 時計の対応付けに使う最近の点の数
NO_DATA = "-"                     # 波形の要約: その時間に波形が無い

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    t INTEGER NOT NULL, t_end INTEGER, stream TEXT NOT NULL, kind TEXT NOT NULL,
    a INTEGER NOT NULL DEFAULT 0, b INTEGER NOT NULL DEFAULT 0, body TEXT, unc REAL);
CREATE INDEX IF NOT EXISTS events_t ON events (t);
CREATE INDEX IF NOT EXISTS events_stream_t ON events (stream, t);
CREATE INDEX IF NOT EXISTS events_stream_kind_t ON events (stream, kind, t);
CREATE INDEX IF NOT EXISTS events_t_end ON events (t_end) WHERE t_end IS NOT NULL;
CREATE TABLE IF NOT EXISTS chunks (
    t0 INTEGER NOT NULL, t1 INTEGER NOT NULL, path TEXT NOT NULL, ticks_per_sample REAL NOT NULL,
    channels INTEGER NOT NULL, length INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_t ON chunks (t0, t1);
"""


def write_json(path: Path, obj: Any) -> None:
    """JSON を一時ファイルに書いてから置き換える（途中で失敗しても元のファイルは壊れない）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ClockMap:
    """ホストの単調時計（秒）→ デバイスのタイムベース。読みの往復の中点とタイムベースの組を直線で当てはめる。"""

    def __init__(self, timebase_hz: float):
        self.hz = timebase_hz
        self._points: list[tuple[float, int, float]] = []

    def add(self, host_s: float, ticks: int, round_trip_s: float) -> None:
        """host_s は読みの前後のホストの時刻の中点、round_trip_s は読みの往復時間。"""
        self._points.append((host_s, ticks, round_trip_s))
        del self._points[:-CLOCK_WINDOW]

    def to_ticks(self, host_s: float) -> tuple[int, float]:
        """(タイムベース, 不確かさ（タイムベースの単位）)。点の誤りの上限 u = 往復の半分（ホストの時刻の中点の誤り）。
        - 2 点以上（ホストの時刻に幅がある）: 最小二乗の直線。不確かさ = u + 傾きの誤りの上限 × |host_s − 点の平均|。
          傾きの誤りの上限 = u Σ|x − x̄| / Σ(x − x̄)²（各点の誤りが u 以下のときの最小二乗の傾きの最大のずれ）。
          点の幅が狭いほど、遠い時刻ほど大きい（幅の境界の値で公称の速さに切り替えない）。
        - 1 点（幅が 0）: 速さが分からないので公称の速さで写し、不確かさに点からの経過時間をそのまま入れる
          （速さの誤りの上限を仮定しない）。"""
        if not self._points:
            raise RuntimeError("時計の対応付けの点が無い（記録を始める前のホストの出来事は置けない）")
        xs, ys = [p[0] for p in self._points], [p[1] for p in self._points]
        u = max(p[2] for p in self._points) / 2 * self.hz
        mx = statistics.fmean(xs)
        spread = sum((x - mx) ** 2 for x in xs)
        if spread <= 0:                                              # 1 点: 速さが分からない
            x0, y0 = xs[-1], ys[-1]
            return round(y0 + (host_s - x0) * self.hz), u + abs(host_s - x0) * self.hz
        my = statistics.fmean(ys)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / spread
        slope_bound = u * sum(abs(x - mx) for x in xs) / spread
        return round(my + (host_s - mx) * slope), u + slope_bound * abs(host_s - mx)


@dataclass(frozen=True)
class Event:
    t: int
    stream: str
    kind: str
    a: int = 0
    b: int = 0
    body: Any = None
    t_end: int | None = None
    uncertainty: float | None = None          # ホストで起きた出来事の時刻の不確かさ（タイムベース）


@dataclass(frozen=True)
class _Chunk:
    t0: int
    t1: int
    path: str
    ticks_per_sample: float
    channels: int
    length: int


def in_list(column: str, values: Iterable, negate: bool = False) -> str:
    """SQL の `column IN (?, ?, ...)`（negate なら NOT IN）。値は呼び出し側が同じ順で渡す。"""
    return f"{column} {'NOT ' if negate else ''}IN ({','.join('?' * len(list(values)))})"


def connect_db(path: Path, schema: str = "", timeout: float = 5.0) -> sqlite3.Connection:
    """記録の SQLite を開く（WAL: 書いている間も読める・落ちても壊れない）。schema があれば表を作る。"""
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=timeout)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if schema:
        db.executescript(schema)
    return db


def _connect(path: Path) -> sqlite3.Connection:
    return connect_db(path, _SCHEMA)


class HostEvents:
    """書き手の共通の口: ホストで起きた出来事を時計の対応付け（self.clock）で写して add する。"""

    def add_host(self, stream: str, kind: str, host_s: float, **fields: Any) -> int:
        """ホストで起きた出来事: ホストの時刻を時計の対応付けで写し、不確かさを付けて入れる。写した時刻を返す。"""
        t, unc = self.clock.to_ticks(host_s)
        self.add(stream, kind, t, uncertainty=unc, **fields)
        return t


class Recorder(HostEvents):
    """記録の書き手。add / add_many / add_waveform を溜め、flush でまとめて書く（スレッド安全）。"""

    def __init__(self, directory: Path, timebase_hz: float, meta: dict | None = None):
        self.directory = Path(directory)
        (self.directory / "waveform").mkdir(parents=True, exist_ok=True)
        self.timebase_hz = timebase_hz
        self.clock = ClockMap(timebase_hz)
        self._meta = {**(meta or {}), "timebase_hz": timebase_hz, "format": 1}
        self._write_meta()
        self._db = _connect(self.directory / "recording.sqlite")
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._chunk_no = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        self._closed = False

    def _write_meta(self) -> None:
        write_json(self.directory / "meta.json", self._meta)

    def update_meta(self, **items: Any) -> None:
        self._meta.update(items)
        self._write_meta()

    def add(self, stream: str, kind: str, t: int, a: int = 0, b: int = 0, body: Any = None,
            t_end: int | None = None, uncertainty: float | None = None) -> None:
        row = (t, t_end, stream, kind, a, b, None if body is None else json.dumps(body, ensure_ascii=False), uncertainty)
        with self._lock:
            self._pending.append(row)

    def add_many(self, rows: Iterable[tuple]) -> None:
        """(stream, kind, t[, a, b, body, t_end, uncertainty]) の列。"""
        out = []
        for r in rows:
            stream, kind, t, a, b, body, t_end, unc = (*r, *(0, 0, None, None, None)[len(r) - 3:])
            out.append((t, t_end, stream, kind, a, b, None if body is None else json.dumps(body, ensure_ascii=False), unc))
        with self._lock:
            self._pending.extend(out)

    def add_waveform(self, tr: Transitions, start_ticks: int) -> str:
        """ロジアナの変化点（サンプル 0 がタイムベース start_ticks）を、かたまりのファイルとして入れる。
        ファイルの名前（waveform/ の中）を返す。"""
        tps = tr.sample_period_s * self.timebase_hz
        with self._lock:
            name = f"{self._chunk_no:06d}.bin"
            self._chunk_no += 1
            header = json.dumps({"count": len(tr.values), "length": tr.length, "channels": tr.channels,
                                 "trigger": tr.trigger, "period_s": tr.sample_period_s}).encode() + b"\n"
            with (self.directory / "waveform" / name).open("wb") as f:
                f.write(header)
                f.write(array("H", tr.values).tobytes())
                f.write(array("Q", tr.starts).tobytes())
            self._db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
                             (start_ticks, start_ticks + round(tr.length * tps), name, tps, tr.channels, tr.length))
        return name

    def flush(self) -> None:
        with self._lock:
            rows, self._pending = self._pending, []
            if rows:
                self._db.execute("BEGIN")
                self._db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                self._db.execute("COMMIT")

    def close(self) -> None:
        if not self._closed:
            self.flush()
            self._db.close()
            self._closed = True

    def _abandon(self) -> None:
        """試験用: 書きかけを捨てて閉じる（落ちた場合の模擬）。"""
        with self._lock:
            self._pending = []
        self._db.close()
        self._closed = True


def _vcd_id(i: int) -> str:
    """VCD の識別子（印字できる ASCII 94 字の並び）。"""
    out = chr(33 + i % 94)
    while i >= 94:
        i = i // 94 - 1
        out = chr(33 + i % 94) + out
    return out


WAVE_CACHE_BYTES = 256 << 20          # 記録の読み手が覚えておく変化点の表の大きさの上限（使った順に捨てる）


class Recording:
    """記録の読み手（記録中でも読める）。時刻はタイムベース、t1 は含まない。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
        self._db = _connect(self.directory / "recording.sqlite")
        self._init_waves()

    def _init_waves(self) -> None:
        """かたまりの変化点の表を使った順に覚える（WAVE_CACHE_BYTES まで。超えたら古いものから捨てる）。"""
        self._waves: OrderedDict[str, Waveform] = OrderedDict()
        self._distinct: dict = {}                                       # distinct の答え（rowid の最大と）
        self._wave_bytes = 0
        self._wave_lock = threading.Lock()

    def update_meta(self, **items) -> None:
        """記録の情報を書き換える（終わった記録でも。例: FPGA の信号の定義 fpga.signals を後から直す）。"""
        path = self.directory / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta.update(items)
        write_json(path, meta)
        self.meta.update(items)

    @classmethod
    def open(cls, directory: Path) -> "Recording":
        """記録を開く。長期の記録（分割した記録。fprobe.recording_long）ならその読み手を返す。"""
        meta = json.loads((Path(directory) / "meta.json").read_text(encoding="utf-8"))
        if meta.get("layout") == "segmented":
            from .recording_long import SegmentedRecording
            return SegmentedRecording(directory)
        return cls(directory)

    def coverage(self, t0: int | None = None, t1: int | None = None) -> dict:
        """範囲の出来事・波形が欠けずに残っているか（長期の記録は間引き・消した部分を返す）。"""
        return {"complete": True, "gaps": []}

    def close(self) -> None:
        self._db.close()

    # --- 出来事 --------------------------------------------------------------------

    def events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
               kinds: list[str] | None = None, limit: int | None = None) -> list[Event]:
        return list(self.iter_events(t0, t1, streams, kinds, limit))

    def iter_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                    kinds: list[str] | None = None, limit: int | None = None,
                    overlapping: bool = False, a: int | None = None) -> Iterator[Event]:
        """events と同じ（時刻順）だが、読みながら返す（大きな記録の書き出し用）。overlapping = True なら、t0 より前に
        始まって t0 より後に終わる区間（t_end を持つ出来事）も含める。a を渡せば a が一致する出来事だけ（SQL で絞る）。"""
        sql, args = self._where(t0, t1, streams, kinds, overlapping, a)
        sql = f"SELECT t, stream, kind, a, b, body, t_end, unc FROM events {sql} ORDER BY t, rowid"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        for t, s, k, a, b, body, te, unc in self._db.execute(sql, args):
            yield Event(t, s, k, a, b, None if body is None else json.loads(body), te, unc)

    @staticmethod
    def _where(t0, t1, streams, kinds, overlapping: bool = False, a: int | None = None) -> tuple[str, list]:
        clauses, args = [], []
        if t0 is not None:
            clauses.append("(t >= ? OR t_end > ?)" if overlapping else "t >= ?")
            args.extend([t0, t0] if overlapping else [t0])
        if t1 is not None:
            clauses.append("t < ?")
            args.append(t1)
        for column, values in (("stream", streams), ("kind", kinds)):
            if values:
                clauses.append(in_list(column, values))
                args.extend(values)
        if a is not None:
            clauses.append("a = ?")
            args.append(a)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", args

    def nearest(self, t: int, before: bool, stream: str, kind: str | None = None, a: int | None = None,
                body: dict[str, Any] | None = None) -> Event | None:
        """t の直前（before: 時刻 ≦ t のいちばん後）か直後（時刻 > t のいちばん前）の出来事 1 つ（無ければ None）。
        stream・kind・a と body の欄の値（{欄: 値}。一致）で絞る。同じ時刻のものは時刻順の並び（入れた順）で t に近い側。"""
        clauses, args = ["stream = ?", "t <= ?" if before else "t > ?"], [stream, t]
        if kind is not None:
            clauses.append("kind = ?")
            args.append(kind)
        if a is not None:
            clauses.append("a = ?")
            args.append(a)
        for key, value in (body or {}).items():
            if not re.fullmatch(r"\w+", key):
                raise ValueError(f"body の欄 {key!r} は英数字と _ だけ")
            clauses.append(f"json_extract(body, '$.{key}') = ?")
            args.append(value)
        order = "DESC" if before else "ASC"
        row = self._db.execute(f"SELECT t, stream, kind, a, b, body, t_end, unc FROM events WHERE {' AND '.join(clauses)} "
                               f"ORDER BY t {order}, rowid {order} LIMIT 1", args).fetchone()
        if row is None:
            return None
        t0, s, k, a0, b0, raw, te, unc = row
        return Event(t0, s, k, a0, b0, None if raw is None else json.loads(raw), te, unc)

    def add(self, stream: str, kind: str, t: int, a: int = 0, b: int = 0, body: Any = None,
            t_end: int | None = None) -> None:
        """閉じた記録に出来事を 1 つ足す（後から置く調査の印。recording_mark の t）。"""
        row = (t, t_end, stream, kind, a, b, None if body is None else json.dumps(body, ensure_ascii=False), None)
        self._db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)

    def replace_derived(self, stream: str, source: str, events: Iterable[Event]) -> int:
        """解析が作った出来事（派生イベント。例: 規則の違反）を書く。同じ stream で body の "source" が同じものを先に
        消す（検査し直すと前の結果が残らない）。各出来事の body（辞書）に source を入れる。書いた件数を返す。"""
        rows = []
        for e in events:
            body = {**(e.body or {}), "source": source}
            rows.append((e.t, e.t_end, stream, e.kind, e.a, e.b, json.dumps(body, ensure_ascii=False), e.uncertainty))
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("DELETE FROM events WHERE stream = ? AND json_extract(body, '$.source') = ?",
                             (stream, source))
            self._db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._distinct.clear()                      # 消したので distinct の覚えは古い（rowid の最大では消したことが分からない）
        return len(rows)

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def count_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                     kinds: list[str] | None = None) -> int:
        """events（limit なし）が返す出来事の数。"""
        sql, args = self._where(t0, t1, streams, kinds)
        return self._db.execute(f"SELECT COUNT(*) FROM events {sql}", args).fetchone()[0]

    def distinct(self, column: str, streams: list[str] | None = None) -> list:
        """欄（stream / kind / a / b）の異なる値の一覧（例: サンプラが読んだ番地 = distinct("a", ["sample"])）。"""
        if column not in ("stream", "kind", "a", "b"):
            raise ValueError(f"欄 {column!r} は stream / kind / a / b")
        # 記録が増えていなければ前の答え（全件を読むので。増えたかは rowid の最大 = 索引で一瞬）
        newest, = self._db.execute("SELECT MAX(rowid) FROM events").fetchone()
        key = (column, tuple(streams or ()), newest)
        cached = self._distinct.get(key[:2])
        if cached is not None and cached[0] == newest:
            return list(cached[1])
        where, args = self._where(None, None, streams, None)
        values = [v for v, in self._db.execute(f"SELECT DISTINCT {column} FROM events {where} ORDER BY {column}", args)]
        self._distinct[key[:2]] = (newest, values)
        return list(values)

    def find(self, query: dict) -> list[Event]:
        """構造化した条件に一致する出来事。query: stream / kind（文字列か列）、t0 / t1、a / b（値か {"eq"|"ne"|"lt"|"le"|
        "gt"|"ge": 値}）、body（{欄: 値か {"regex": 正規表現}}）。"""
        def listed(v):
            return None if v is None else [v] if isinstance(v, str) else list(v)
        events = self.events(query.get("t0"), query.get("t1"), listed(query.get("stream")), listed(query.get("kind")))
        ops = {"eq": lambda x, y: x == y, "ne": lambda x, y: x != y, "lt": lambda x, y: x < y,
               "le": lambda x, y: x <= y, "gt": lambda x, y: x > y, "ge": lambda x, y: x >= y}

        def number_ok(value: int, cond) -> bool:
            if cond is None:
                return True
            if not isinstance(cond, dict):
                return value == cond
            return all(ops[op](value, ref) for op, ref in cond.items())

        def body_ok(body, cond) -> bool:
            if not cond:
                return True
            if not isinstance(body, dict):
                return False
            for name, want in cond.items():
                have = body.get(name)
                if isinstance(want, dict) and "regex" in want:
                    if have is None or not re.search(want["regex"], str(have)):
                        return False
                elif have != want:
                    return False
            return True
        return [e for e in events
                if number_ok(e.a, query.get("a")) and number_ok(e.b, query.get("b")) and body_ok(e.body, query.get("body"))]

    def intervals(self, start: dict, end: dict) -> dict:
        """開始の条件から、次に来る終了の条件までの時間（タイムベース）の一覧と統計。"""
        starts, ends = self.find(start), self.find(end)
        pairs, j = [], 0
        for i, s in enumerate(starts):
            limit = starts[i + 1].t if i + 1 < len(starts) else None
            while j < len(ends) and ends[j].t < s.t:
                j += 1
            if j < len(ends) and (limit is None or ends[j].t <= limit):
                pairs.append((s.t, ends[j].t - s.t))
                j += 1
        durations = [d for _, d in pairs]
        stats: dict[str, Any] = {"intervals": pairs, "count": len(durations)}
        if durations:
            stats.update(min=min(durations), max=max(durations), mean=statistics.fmean(durations),
                         stdev=statistics.pstdev(durations))
        return stats

    def kind_counts(self, t0: int | None = None, t1: int | None = None) -> list[tuple[str, str, int, int, int]]:
        """範囲 [t0, t1) の stream・kind ごとの (stream, kind, 件数, 最初の時刻, 最後の時刻)。stream・kind の順。"""
        where, args = self._where(t0, t1, None, None)
        sql = f"SELECT stream, kind, COUNT(*), MIN(t), MAX(t) FROM events {where} GROUP BY stream, kind ORDER BY stream, kind"
        return [tuple(row) for row in self._db.execute(sql, args)]

    def density_bounds(self, t0: int, t1: int, width: int, streams: list[str] | None = None) -> dict:
        """density と同じ件数と、数えられない画素（普通の記録は全部数えられるので inexact は空。長期の記録と同じ形）。"""
        return {"counts": self.density(t0, t1, width, streams), "inexact": []}

    def density(self, t0: int, t1: int, width: int, streams: list[str] | None = None) -> list[int]:
        """画素ごとの出来事の件数（縮図・タイムラインの要約）。"""
        if t1 <= t0 or width < 1:
            raise ValueError("要約の区間が空")
        # 画素ごとに範囲の件数を索引（stream, t）で数える（式でまとめると並べ直しが要り、100 万件で遅い）
        span, out = t1 - t0, []
        for px in range(width):
            p0, p1 = t0 + px * span // width, t0 + (px + 1) * span // width
            where, args = self._where(p0, p1, streams, None)
            out.append(self._db.execute(f"SELECT COUNT(*) FROM events {where}", args).fetchone()[0])
        return out

    # --- 波形 ----------------------------------------------------------------------

    def _chunks(self, t0: int | None = None, t1: int | None = None) -> list[_Chunk]:
        sql, clauses, args = "SELECT t0, t1, path, ticks_per_sample, channels, length FROM chunks", [], []
        if t0 is not None:
            clauses.append("t1 > ?")
            args.append(t0)
        if t1 is not None:
            clauses.append("t0 < ?")
            args.append(t1)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [_Chunk(*row) for row in self._db.execute(sql + " ORDER BY t0", args)]

    def waveforms(self, t0: int | None = None, t1: int | None = None) -> Iterator[tuple[int, float, Transitions]]:
        """範囲 [t0, t1) に重なる波形のかたまりを時刻順に: (サンプル 0 のタイムベース, 1 サンプルのタイムベース, 変化点)。
        かたまりは丸ごと返す（範囲で切るのは使う側）。"""
        for c in self._chunks(t0, t1):
            yield c.t0, c.ticks_per_sample, self._transitions(c)

    def _transitions(self, chunk: _Chunk) -> Transitions:
        data = self._chunk_file(chunk).read_bytes()
        newline = data.index(b"\n")
        head = json.loads(data[:newline])
        n = head["count"]
        values = array("H")
        values.frombytes(data[newline + 1:newline + 1 + 2 * n])
        starts = array("Q")
        starts.frombytes(data[newline + 1 + 2 * n:newline + 1 + 10 * n])
        return Transitions(values, starts, head["length"], head["trigger"], 0, head["period_s"], head["channels"])

    def wave_chunks(self, t0: int, t1: int) -> list[tuple[_Chunk, bool]]:
        """範囲に重なる波形のかたまりと、ファイルが残っているか（長期の記録は間引くと False）。時刻順。"""
        return [(c, True) for c in self._chunks(t0, t1)]

    def waveform(self, chunk: _Chunk) -> Waveform:
        """かたまりの変化点の表（読み手が覚えておく）。"""
        return self._wave(chunk)

    def event_bins(self, start: int, width: int, count: int, streams: list[str] | None = None) -> dict[str, array]:
        """stream ごとの、幅 width の区間 count 個（start から）の出来事の件数（t が区間に入るもの）。
        区間の境が density の画素の境と同じなら、結果は density と同じ（1 回の問い合わせで数える）。"""
        if width < 1 or count < 1:
            raise ValueError("区間の幅と数は 1 以上")
        where = f" AND {in_list('stream', streams)}" if streams else ""
        out: dict[str, array] = {}
        for stream, b, n in self._db.execute(
                f"SELECT stream, (t - ?) / ?, COUNT(*) FROM events WHERE t >= ? AND t < ?{where} GROUP BY 1, 2",
                [start, width, start, start + width * count, *(streams or [])]):
            out.setdefault(stream, array("I", [0]) * count)[b] += n
        return out

    def query_events(self, t0: int, t1: int, queries: list[tuple[str, list]]) -> list[tuple]:
        """出来事の表への読みの問い合わせ（集計）の行をまとめて返す（長期の記録は [t0, t1) に重なる分割ごとの行を
        並べる。集計は分割ごとなので、呼び手が足し合わせる）。共通タイムラインの段の要約（fprobe.timeline_rows）用。"""
        return [row for sql, args in queries for row in self._db.execute(sql, args)]

    def _wave(self, chunk: _Chunk) -> Waveform:
        with self._wave_lock:
            wave = self._waves.get(chunk.path)
            if wave is not None:
                self._waves.move_to_end(chunk.path)
                return wave
        wave = Waveform(self._transitions(chunk))                     # 作るのはロックの外で（数百 ms かかる）
        with self._wave_lock:
            if chunk.path in self._waves:                              # ほかのスレッドが先に作った
                return self._waves[chunk.path]
            self._waves[chunk.path] = wave
            self._wave_bytes += wave.nbytes
            while len(self._waves) > 1 and self._wave_bytes > WAVE_CACHE_BYTES:
                _, old = self._waves.popitem(last=False)
                self._wave_bytes -= old.nbytes
        return wave

    def waveform_view(self, t0: int, t1: int, width: int) -> list[str]:
        """範囲の波形を画素ごとに '0' / '1' / 'x'（変化）/ '-'（波形が無い）。かたまりをまたぐ。"""
        if t1 <= t0 or width < 1:
            raise ValueError("要約の区間が空")
        chunks = self._chunks(t0, t1)
        channels = self._channel_count()
        rows = [[NO_DATA] * width for _ in range(channels)]
        span = t1 - t0
        # ponytail: 画素ごと・かたまりごとに Waveform.view を呼ぶ（数千画素で十分速い）。遅ければかたまりごとにまとめる
        for px in range(width):
            p0, p1 = t0 + px * span // width, t0 - (-(px + 1) * span // width)
            for c in chunks:
                if c.t1 <= p0 or c.t0 >= p1:
                    continue
                s0 = max(0, int((p0 - c.t0) / c.ticks_per_sample))
                s1 = min(c.length, max(s0 + 1, int(-(-(p1 - c.t0) // c.ticks_per_sample))))
                levels = self._wave(c).view(s0, s1, 1)
                for ch, level in enumerate(levels):
                    have = rows[ch][px]
                    rows[ch][px] = level[0] if have in (NO_DATA, level[0]) else "x"
        return ["".join(r) for r in rows]

    def channel_count(self) -> int:
        """波形のチャンネルの数（波形が無ければ 0）。"""
        return self._channel_count()

    def _channel_count(self) -> int:
        return self._db.execute("SELECT COALESCE(MAX(channels), 0) FROM chunks").fetchone()[0]

    def _chunk_file(self, chunk: _Chunk) -> Path:
        return self.directory / "waveform" / chunk.path

    def span(self) -> tuple[int, int] | None:
        """記録の最初と最後の時刻（波形のかたまりと出来事）。"""
        # 索引で（MAX(COALESCE(t_end, t)) は全件を読む: 114 万件で 0.2 秒、タイルごとに何度も呼ばれる）
        lo, = self._db.execute("SELECT MIN(t) FROM events").fetchone()      # 1 文に 1 つ（並べると索引を使わない）
        hi, = self._db.execute("SELECT MAX(t) FROM events").fetchone()
        hi_end, = self._db.execute("SELECT MAX(t_end) FROM events WHERE t_end IS NOT NULL").fetchone()
        hi = hi_end if hi is None or (hi_end is not None and hi_end > hi) else hi
        clo, chi = self._db.execute("SELECT MIN(t0), MAX(t1) FROM chunks").fetchone()
        values_lo = [v for v in (lo, clo) if v is not None]
        values_hi = [v for v in (hi, chi) if v is not None]
        return (min(values_lo), max(values_hi)) if values_lo else None

    # --- 書き出し --------------------------------------------------------------------

    def export_vcd(self, path: Path, channels: list[int] | None = None) -> None:
        """波形と FPGA の観測の信号を VCD に（時刻は ns。タイムベースは 10 の冪とは限らないため）。FPGA の信号は記録の
        信号の定義（fpga.signals）で信号ごとに `fpga_<組>_<名前>`、定義の無い組は 16 ビットの `fpga_<組>`。
        変化は波形と FPGA の出来事を時刻順に流しながら合わせて書く（長い記録でも全部をメモリに載せない）。"""
        from .timeline_fpga import _lanes, signals_of           # timeline_fpga はこのモジュールを読む

        hz = self.meta["timebase_hz"]
        chunks = self._chunks()
        count = max((c.channels for c in chunks), default=0)
        wanted = channels if channels is not None else list(range(count))
        ids = {ch: _vcd_id(i) for i, ch in enumerate(wanted)}
        lines = ["$timescale 1 ns $end", "$scope module fprobe $end"]
        lines += [f"$var wire 1 {ids[ch]} ch{ch} $end" for ch in wanted]
        groups = sorted({e.a for e in self.iter_events(streams=["fpga"], kinds=["value"])})
        plan: dict[int, list[tuple[str, int, Any]]] = {}
        if groups:
            signals = signals_of(self)
            used_names = {f"ch{ch}" for ch in wanted}
            for g in groups:
                plan[g] = []
                for _kind, row, _title, sig in _lanes(signals, g):
                    # VCD は ASCII: 英数字と _ だけにし、置き換えて同じになった名前は _2・_3… で分ける
                    base = name = "fpga_" + re.sub(r"[^A-Za-z0-9_]", "_", row.replace(".", "_"))
                    k = 2
                    while name in used_names:
                        name, k = f"{base}_{k}", k + 1
                    used_names.add(name)
                    ident = _vcd_id(len(ids) + sum(map(len, plan.values())))
                    width = sig.width if sig is not None else 16
                    lines.append(f"$var wire {width} {ident} {name} $end")
                    plan[g].append((ident, width, sig))
        lines += ["$upscope $end", "$enddefinitions $end"]

        def wave() -> Iterator[tuple[int, str]]:
            previous = None
            wave_ids = {ch: ids[ch] for ch in wanted}
            for c in chunks:
                tr = self._transitions(c)
                blocks, previous = vcd_blocks(tr.values, tr.starts, wave_ids, previous)
                for start, text in blocks:
                    yield round((c.t0 + start * c.ticks_per_sample) * 1e9 / hz), text

        def fpga() -> Iterator[tuple[int, str]]:
            if not plan:
                return
            last: dict[str, int] = {}
            for e in self.iter_events(streams=["fpga"]):
                untimed = e.kind.endswith("_untimed")              # 時刻の決まらない記録の間も不明
                if e.kind in ("group", "gap") or untimed:          # 観測していない信号は不明（x）
                    for g, items in plan.items():
                        for ident, width, _sig in items:
                            if (e.kind == "gap" or untimed or g != e.a) and ident in last:
                                del last[ident]
                                yield round(e.t * 1e9 / hz), (f"x{ident}" if width == 1 else f"bx {ident}")
                    continue
                if e.kind != "value":
                    continue
                for ident, width, sig in plan[e.a]:
                    v = sig.extract(e.b) if sig is not None else e.b
                    if last.get(ident) == v:
                        continue
                    last[ident] = v
                    yield round(e.t * 1e9 / hz), (f"{v}{ident}" if width == 1 else f"b{v:0{width}b} {ident}")

        with Path(path).open("w", encoding="ascii") as f:     # 改行は OS の既定（前の write_text と同じ）
            f.write("\n".join(lines) + "\n")
            current = None
            # 同じ時刻では波形の変化が先、FPGA の変化が後（heapq.merge は同じ鍵で前の並びを先に出す）
            for t, text in heapq.merge(wave(), fpga(), key=lambda x: x[0]):
                if t != current:
                    f.write(f"#{t}\n")
                    current = t
                f.write(text + "\n")

    def export_csv(self, path: Path, **filters: Any) -> None:
        with Path(path).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "t_end", "stream", "kind", "a", "b", "body", "uncertainty"])
            for e in self.events(**filters):
                w.writerow([e.t, "" if e.t_end is None else e.t_end, e.stream, e.kind, e.a, e.b,
                            "" if e.body is None else json.dumps(e.body, ensure_ascii=False),
                            "" if e.uncertainty is None else e.uncertainty])

    def export_json(self, path: Path, **filters: Any) -> None:
        Path(path).write_text(json.dumps([asdict(e) for e in self.events(**filters)], ensure_ascii=False),
                              encoding="utf-8")
