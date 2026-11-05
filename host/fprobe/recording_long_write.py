"""長期の記録の書き手 `SegmentedRecorder`（分割・縮図の事前計算・間引き・容量の上限）。読み手は fprobe.recording_long。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from .la import Transitions
from .la_wave import raw_facts
from .recording_model import ClockMap, write_json
from .recording_segments import (INDEX_LEVEL, INDEX_SCHEMA, SEGMENT_SUMMARY, WAVEFORM, Policy, bytes_of, bytes_of_index,
                                 connect_long, toggles)
from .recording_summary import SummaryGrid, bin_counts
from .recording_table import EventTable, extent
from .recording_write import HostEvents, Recorder

__all__ = ["SegmentedRecorder"]


class SegmentedRecorder(HostEvents):
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
        self._index = connect_long(self.directory / "index.sqlite", INDEX_SCHEMA)
        self._lock = threading.RLock()
        self._closed = False
        self._last_ticks: int | None = None          # 時刻の基準として最後に読んだタイムベース（Recorder.observe_time）
        self.ended: dict | None = None
        self._rejected = 0
        self._open_segment()

    def observe_time(self, ticks: int, host_s: float | None = None) -> bool:
        """Recorder.observe_time と同じ（タイムベースが 0 に戻ったら事実の行を書いて記録を終える。分割をまたいで見る）。"""
        with self._lock:
            if self.ended is not None:
                return False
            last = self._last_ticks
            if last is None or ticks >= last:
                self._last_ticks = ticks
                return True
            self._current.add("timebase", "restart", last, body={"ticks_after": ticks, "host_s": host_s})
            self.end("timebase_restart", last_ticks=last, ticks_after=ticks)
            return False

    def end(self, reason: str, **facts: Any) -> None:
        """Recorder.end と同じ（以後の行は受けず、数を meta の rejected_rows に）。"""
        if not reason:
            raise ValueError("理由が要る")
        with self._lock:
            if self.ended is not None:
                return
            self.ended = {"reason": reason, **facts}
            self.update_meta(ended=self.ended, rejected_rows=0)

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
            if self.ended is not None:
                self._rejected += 1
                return
            self._current.add(stream, kind, t, a, b, body, t_end, uncertainty)

    def add_many(self, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        with self._lock:
            if self.ended is not None:
                self._rejected += len(rows)
                return
            self._current.add_many(rows)

    def add_waveform(self, tr: Transitions, start_ticks: int) -> str:
        tps = tr.sample_period_s * self.timebase_hz
        with self._lock:
            if self.ended is not None:
                self._rejected += 1
                return ""
            name = self._current.add_waveform(tr, start_ticks)
            values = tr.values
            self._index.execute(
                "INSERT INTO wave VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (self._no, start_ticks, start_ticks + round(tr.length * tps), f"{self._path}/waveform/{name}", tps,
                 tr.channels, tr.length, int(values[0]) if len(values) else 0, int(values[-1]) if len(values) else 0,
                 toggles(values)))
            self._wave_bytes += 10 * len(values) + 64
            return name

    @property
    def waveform_format(self) -> int:
        """いまの分割に書き足す波形のかたまりの形式（Recorder.waveform_format。新しい分割は 2、古い分割に書き足すなら 1）。"""
        return self._current.waveform_format

    def add_raw_waveform(self, pieces: Sequence, *, rle: bool, pack: int, channels: int, period_s: float,
                         previous: int | None, start_ticks: int, length: int,
                         channel_skew: Sequence[float | None] = ()) -> str:
        """Recorder.add_raw_waveform と同じ（生の拍を入れる）。索引の first / last / toggles（縮図の事実）は、変化点を作らずに
        拍の列から求める（la_wave.raw_facts。add_waveform が変化点から求めた値と同じ）。"""
        tps = period_s * self.timebase_hz
        with self._lock:
            if self.ended is not None:
                self._rejected += 1
                return ""
            name = self._current.add_raw_waveform(pieces, rle=rle, pack=pack, channels=channels, period_s=period_s,
                                                  previous=previous, start_ticks=start_ticks, length=length,
                                                  channel_skew=channel_skew)
            first, last, toggles = raw_facts(pieces, rle, pack)
            self._index.execute(
                "INSERT INTO wave VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (self._no, start_ticks, start_ticks + round(length * tps), f"{self._path}/waveform/{name}", tps,
                 channels, length, first, last, toggles))
            self._wave_bytes += sum(len(p) for p in pieces) + 64
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
            if self.ended is not None:
                self._meta["rejected_rows"] = self._rejected
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
        db = connect_long(directory / "recording.sqlite", SEGMENT_SUMMARY)
        try:
            lo, hi, count = extent(db)
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
                            "WHERE no = ?", (t0, t1, count, bytes_of(directory), self._now(), self._no))
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
            total = sum(r[2] for r in rows) + bytes_of_index(self.directory)
            for no, path, size in rows:
                if total <= p.max_bytes or no == newest:
                    break
                if self._delete(no, path):
                    total -= size

    def _decimate(self, no: int, path: str) -> None:
        directory = self.directory / path
        streams = [s for s in self.policy.decimate_streams if s != WAVEFORM]
        try:
            db = connect_long(directory / "recording.sqlite")
            try:
                table = EventTable.open(db)
                if streams:
                    table.delete(streams)
                if WAVEFORM in self.policy.decimate_streams:
                    for name, in db.execute("SELECT path FROM chunks").fetchall():
                        (directory / "waveform" / name).unlink(missing_ok=True)
                    db.execute("DELETE FROM chunks")
                db.execute("VACUUM")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                remaining = db.execute(f"SELECT COUNT(*) FROM {table.physical}").fetchone()[0]
            finally:
                db.close()
        except (OSError, sqlite3.OperationalError):              # 読み手が開いている等: 次の機会に
            return
        if WAVEFORM in self.policy.decimate_streams:
            self._index.execute("UPDATE wave SET kept = 0 WHERE segment = ?", (no,))
        self._index.execute("UPDATE segments SET dropped = ?, events = ?, bytes = ? WHERE no = ?",
                            (json.dumps(list(self.policy.decimate_streams)), remaining, bytes_of(directory), no))

    def _delete(self, no: int, path: str) -> bool:
        try:
            shutil.rmtree(self.directory / path)
        except OSError:                                            # 読み手が開いている等（Windows）: 次の機会に
            return False
        self._index.execute("UPDATE segments SET state = 'deleted', bytes = 0 WHERE no = ?", (no,))
        self._index.execute("UPDATE wave SET kept = 0 WHERE segment = ?", (no,))
        return True
