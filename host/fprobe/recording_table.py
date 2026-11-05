"""記録の出来事の表（recording.sqlite の中）の形式と、その書き・消し・時刻順の読みの口。

読み手・SQL の問い合わせ（fprobe.recording_sql・timeline_rows 等）が見る形はどの版でも同じ「events」:
    events(t, t_end, stream, kind, a, b, body, unc) と rowid（入れた順。同じ時刻の並びの鍵）
版（SQLite の PRAGMA user_version）:
- 版 1（〜2026-11-03 の記録。user_version 0）: 表 events そのもの。stream・kind を文字列で持ち、索引は (t)・(stream, t)・
  (stream, kind, t)・(t_end) の 4 つ。そのまま読める（書き足すときもこの形のまま）。
- 版 2（user_version 2）: 表 event に (stream, kind) の組の番号 key、組の辞書の表 names、events はその 2 つをつなぐビュー
  （rowid も列として出す）。索引は (t)・(key, t)・(t_end) の 3 つ（stream だけの絞り込みは、その stream の組の key の
  (key, t) の範囲を合わせる）。1 件の書き込みは版 1 の約半分の時間・約 6 割の大きさ（tools/bench/recording_bench.py）。
  names.longest = その組の区間（t_end − t）の長さの上限（書いた区間の最大。消しても縮めない）。t0 より前に始まって t0 の
  後に終わる区間を、(key, t) の [t0 − longest, t0) の範囲だけで探すのに使う（記録の始めから探さない）。
- 記録の形式の版（user_version 3。2026-11-03。docs/superpowers/specs/2026-11-03-recording-raw-waveform-design.md）: 表は版 2 のまま、
  波形のかたまりのファイルに生の拍の形式（見出しの format = 2）が現れうる。新しい記録はいつも 3（FORMAT_VERSION）。0・2 の記録は
  書き換えず、波形は変化点の形式で書き足す（EventTable.waveform_format）。4 以上は読めない（RecordingVersionError）。
新しい記録はいつも版 2。古い記録は移さない（調査の証拠のファイルを書き換えない）: 版ごとの口（EventTable）で両方を読み書きする。
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["EventTable", "FORMAT_VERSION", "ROW_COLUMNS", "RecordingVersionError", "VERSION", "event_view_sql", "extent",
           "version_of"]

VERSION = 2                       # 新しく作る表の版（出来事の表の形）
FORMAT_VERSION = 3                # 新しく作る記録の形式の版（PRAGMA user_version。波形のかたまりに生の拍の形式がありうる）


class RecordingVersionError(RuntimeError):
    """この版のソフトが読めない新しい形式の記録（user_version が FORMAT_VERSION より大きい）。"""
# 本体の JSON（json.dumps(本体, ensure_ascii=False) と同じ文字列。dumps は呼ぶたびに符号器を作るので 1 つを使い回す）
_ENCODE = json.JSONEncoder(ensure_ascii=False).encode

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    t INTEGER NOT NULL, t_end INTEGER, stream TEXT NOT NULL, kind TEXT NOT NULL,
    a INTEGER NOT NULL DEFAULT 0, b INTEGER NOT NULL DEFAULT 0, body TEXT, unc REAL);
CREATE INDEX IF NOT EXISTS events_t ON events (t);
CREATE INDEX IF NOT EXISTS events_stream_t ON events (stream, t);
CREATE INDEX IF NOT EXISTS events_stream_kind_t ON events (stream, kind, t);
CREATE INDEX IF NOT EXISTS events_t_end ON events (t_end) WHERE t_end IS NOT NULL;
"""
_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS names (
    id INTEGER PRIMARY KEY, stream TEXT NOT NULL, kind TEXT NOT NULL, longest INTEGER, UNIQUE (stream, kind));
CREATE TABLE IF NOT EXISTS event (
    t INTEGER NOT NULL, t_end INTEGER, key INTEGER NOT NULL,
    a INTEGER NOT NULL DEFAULT 0, b INTEGER NOT NULL DEFAULT 0, body TEXT, unc REAL);
CREATE INDEX IF NOT EXISTS event_t ON event (t);
CREATE INDEX IF NOT EXISTS event_key_t ON event (key, t);
CREATE INDEX IF NOT EXISTS event_t_end ON event (t_end) WHERE t_end IS NOT NULL;
CREATE VIEW IF NOT EXISTS events AS
    SELECT e.t AS t, e.t_end AS t_end, n.stream AS stream, n.kind AS kind, e.a AS a, e.b AS b, e.body AS body,
           e.unc AS unc, e.rowid AS rowid
    FROM event AS e JOIN names AS n ON n.id = e.key;
"""
_CHUNKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    t0 INTEGER NOT NULL, t1 INTEGER NOT NULL, path TEXT NOT NULL, ticks_per_sample REAL NOT NULL,
    channels INTEGER NOT NULL, length INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_t ON chunks (t0, t1);
"""
_COLUMNS = "t, t_end, stream, kind, a, b, body, unc"          # events の列（rowid のほか）
ROW_COLUMNS = "a, b, body, t_end, unc"                         # rows の既定の列（t・rowid・stream・kind の後）
_INSERT = {1: "INSERT INTO events (t, t_end, stream, kind, a, b, body, unc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
           2: "INSERT INTO event (t, t_end, key, a, b, body, unc) VALUES (?, ?, ?, ?, ?, ?, ?)"}


def event_view_sql(schema: str) -> str:
    """接続にあとから足した記録（ATTACH の schema）の events を、どの版でも同じ列（rowid を含む）で読む SELECT。"""
    return f"SELECT {_COLUMNS}, rowid AS rowid FROM {schema}.events"


def version_of(db: sqlite3.Connection, schema: str = "main") -> int | None:
    """表の版（表がまだ無ければ None）。"""
    kinds = dict(db.execute(f"SELECT name, type FROM {schema}.sqlite_master WHERE name IN ('events', 'event')").fetchall())
    if kinds.get("events") == "table":
        return 1
    if "event" in kinds:
        return 2
    return None


def extent(db: sqlite3.Connection) -> tuple[int | None, int | None, int]:
    """出来事の (最初の時刻, 最後の時刻（区間の終わり t_end を含む）, 件数)。索引の端だけを読む（全件を読まない）。
    表がまだ無ければ (None, None, 0)。"""
    version = version_of(db)
    if version is None:
        return None, None, 0
    table = "events" if version == 1 else "event"
    lo, = db.execute(f"SELECT MIN(t) FROM {table}").fetchone()          # 1 文に 1 つ（並べると索引を使わない）
    hi, = db.execute(f"SELECT MAX(t) FROM {table}").fetchone()
    hi_end, = db.execute(f"SELECT MAX(t_end) FROM {table} WHERE t_end IS NOT NULL").fetchone()
    count, = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    if hi_end is not None and (hi is None or hi_end > hi):
        hi = hi_end
    return lo, hi, count


class EventTable:
    """1 つの recording.sqlite の出来事の表の口（版 1・2）。接続を使う側が排他する（Recorder のロック等）。
    insert / delete は呼び手のトランザクションの中で呼ぶ（自分では BEGIN しない）。"""

    def __init__(self, db: sqlite3.Connection, version: int):
        self.db, self.version = db, version
        self.format_version = db.execute("PRAGMA user_version").fetchone()[0]    # 記録の形式の版（0・2 = 古い記録）
        if self.format_version > FORMAT_VERSION:
            raise RecordingVersionError(f"記録の形式の版 {self.format_version} はこのソフトでは読めない（読めるのは "
                                        f"{FORMAT_VERSION} まで）。新しい fprobe で開く")
        self.waveform_format = 2 if self.format_version >= 3 else 1    # 書き足す波形のかたまりの形式（1 = 変化点、2 = 生の拍）
        self.physical = "events" if version == 1 else "event"        # t・t_end・a・b・body・unc を持つ実の表
        self._key: dict[tuple[str, str], int] = {}                   # 版 2: (stream, kind) → key
        self._name: dict[int, tuple[str, str]] = {}                  # 版 2: key → (stream, kind)
        self._longest: dict[int, int | None] = {}                    # 版 2: key → names.longest（覚えた値）
        self._longest_dirty: dict[int, int] = {}                     # 版 2: 次の insert で names に書く長さ
        if version == 2:
            self.reload()

    @classmethod
    def open(cls, db: sqlite3.Connection, create: bool = True) -> EventTable:
        """表の版を見て口を作る。表が無ければ（create なら）版 2 で作る。波形のかたまりの表 chunks も作る。"""
        version = version_of(db)
        if version is None and create:
            db.execute("BEGIN IMMEDIATE")
            try:
                version = version_of(db)                            # ほかの接続が先に作ったかもしれない
                if version is None:
                    for statement in _V2_SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    db.execute(f"PRAGMA user_version = {FORMAT_VERSION}")
                    version = VERSION
                for statement in _CHUNKS_SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        elif version is not None and create:
            db.executescript(_CHUNKS_SCHEMA if version == 2 else _V1_SCHEMA + _CHUNKS_SCHEMA)
        if version is None:
            raise sqlite3.OperationalError("記録の出来事の表がまだ無い")
        return cls(db, version)

    # --- 組の辞書（版 2）-----------------------------------------------------------------------------------

    def reload(self) -> None:
        """組の辞書を読み直す（ほかの接続が足した組を知る）。"""
        for key, stream, kind, longest in self.db.execute("SELECT id, stream, kind, longest FROM names"):
            self._key[(stream, kind)] = key
            self._name[key] = (stream, kind)
            self._longest[key] = longest

    def forget(self) -> None:
        """組の辞書の覚えを捨てて読み直す。呼び手のトランザクションを ROLLBACK した後に必ず呼ぶ（その中で足した組・長さの
        上限の覚えが、消えた names の行を指したまま残らないように。残ると足し直した出来事が消えた key で書かれて見えなくなり、
        別の組がその番号を使い直すと別の名前で見えた。レビュー 2026-11-03）。版 1 では何もしない。"""
        if self.version != 2:
            return
        self._key.clear()
        self._name.clear()
        self._longest.clear()
        self._longest_dirty.clear()
        self.reload()

    @contextmanager
    def transaction(self, begin: str = "BEGIN") -> Iterator[None]:
        """insert / delete を包むトランザクション（begin = "BEGIN" か "BEGIN IMMEDIATE"）。途中で失敗したら ROLLBACK して
        組の辞書の覚えを捨てる（forget。ROLLBACK で消えた組・長さの覚えを残さない）。"""
        self.db.execute(begin)
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.forget()
            raise

    def _key_of(self, stream: str, kind: str) -> int:
        key = self._key.get((stream, kind))
        if key is None:
            if not isinstance(stream, str) or not isinstance(kind, str):
                raise TypeError(f"stream・kind は文字列: {stream!r}, {kind!r}")
            self.db.execute("INSERT OR IGNORE INTO names (stream, kind) VALUES (?, ?)", (stream, kind))
            key, longest = self.db.execute("SELECT id, longest FROM names WHERE stream = ? AND kind = ?",
                                           (stream, kind)).fetchone()
            self._key[(stream, kind)], self._name[key], self._longest[key] = key, (stream, kind), longest
        return key

    def name_of(self, key: int) -> tuple[str, str]:
        name = self._name.get(key)
        if name is None:
            self.reload()
            name = self._name[key]
        return name

    def keys(self, streams: list[str] | None, kinds: list[str] | None) -> list[int] | None:
        """版 2: 絞り込みに当たる組の key（昇順。None = 絞らない）。"""
        if not streams and not kinds:
            return None
        self.reload()
        s, k = set(streams or ()), set(kinds or ())
        return sorted(key for (stream, kind), key in self._key.items()
                      if (not s or stream in s) and (not k or kind in k))

    def longest(self, key: int) -> int | None:
        return self._longest.get(key)

    # --- 書き・消し ----------------------------------------------------------------------------------------

    def encode(self, rows: Iterable[tuple]) -> list[tuple]:
        """(stream, kind, t[, a, b, body, t_end, unc]) の列（body は JSON にする前の値）を、insert に渡す行にする。
        版 2 は組の番号を引き（無い組は names に足す）、区間の長さの上限を覚える。"""
        pad = (0, 0, None, None, None)
        dumps = _ENCODE
        out = []
        append = out.append
        if self.version == 1:
            for r in rows:
                if type(r) is not tuple:
                    r = tuple(r)
                stream, kind, t, a, b, body, t_end, unc = r + pad[len(r) - 3:] if len(r) < 8 else r
                append((t, t_end, stream, kind, a, b, None if body is None else dumps(body), unc))
            return out
        key_of, keys = self._key_of, self._key
        longest, dirty = self._longest, self._longest_dirty
        for r in rows:
            if type(r) is not tuple:
                r = tuple(r)
            stream, kind, t, a, b, body, t_end, unc = r + pad[len(r) - 3:] if len(r) < 8 else r
            key = keys.get((stream, kind))
            if key is None:
                key = key_of(stream, kind)
            if t_end is not None:
                span = t_end - t
                known = dirty.get(key, longest.get(key))
                if known is None or span > known:
                    dirty[key] = span
            append((t, t_end, key, a, b, None if body is None else dumps(body), unc))
        return out

    def insert(self, rows: list[tuple]) -> None:
        """encode した行を書く（呼び手のトランザクションの中で）。"""
        if self.version == 2 and self._longest_dirty:
            self.db.executemany("UPDATE names SET longest = MAX(COALESCE(longest, ?), ?) WHERE id = ?",
                                [(span, span, key) for key, span in self._longest_dirty.items()])
            for key, span in self._longest_dirty.items():
                self._longest[key] = max(span, self._longest.get(key) or span)
            self._longest_dirty.clear()
        if rows:
            self.db.executemany(_INSERT[self.version], rows)

    def delete(self, streams: list[str], where: str = "", args: Iterable[Any] = ()) -> None:
        """stream が streams のどれかで、where（実の表の列 t・t_end・a・b・body・unc の条件。空 = すべて）に合う出来事を消す。"""
        if not streams:
            return
        extra = f" AND ({where})" if where else ""
        if self.version == 1:
            self.db.execute(f"DELETE FROM events WHERE stream IN ({','.join('?' * len(streams))}){extra}",
                            [*streams, *args])
            return
        keys = self.keys(streams, None)
        if keys:
            self.db.execute(f"DELETE FROM event WHERE key IN ({','.join('?' * len(keys))}){extra}", [*keys, *args])


    # --- 時刻順の読み -------------------------------------------------------------------------------------

    def rows(self, t0: int | None, t1: int | None, streams: list[str] | None, kinds: list[str] | None,
             overlapping: bool = False, a: int | None = None, columns: str = ROW_COLUMNS) -> Iterator[tuple]:
        """時刻順（同じ時刻は入れた順）の (t, rowid, stream, kind, *columns)。overlapping なら t0 より前に始まって t0 より
        後に終わる区間も（events の `(t >= t0 OR t_end > t0)` と同じ集まり）。columns は実の表の列（a・b・body・t_end・unc）。
        版 2: 組ごとに索引 (key, t) の範囲を読み、(t, rowid) で合わせる（並べ直さない。stream・kind は辞書の表をつないで）。"""
        if self.version == 1:
            yield from self._rows_v1(t0, t1, streams, kinds, overlapping, a, columns)
            return
        keys = self.keys(streams, kinds)
        if keys is not None and not keys:
            return
        if overlapping and t0 is not None:
            if keys is None:
                self.reload()
            yield from self._straddlers(sorted(self._name) if keys is None else keys, t0, t1, a, columns)
        cursors = [self._scan(k, t0, t1, a, columns) for k in (keys or [None])]
        yield from cursors[0] if len(cursors) == 1 else heapq.merge(*cursors)          # (t, rowid) の順

    def _scan(self, key: int | None, t0: int | None, t1: int | None, a: int | None, columns: str,
              extra: str = "", extra_args: tuple = ()) -> Iterator[tuple]:
        """(t, rowid, stream, kind, *columns) を (t, rowid) の順に。key があれば索引 (key, t)、無ければ (t)。"""
        clauses, args = [], []
        if key is not None:
            clauses.append("e.key = ?")
            args.append(key)
        if t0 is not None:
            clauses.append("e.t >= ?")
            args.append(t0)
        if t1 is not None:
            clauses.append("e.t < ?")
            args.append(t1)
        if a is not None:
            clauses.append("e.a = ?")
            args.append(a)
        if extra:
            clauses.append(extra)
            args.extend(extra_args)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        index = "event_key_t" if key is not None else "event_t"
        return self.db.execute(f"SELECT e.t, e.rowid, n.stream, n.kind, {columns} FROM event AS e INDEXED BY {index} "
                               f"JOIN names AS n ON n.id = e.key{where} ORDER BY e.t, e.rowid", args)

    def _straddlers(self, keys: list[int], t0: int, t1: int | None, a: int | None, columns: str) -> Iterator[tuple]:
        """t0 より前に始まって t0 より後に終わる区間を (t, rowid) の順に。組ごとに [t0 − longest, t0) だけを探す
        （longest = その組の区間の長さの上限。それより前に始まった区間は t0 より前に終わっている）。"""
        end = t0 if t1 is None else min(t0, t1)
        parts = [self._scan(key, t0 - longest, end, a, columns, "e.t_end > ?", (t0,)).fetchall()
                 for key in keys if (longest := self._longest.get(key)) is not None and longest > 0]
        return iter(parts[0]) if len(parts) == 1 else heapq.merge(*parts)

    def nearest(self, t: int, before: bool, stream: str, kind: str | None, a: int | None = None,
                body: dict[str, Any] | None = None) -> tuple | None:
        """t の直前（before: 時刻 ≦ t のいちばん後）か直後（時刻 > t のいちばん前）の 1 件の (t, rowid, stream, kind,
        a, b, body, t_end, unc)。同じ時刻は入れた順で t に近い側。a・body（{欄: 値}。欄の名前は呼び手が検査済み）で絞る。"""
        order = "DESC" if before else "ASC"
        p = "" if self.version == 1 else "e."                                 # 実の表の列の前置き
        clauses, args = [f"{p}t <= ?" if before else f"{p}t > ?"], [t]
        if a is not None:
            clauses.append(f"{p}a = ?")
            args.append(a)
        for key, value in (body or {}).items():
            clauses.append(f"json_extract({p}body, '$.{key}') = ?")
            args.append(value)
        if self.version == 1:
            names = ["stream = ?"] + (["kind = ?"] if kind is not None else [])
            return self.db.execute(f"SELECT t, rowid, stream, kind, {ROW_COLUMNS} FROM events WHERE "
                                   f"{' AND '.join(names + clauses)} ORDER BY t {order}, rowid {order} LIMIT 1",
                                   [stream, *([kind] if kind is not None else []), *args]).fetchone()
        if kind is not None:                  # 組が 1 つ: 辞書の表の一意の索引で組を引き、索引 (key, t) の端を読む（1 文）
            return self.db.execute(
                f"SELECT e.t, e.rowid, n.stream, n.kind, {ROW_COLUMNS} FROM names AS n JOIN event AS e INDEXED BY "
                f"event_key_t ON e.key = n.id WHERE n.stream = ? AND n.kind = ? AND {' AND '.join(clauses)} "
                f"ORDER BY e.t {order}, e.rowid {order} LIMIT 1", [stream, kind, *args]).fetchone()
        # 組ごとの直前・直後の rowid（相関の副問い合わせ: 組ごとに索引 (key, t) の端を 1 回引く）から、t に近い 1 件
        names = "n.stream = ?"
        rowids = [r for r, in self.db.execute(
            f"SELECT (SELECT e.rowid FROM event AS e INDEXED BY event_key_t WHERE e.key = n.id AND {' AND '.join(clauses)} "
            f"ORDER BY e.t {order}, e.rowid {order} LIMIT 1) FROM names AS n WHERE {names}",
            [*args, stream]) if r is not None]
        if not rowids:
            return None
        return self.db.execute(f"SELECT e.t, e.rowid, n.stream, n.kind, {ROW_COLUMNS} FROM event AS e JOIN names AS n "
                               f"ON n.id = e.key WHERE e.rowid IN ({','.join('?' * len(rowids))}) "
                               f"ORDER BY e.t {order}, e.rowid {order} LIMIT 1", rowids).fetchone()

    def _rows_v1(self, t0, t1, streams, kinds, overlapping, a, columns) -> Iterator[tuple]:
        clauses, args = [], []
        if t0 is not None:
            clauses.append("(t >= ? OR t_end > ?)" if overlapping else "t >= ?")
            args.extend([t0, t0] if overlapping else [t0])
        if t1 is not None:
            clauses.append("t < ?")
            args.append(t1)
        for column, values in (("stream", streams), ("kind", kinds)):
            if values:
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                args.extend(values)
        if a is not None:
            clauses.append("a = ?")
            args.append(a)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        yield from self.db.execute(f"SELECT t, rowid, stream, kind, {columns} FROM events {where} ORDER BY t, rowid", args)

    # --- 数える（版 2 は組の番号で数えて名前に直す。ビューをつながない）-------------------------------------------

    def group_counts(self, t0: int | None, t1: int | None, bin_width: int | None = None,
                     streams: list[str] | None = None) -> list[tuple]:
        """範囲 [t0, t1) の (stream, kind, 区間, 件数, 最小の時刻, 最大の時刻)。bin_width があれば区間 = (t − t0) // 幅、
        無ければ区間 = 0。streams で絞る。"""
        clauses, args = [], []
        if t0 is not None:
            clauses.append("t >= ?")
            args.append(t0)
        if t1 is not None:
            clauses.append("t < ?")
            args.append(t1)
        binning = "(t - ?) / ?" if bin_width is not None else "0"
        bin_args = [t0, bin_width] if bin_width is not None else []
        group_v1, group_v2 = ("1, 2, 3", "1, 2") if bin_width is not None else ("1, 2", "1")   # 区間なし: 組だけで（索引の順）
        if self.version == 1:
            if streams:
                clauses.append(f"stream IN ({','.join('?' * len(streams))})")
                args.extend(streams)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            return [tuple(r) for r in self.db.execute(
                f"SELECT stream, kind, {binning}, COUNT(*), MIN(t), MAX(t) FROM events{where} GROUP BY {group_v1}",
                [*bin_args, *args])]
        keys = self.keys(streams, None)
        if keys is not None:
            if not keys:
                return []
            clauses.append(f"key IN ({','.join('?' * len(keys))})")
            args.extend(keys)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        name = self.name_of
        return [(*name(key), b, n, lo, hi) for key, b, n, lo, hi in self.db.execute(
            f"SELECT key, {binning}, COUNT(*), MIN(t), MAX(t) FROM event{where} GROUP BY {group_v2}", [*bin_args, *args])]
