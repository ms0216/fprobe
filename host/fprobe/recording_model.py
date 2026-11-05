"""共通の時間軸の記録の部品（recording・recording_write・recording_long が共有する）: 時計の対応付け・出来事・
かたまりの索引・SQLite の接続。仕様は fprobe.recording と同じ。"""

import json
import os
import sqlite3
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .recording_table import EventTable

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


def events_of(rows: Iterable[tuple]) -> Iterator[Event]:
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
class Chunk:
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


def open_table(path: Path) -> tuple[sqlite3.Connection, EventTable]:
    """記録の SQLite を開き、出来事の表の口を作る（表が無ければ新しい版で作る。古い版はそのまま: recording_table）。"""
    db = connect_db(path)
    return db, EventTable.open(db)
