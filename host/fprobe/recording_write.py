"""記録の書き手 `Recorder`（記録中の SQLite への追記・波形のかたまりのファイル）。読み手は fprobe.recording。"""

import json
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .la import Transitions
from .recording_chunk import RAW_MAGIC, RAW_SUFFIX, raw_bytes
from .recording_model import HOST_TIME_SOURCES, ClockMap, open_table, write_json

__all__ = ["HostEvents", "Recorder"]


class HostEvents:
    """書き手の共通の口: ホストで起きた出来事を時計の対応付け（self.clock）で写して add する。"""

    def add_host(self, stream: str, kind: str, host_s: float, time_source: str = "host", **fields: Any) -> int:
        """PC の時計の時刻の出来事: PC の時刻を時計の対応付けで写し、不確かさ（写しの不確かさだけ）を付けて入れる。
        写した時刻を返す。本体（辞書）に PC の時刻である印 time_source を入れる（HOST_TIME_SOURCES）:
        "host" = PC で起きた出来事（操作・印）、"host_received" = PC が対象の出来事を受けた・読んだ時刻（対象の
        出来事はそれより前。どれだけ前かは分からず、不確かさに入っていない）。"""
        if time_source not in HOST_TIME_SOURCES:
            raise ValueError(f"time_source は {' / '.join(HOST_TIME_SOURCES)}")
        body = fields.pop("body", None)
        if body is not None and not isinstance(body, dict):
            raise TypeError("PC の時刻の出来事の本体は辞書（time_source の印を入れる）")
        t, unc = self.clock.to_ticks(host_s)
        self.add(stream, kind, t, uncertainty=unc, body={**(body or {}), "time_source": time_source}, **fields)
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
        self._db, self._table = open_table(self.directory / "recording.sqlite")
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._chunk_no = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        self._closed = False
        self._last_ticks: int | None = None          # 記録に入れた時刻の基準として最後に読んだタイムベース
        self.ended: dict | None = None               # 記録を終えた理由（以後の行は受けない。time-accuracy §14.1）
        self._rejected = 0

    def observe_time(self, ticks: int, host_s: float | None = None) -> bool:
        """記録に入れる時刻の基準として読んだタイムベースを渡す。前の読みより小さければタイムベースが 0 に戻った事実
        （64 ビットは一周しない）: 事実の行（stream timebase・kind restart、t = 前の時代の最後の読み）を書き、記録を
        終える（ended。以後の行は受けない）。続けてよければ True。"""
        with self._lock:
            if self.ended is not None:
                return False
            last = self._last_ticks
            if last is None or ticks >= last:
                self._last_ticks = ticks
                return True
            self._pending.extend(self._table.encode(
                [("timebase", "restart", last, 0, 0, {"ticks_after": ticks, "host_s": host_s})]))
        self.end("timebase_restart", last_ticks=last, ticks_after=ticks)
        return False

    def end(self, reason: str, **facts: Any) -> None:
        """記録を終える（理由と事実を meta の ended に。以後の行は受けず、数を meta の rejected_rows に）。最初の理由のまま。"""
        if not reason:
            raise ValueError("理由が要る")
        with self._lock:
            if self.ended is not None:
                return
            self.ended = {"reason": reason, **facts}
        self.update_meta(ended=self.ended, rejected_rows=0)

    def _write_meta(self) -> None:
        write_json(self.directory / "meta.json", self._meta)

    def update_meta(self, **items: Any) -> None:
        self._meta.update(items)
        self._write_meta()

    def add(self, stream: str, kind: str, t: int, a: int = 0, b: int = 0, body: Any = None,
            t_end: int | None = None, uncertainty: float | None = None) -> None:
        with self._lock:
            if self.ended is not None:
                self._rejected += 1
                return
            self._pending.extend(self._table.encode([(stream, kind, t, a, b, body, t_end, uncertainty)]))

    def add_many(self, rows: Iterable[tuple]) -> None:
        """(stream, kind, t[, a, b, body, t_end, uncertainty]) の列。"""
        rows = rows if isinstance(rows, list) else list(rows)
        with self._lock:
            if self.ended is not None:
                self._rejected += len(rows)
                return
            self._pending.extend(self._table.encode(rows))

    def add_waveform(self, tr: Transitions, start_ticks: int) -> str:
        """ロジアナの変化点（サンプル 0 がタイムベース start_ticks）を、かたまりのファイルとして入れる。
        ファイルの名前（waveform/ の中）を返す。記録を終えた後（ended）は入れず、空の名前を返す（数は rejected_rows）。"""
        tps = tr.sample_period_s * self.timebase_hz
        with self._lock:
            if self.ended is not None:
                self._rejected += 1
                return ""
            name = f"{self._chunk_no:06d}.bin"
            self._chunk_no += 1
            head = {"count": len(tr.values), "length": tr.length, "channels": tr.channels,
                    "trigger": tr.trigger, "period_s": tr.sample_period_s}
            if tr.channel_skew:                                       # ch ごとの時刻のずれ（刻み。fprobe.timing）
                head["channel_skew"] = list(tr.channel_skew)
        # ファイルはロックの外で書く（出来事を足す側を待たせない）。表に載せるのは書き終えてから（読み手は表から探す）
        header = json.dumps(head).encode() + b"\n"
        with (self.directory / "waveform" / name).open("wb") as f:
            f.write(header)
            f.write(raw_bytes(tr.values, "H"))
            f.write(raw_bytes(tr.starts, "Q"))
        with self._lock:
            self._db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
                             (start_ticks, start_ticks + round(tr.length * tps), name, tps, tr.channels, tr.length))
        return name

    @property
    def waveform_format(self) -> int:
        """この記録に書き足す波形のかたまりの形式（1 = 変化点、2 = 生の拍。記録の形式の版 3 から。古い記録は 1 のまま）。"""
        return self._table.waveform_format

    def add_raw_waveform(self, pieces: Sequence, *, rle: bool, pack: int, channels: int, period_s: float,
                         previous: int | None, start_ticks: int, length: int,
                         channel_skew: Sequence[float | None] = ()) -> str:
        """ロジアナの生の拍（サンプルの順につながる bytes・memoryview の並び）を、かたまりのファイルとして入れる（形式 2。変化点にしない。
        復号は読み手）。length = サンプルの数、previous = 最初のサンプルの直前のサンプルの値（無ければ None）。ファイルの名前を返す。
        記録を終えた後（ended）は入れず、空の名前を返す。形式 2 を書けない古い記録（waveform_format = 1）は RuntimeError。
        仕様: docs/superpowers/specs/2026-11-03-recording-raw-waveform-design.md"""
        if self.waveform_format != 2:
            raise RuntimeError("この記録（古い形式）には生の拍を書けない。変化点を add_waveform で入れる")
        tps = period_s * self.timebase_hz
        with self._lock:
            if self.ended is not None:
                self._rejected += 1
                return ""
            name = f"{self._chunk_no:06d}{RAW_SUFFIX}"
            self._chunk_no += 1
        head = {"format": 2, "rle": rle, "pack": pack, "channels": channels, "period_s": period_s, "previous": previous}
        if channel_skew:
            head["channel_skew"] = list(channel_skew)
        with (self.directory / "waveform" / name).open("wb") as f:
            f.write(RAW_MAGIC + json.dumps(head).encode() + b"\n")
            for piece in pieces:
                f.write(piece)
        with self._lock:
            self._db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
                             (start_ticks, start_ticks + round(length * tps), name, tps, channels, length))
        return name

    def flush(self) -> None:
        with self._lock:
            rows, self._pending = self._pending, []
            if rows:
                with self._table.transaction():
                    self._table.insert(rows)

    def close(self) -> None:
        if not self._closed:
            self.flush()
            if self.ended is not None:
                self.update_meta(rejected_rows=self._rejected)
            self._db.close()
            self._closed = True

    def _abandon(self) -> None:
        """試験用: 書きかけを捨てて閉じる（落ちた場合の模擬）。"""
        with self._lock:
            self._pending = []
        self._db.close()
        self._closed = True
