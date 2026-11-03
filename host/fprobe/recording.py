"""共通の時間軸の記録（F3）: fprobe が観測する出来事を、デバイスのタイムベースの上の 1 つの記録にまとめる。

仕様: docs/superpowers/specs/2026-10-17-f3-timeline-recording-design.md
- 時刻は整数のタイムベース（デバイスの 64 ビットのカウンタ）。ホストで起きた出来事は `ClockMap` で写し、不確かさを持つ。
- 1 回の記録 = 1 つのディレクトリ。索引と小さな出来事は SQLite（recording.sqlite）、波形は追記だけのかたまりのファイル
  （waveform/）、記録の情報は meta.json。
- 書き手 `Recorder` はまとめて書き（flush）、読み手 `Recording` は記録中でも読める（SQLite の WAL）。
"""

import csv
import heapq
import itertools
import json
import os
import re
import sqlite3
import statistics
import sys
import threading
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .la import Transitions, Waveform, vcd_blocks_skewed
from .la_vcd import vcd_comments, vcd_unit
from .la_wave import skew_unknown
from .recording_table import ROW_COLUMNS, EventTable

__all__ = ["ClockMap", "Event", "Recorder", "Recording"]

CLOCK_WINDOW = 64                 # 時計の対応付けに使う最近の点の数
NO_DATA = "-"                     # 波形の要約: その時間に波形が無い



# PC の時計で付けた出来事の時刻の印（本体の time_source）: "host" = PC で起きた出来事、"host_received" = PC が対象の
# 出来事を受けた・読んだ時刻（対象の出来事はそれより前）。印の無い出来事の時刻はデバイスのタイムベース
HOST_TIME_SOURCES = ("host", "host_received")


def write_json(path: Path, obj: Any) -> None:
    """JSON を一時ファイルに書いてから置き換える（途中で失敗しても元のファイルは壊れない）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ClockMap:
    """ホストの単調時計（秒）→ デバイスのタイムベース。読みの往復の中点とタイムベースの組を直線で当てはめる。
    タイムベースが前の点より小さくなったら 0 に戻った事実（時代の切れ目。time-accuracy §14.1）: 前の時代の点を捨てて
    当てはめ直し、回数を restarts に数える（前後の時代を 1 本の直線にしない）。"""

    def __init__(self, timebase_hz: float):
        self.hz = timebase_hz
        self._points: list[tuple[float, int, float]] = []
        self.restarts = 0

    def add(self, host_s: float, ticks: int, round_trip_s: float) -> None:
        """host_s は読みの前後のホストの時刻の中点、round_trip_s は読みの往復時間。"""
        if self._points and ticks < self._points[-1][1]:
            self._points.clear()
            self.restarts += 1
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


def _events(rows: Iterable[tuple]) -> Iterator[Event]:
    """表の行 (t, rowid, stream, kind, a, b, body の JSON, t_end, unc)（EventTable.rows）→ Event。凍った dataclass の
    __init__（欄ごとに object.__setattr__）を通さず、同じ中身の Event を作る（読みの 1 件の時間の多くがそこだった。等しさ・
    ハッシュ・表示は同じ）。"""
    new, loads = object.__new__, json.loads
    for t, _rowid, s, k, a, b, body, te, unc in rows:
        e = new(Event)
        d = e.__dict__
        d["t"], d["stream"], d["kind"], d["a"], d["b"] = t, s, k, a, b
        d["body"], d["t_end"], d["uncertainty"] = None if body is None else loads(body), te, unc
        yield e


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


def _connect(path: Path) -> tuple[sqlite3.Connection, EventTable]:
    """記録の SQLite を開き、出来事の表の口を作る（表が無ければ新しい版で作る。古い版はそのまま: recording_table）。"""
    db = connect_db(path)
    return db, EventTable.open(db)


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
        self._db, self._table = _connect(self.directory / "recording.sqlite")
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
            f.write(_raw(tr.values, "H"))
            f.write(_raw(tr.starts, "Q"))
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
                self._db.execute("BEGIN")
                try:
                    self._table.insert(rows)
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    self._table.forget()               # ROLLBACK で消えた組・長さの覚えを残さない
                    raise

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


def _raw(values: Any, code: str) -> bytes | memoryview:
    """かたまりのファイルに書く値の列のバイト列（array(code) の tobytes と同じ）。符号なし整数の同じ幅の連続した
    バッファ（array・numpy の配列）は写さずにそのまま、ほかは array にして。"""
    try:
        view = memoryview(values)
    except TypeError:
        return array(code, values).tobytes()
    fmt = view.format
    if fmt[:1] in ("@", "=") or (fmt[:1] == "<" and sys.byteorder == "little"):
        fmt = fmt[1:]                                         # 自分の機械の並びのバイト順
    if view.ndim == 1 and view.c_contiguous and fmt in ("B", "H", "I", "L", "Q") and view.itemsize == array(code).itemsize:
        return view.cast("B")
    return array(code, values).tobytes()


def _vcd_id(i: int) -> str:
    """VCD の識別子（印字できる ASCII 94 字の並び）。"""
    out = chr(33 + i % 94)
    while i >= 94:
        i = i // 94 - 1
        out = chr(33 + i % 94) + out
    return out


# 生の拍のかたまり（形式 2）のファイル: 拡張子と、見出しの行の頭の印。古い読み手（形式 1 しか知らない）は見出しの行を JSON として読む
# ので、印があると JSON として読めず、必ず「読めない」誤りで止まる（変化点と読み違えない）。見出しの欄を足しただけでは、
# 欄を見ない古い読み手が通ってしまう（見出しだけを読む口: chunk_skew）。仕様 2026-11-03-recording-raw-waveform-design.md §3
RAW_SUFFIX = ".fpraw"
RAW_MAGIC = b"FPRAW2 "


def _head_of(line: bytes) -> dict:
    """かたまりのファイルの見出しの行（改行の前まで）。形式 2 は RAW_MAGIC の後ろが JSON。"""
    if line.startswith(RAW_MAGIC):
        return json.loads(line[len(RAW_MAGIC):])
    return json.loads(line)


WAVE_CACHE_BYTES = 256 << 20          # 記録の読み手が覚えておく変化点の表の大きさの上限（使った順に捨てる）


class Recording:
    """記録の読み手（記録中でも読める）。時刻はタイムベース、t1 は含まない。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
        self._db, self._table = _connect(self.directory / "recording.sqlite")
        self._init_waves()

    def _init_waves(self) -> None:
        """かたまりの変化点の表を使った順に覚える（WAVE_CACHE_BYTES まで。超えたら古いものから捨てる）。"""
        self._waves: OrderedDict[str, Waveform] = OrderedDict()
        self._distinct: dict = {}                                       # distinct の答え（記録の変わり目の印と）
        self._writes = 0                                                # この読み手が書いた回数（distinct の覚えの印）
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
        events = _events(self._rows(t0, t1, streams, kinds, overlapping, a))
        return events if limit is None else itertools.islice(events, max(0, int(limit)))

    def _rows(self, t0: int | None, t1: int | None, streams: list[str] | None, kinds: list[str] | None,
              overlapping: bool = False, a: int | None = None, columns: str = ROW_COLUMNS) -> Iterator[tuple]:
        """iter_events と同じ集まり・並びの (t, rowid, stream, kind, *columns)（body は JSON の文字列のまま）。"""
        return self._table.rows(t0, t1, streams, kinds, overlapping, a, columns)

    def _distinct_a(self, stream: str, kind: str) -> list[int]:
        """stream・kind の出来事の a の異なる値（昇順）。"""
        return [a for a, in self._db.execute("SELECT DISTINCT a FROM events WHERE stream = ? AND kind = ? ORDER BY a",
                                             (stream, kind))]

    @staticmethod
    def _where(t0, t1, streams, kinds) -> tuple[str, list]:
        """読みの口 events（どの版でも同じ列）への絞り込みの WHERE。"""
        clauses, args = [], []
        if t0 is not None:
            clauses.append("t >= ?")
            args.append(t0)
        if t1 is not None:
            clauses.append("t < ?")
            args.append(t1)
        for column, values in (("stream", streams), ("kind", kinds)):
            if values:
                clauses.append(in_list(column, values))
                args.extend(values)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", args

    def nearest(self, t: int, before: bool, stream: str, kind: str | None = None, a: int | None = None,
                body: dict[str, Any] | None = None) -> Event | None:
        """t の直前（before: 時刻 ≦ t のいちばん後）か直後（時刻 > t のいちばん前）の出来事 1 つ（無ければ None）。
        stream・kind・a と body の欄の値（{欄: 値}。一致）で絞る。同じ時刻のものは時刻順の並び（入れた順）で t に近い側。"""
        for key in body or {}:
            if not re.fullmatch(r"\w+", key):
                raise ValueError(f"body の欄 {key!r} は英数字と _ だけ")
        row = self._table.nearest(t, before, stream, kind, a, body)
        return None if row is None else next(_events([row]))

    def add(self, stream: str, kind: str, t: int, a: int = 0, b: int = 0, body: Any = None,
            t_end: int | None = None) -> None:
        """閉じた記録に出来事を 1 つ足す（後から置く調査の印。recording_mark の t）。"""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._table.insert(self._table.encode([(stream, kind, t, a, b, body, t_end)]))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            self._table.forget()                       # ROLLBACK で消えた組・長さの覚えを残さない
            raise
        self._writes += 1

    def replace_derived(self, stream: str, source: str, events: Iterable[Event]) -> int:
        """解析が作った出来事（派生イベント。例: 規則の違反）を書く。同じ stream で body の "source" が同じものを先に
        消す（検査し直すと前の結果が残らない）。各出来事の body（辞書）に source を入れる。書いた件数を返す。"""
        new = [(stream, e.kind, e.t, e.a, e.b, {**(e.body or {}), "source": source}, e.t_end, e.uncertainty)
               for e in events]
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._table.delete([stream], "json_extract(body, '$.source') = ?", [source])
            rows = self._table.encode(new)
            self._table.insert(rows)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            self._table.forget()                       # ROLLBACK で消えた組・長さの覚えを残さない
            raise
        self._writes += 1                           # 消したので distinct の覚えは古い
        return len(rows)

    def count(self) -> int:
        return self._db.execute(f"SELECT COUNT(*) FROM {self._table.physical}").fetchone()[0]

    def count_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                     kinds: list[str] | None = None) -> int:
        """events（limit なし）が返す出来事の数。"""
        if not streams and not kinds:                     # 名前で絞らない: 実の表の索引 (t) で（辞書の表をつながない）
            sql, args = self._where(t0, t1, None, None)
            return self._db.execute(f"SELECT COUNT(*) FROM {self._table.physical} {sql}", args).fetchone()[0]
        sql, args = self._where(t0, t1, streams, kinds)
        return self._db.execute(f"SELECT COUNT(*) FROM events {sql}", args).fetchone()[0]

    def distinct(self, column: str, streams: list[str] | None = None) -> list:
        """欄（stream / kind / a / b）の異なる値の一覧（例: サンプラが読んだ番地 = distinct("a", ["sample"])）。"""
        if column not in ("stream", "kind", "a", "b"):
            raise ValueError(f"欄 {column!r} は stream / kind / a / b")
        # 記録が変わっていなければ前の答え（全件を読むので）。変わったか = ほかの接続の書き込み（PRAGMA data_version）と
        # この読み手の書き込みの回数
        newest = (self._db.execute("PRAGMA data_version").fetchone()[0], self._writes)
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
        return sorted((s, k, n, lo, hi) for s, k, _bin, n, lo, hi in self._table.group_counts(t0, t1))

    def density_bounds(self, t0: int, t1: int, width: int, streams: list[str] | None = None) -> dict:
        """density と同じ件数と、数えられない画素（普通の記録は全部数えられるので inexact は空。長期の記録と同じ形）。"""
        return {"counts": self.density(t0, t1, width, streams), "inexact": []}

    def density(self, t0: int, t1: int, width: int, streams: list[str] | None = None) -> list[int]:
        """画素ごとの出来事の件数（縮図・タイムラインの要約）。"""
        if t1 <= t0 or width < 1:
            raise ValueError("要約の区間が空")
        # 画素ごとに範囲の件数を索引（stream, t）で数える（式でまとめると並べ直しが要り、100 万件で遅い）
        span = t1 - t0
        return [self.count_events(t0 + px * span // width, t0 + (px + 1) * span // width, streams)
                for px in range(width)]

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
        return self._decode_chunk(chunk, numpy=False)

    def _transitions_np(self, chunk: _Chunk) -> Transitions:
        """_transitions と同じ中身を、写さずに numpy の配列（値 uint16・開始 uint64）で（numpy がある時だけ）。"""
        return self._decode_chunk(chunk, numpy=True)

    def _decode_chunk(self, chunk: _Chunk, numpy: bool) -> Transitions:
        """かたまりのファイルを変化点にする。見出しの format で形式を分ける: 無し = 変化点（値の列 + 開始の列）、
        2 = 生の拍（stream_transitions で変化点にする。かたまりの頭が続いている値と同じなら頭に (previous, 0) を足す:
        変化点の形式と同じ変化点になる）。"""
        from .la_wave import _array_of, _np, stream_transitions
        data = self._chunk_file(chunk).read_bytes()
        newline = data.index(b"\n")
        head = _head_of(data[:newline])
        skew = tuple(head.get("channel_skew", ()))                  # 古い記録（ずれの無いもの）は ()
        if head.get("format", 1) == 2:
            values, starts = stream_transitions(memoryview(data)[newline + 1:], head["rle"], head["pack"], 0, head["previous"])
            previous = head["previous"]
            if previous is not None and (not len(starts) or int(starts[0]) != 0):
                if _np is not None:
                    values = _np.concatenate([_np.array([previous], dtype=_np.uint16), values])
                    starts = _np.concatenate([_np.zeros(1, dtype=_np.uint64), starts])
                else:
                    values, starts = array("H", [previous]) + values, array("Q", [0]) + starts
            if not numpy and _np is not None:
                values, starts = _array_of("H", values), _array_of("Q", starts)
            return Transitions(values, starts, chunk.length, 0, 0, head["period_s"], head["channels"], skew)
        n = head["count"]
        if numpy:
            values = _np.frombuffer(data, dtype=_np.uint16, count=n, offset=newline + 1)
            starts = _np.frombuffer(data, dtype=_np.uint64, count=n, offset=newline + 1 + 2 * n)
        else:
            values = array("H")
            values.frombytes(data[newline + 1:newline + 1 + 2 * n])
            starts = array("Q")
            starts.frombytes(data[newline + 1 + 2 * n:newline + 1 + 10 * n])
        return Transitions(values, starts, head["length"], head["trigger"], 0, head["period_s"], head["channels"], skew)

    def chunk_skew(self, chunk: _Chunk) -> tuple[float | None, ...]:
        """かたまりの ch ごとのずれ（見出しの行だけを読む。変化点は読まない）。"""
        with self._chunk_file(chunk).open("rb") as f:
            return tuple(_head_of(f.readline()).get("channel_skew", ()))

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
        out: dict[str, array] = {}
        for stream, _kind, b, n, _lo, _hi in self._table.group_counts(start, start + width * count, width, streams):
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
        wave = Waveform(self._transitions(chunk), chunk.ticks_per_sample)   # 作るのはロックの外で（数百 ms かかる）
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
        table = self._table.physical
        lo, = self._db.execute(f"SELECT MIN(t) FROM {table}").fetchone()      # 1 文に 1 つ（並べると索引を使わない）
        hi, = self._db.execute(f"SELECT MAX(t) FROM {table}").fetchone()
        hi_end, = self._db.execute(f"SELECT MAX(t_end) FROM {table} WHERE t_end IS NOT NULL").fetchone()
        hi = hi_end if hi is None or (hi_end is not None and hi_end > hi) else hi
        clo, chi = self._db.execute("SELECT MIN(t0), MAX(t1) FROM chunks").fetchone()
        values_lo = [v for v in (lo, clo) if v is not None]
        values_hi = [v for v in (hi, chi) if v is not None]
        return (min(values_lo), max(values_hi)) if values_lo else None

    # --- 書き出し --------------------------------------------------------------------

    def export_vcd(self, path: Path, channels: list[int] | None = None) -> None:
        """波形と FPGA の観測の信号を VCD に（時刻は ns か、丸めずに書ける最も粗い ps の単位 = vcd_unit。タイムベースは
        10 の冪とは限らないため。800 MS/s の波形は 10 ps）。FPGA の信号は記録の信号の定義（fpga.signals）で信号ごとに
        `fpga_<組>_<名前>`、定義の無い組は 16 ビットの `fpga_<組>`。ずれが分からずずらさずに置いた ch は $comment に書く。
        変化は波形と FPGA の出来事を時刻順に流しながら合わせて書く（長い記録でも全部をメモリに載せない）。
        numpy があれば波形の変化点はかたまりごとにまとめて行にする（fprobe.recording_vcd。同じバイト列）。"""
        from . import recording_vcd
        from .timeline_fpga import _lanes, signals_of           # timeline_fpga はこのモジュールを読む

        hz = self.meta["timebase_hz"]
        chunks = self._chunks()
        count = max((c.channels for c in chunks), default=0)
        wanted = channels if channels is not None else list(range(count))
        ids = {ch: _vcd_id(i) for i, ch in enumerate(wanted)}
        tick_ns = 1e9 / hz
        skews = [self.chunk_skew(c) for c in chunks]
        unit, (unit_count, unit_name), exact = vcd_unit(
            [tick_ns, *(c.ticks_per_sample * tick_ns for c in chunks),
             *(v * tick_ns for skew in skews for v in skew if v is not None)])
        scale = tick_ns / unit                                   # タイムベースの刻み → VCD の単位
        unknown = sorted({ch for skew in skews for ch in skew_unknown(skew, len(skew)) if ch in ids})
        lines = [f"$timescale {unit_count} {unit_name} $end", *vcd_comments(exact, unknown),
                 "$scope module fprobe $end"]
        lines += [f"$var wire 1 {ids[ch]} ch{ch} $end" for ch in wanted]
        groups = self._distinct_a("fpga", "value")
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
            # ch ごとの時刻のずれ（channel_skew）で、かたまりの終わりの変化が次のかたまりの始まりより後になりうるので、
            # 次のかたまりの最も早い変化より前の分だけを出し、残りは次と合わせて並べる
            previous, held, seq = None, [], 0
            wave_ids = {ch: ids[ch] for ch in wanted}
            trs = [self._transitions(c) for c in chunks[:1]]
            for k, c in enumerate(chunks):
                tr = trs[0]
                trs = [self._transitions(chunks[k + 1])] if k + 1 < len(chunks) else []
                blocks, previous = vcd_blocks_skewed(tr.values, tr.starts, wave_ids, previous,
                                                     tr.skew_samples(c.ticks_per_sample))
                for start, text in blocks:
                    heapq.heappush(held, (round((c.t0 + start * c.ticks_per_sample) * scale), seq, text))
                    seq += 1
                if trs:
                    earliest = chunks[k + 1].t0 + min([0.0, *((v or 0.0) for v in trs[0].channel_skew)])
                    limit = round(earliest * scale)
                else:
                    limit = None
                while held and (limit is None or held[0][0] < limit):
                    t, _, text = heapq.heappop(held)
                    yield t, text

        def fpga() -> Iterator[tuple[int, str]]:
            if not plan:
                return
            last: dict[str, int] = {}
            for t, _rowid, _stream, kind, a, b in self._rows(None, None, ["fpga"], None, columns="a, b"):
                untimed = kind.endswith("_untimed")                # 時刻の決まらない記録の間も不明
                if kind in ("group", "gap") or untimed:            # 観測していない信号は不明（x）
                    for g, items in plan.items():
                        for ident, width, _sig in items:
                            if (kind == "gap" or untimed or g != a) and ident in last:
                                del last[ident]
                                yield round(t * scale), (f"x{ident}" if width == 1 else f"bx {ident}")
                    continue
                if kind != "value":
                    continue
                for ident, width, sig in plan[a]:
                    v = sig.extract(b) if sig is not None else b
                    if last.get(ident) == v:
                        continue
                    last[ident] = v
                    yield round(t * scale), (f"{v}{ident}" if width == 1 else f"b{v:0{width}b} {ident}")

        if recording_vcd.fast_ok(wanted, ids):
            def read(c: _Chunk) -> tuple:
                tr = self._transitions_np(c)
                return tr.values, tr.starts, tr.skew_samples(c.ticks_per_sample), tr.channel_skew
            recording_vcd.write_fast(path, lines, chunks, read, wanted, ids, scale, fpga())
            return
        with Path(path).open("w", encoding="ascii") as f:     # 改行は OS の既定（前の write_text と同じ）
            f.write("\n".join(lines) + "\n")
            current = None
            # 同じ時刻では波形の変化が先、FPGA の変化が後（heapq.merge は同じ鍵で前の並びを先に出す）
            for t, text in heapq.merge(wave(), fpga(), key=lambda x: x[0]):
                if t != current:
                    f.write(f"#{t}\n")
                    current = t
                f.write(text + "\n")

    def _export_rows(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                     kinds: list[str] | None = None, limit: int | None = None) -> Iterator[tuple]:
        """events と同じ絞り込み・並びの (t, rowid, stream, kind, a, b, body の JSON の文字列, t_end, unc) を読みながら。本体は
        記録に書いた JSON の文字列のまま（json.dumps(…, ensure_ascii=False) の文字列は、読んで書き直しても同じ文字列）。"""
        rows = self._rows(t0, t1, streams, kinds)
        return rows if limit is None else itertools.islice(rows, max(0, int(limit)))

    def export_csv(self, path: Path, **filters: Any) -> None:
        """出来事を CSV に（読みながら書く。本体の欄は JSON）。"""
        with Path(path).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "t_end", "stream", "kind", "a", "b", "body", "uncertainty"])
            w.writerows([t, "" if te is None else te, s, k, a, b, "" if body is None else body, "" if unc is None else unc]
                        for t, _rowid, s, k, a, b, body, te, unc in self._export_rows(**filters))

    def export_json(self, path: Path, **filters: Any) -> None:
        """出来事を JSON の並びに（json.dumps([asdict(出来事), …], ensure_ascii=False) と同じ文字列を、読みながら書く）。"""
        enc = json.JSONEncoder(ensure_ascii=False).encode
        with Path(path).open("w", encoding="utf-8") as f:
            f.write("[")
            first = True
            for t, _rowid, s, k, a, b, body, te, unc in self._export_rows(**filters):
                f.write(('{"t": %s, "stream": %s, "kind": %s, "a": %s, "b": %s, "body": %s, "t_end": %s, "uncertainty": %s}'
                         if first else
                         ', {"t": %s, "stream": %s, "kind": %s, "a": %s, "b": %s, "body": %s, "t_end": %s, "uncertainty": %s}')
                        % (enc(t), enc(s), enc(k), enc(a), enc(b), "null" if body is None else body, enc(te), enc(unc)))
                first = False
            f.write("]")
