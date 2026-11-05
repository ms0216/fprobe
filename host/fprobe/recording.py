"""共通の時間軸の記録（F3）: fprobe が観測する出来事を、デバイスのタイムベースの上の 1 つの記録にまとめる。

仕様: docs/superpowers/specs/2026-10-17-f3-timeline-recording-design.md
- 時刻は整数のタイムベース（デバイスの 64 ビットのカウンタ）。ホストで起きた出来事は `ClockMap` で写し、不確かさを持つ。
- 1 回の記録 = 1 つのディレクトリ。索引と小さな出来事は SQLite（recording.sqlite）、波形は追記だけのかたまりのファイル
  （waveform/）、記録の情報は meta.json。
- 書き手 `Recorder`（recording_write）はまとめて書き（flush）、読み手 `Recording`（このモジュール）は記録中でも読める（SQLite の WAL）。
- 部品: recording_model（時計・出来事・接続）・recording_chunk（かたまりのファイルの形式）・recording_write（書き手）・
  recording_export（VCD・CSV・JSON）。
"""

import itertools
import json
import re
import statistics
import threading
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .la import Transitions, Waveform
from .recording_chunk import decode_chunk, head_of
from .recording_export import export_csv, export_json, export_vcd
from .recording_model import (  # noqa: F401  (ClockMap・Event・HOST_TIME_SOURCES は fprobe.recording の公開の名前)
    HOST_TIME_SOURCES, NO_DATA, Chunk, ClockMap, Event, events_of, in_list, open_table, write_json)
from .recording_table import ROW_COLUMNS
from .recording_write import Recorder

__all__ = ["ClockMap", "Event", "Recorder", "Recording"]

WAVE_CACHE_BYTES = 256 << 20          # 記録の読み手が覚えておく変化点の表の大きさの上限（使った順に捨てる）



class Recording:
    """記録の読み手（記録中でも読める）。時刻はタイムベース、t1 は含まない。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
        self._db, self._table = open_table(self.directory / "recording.sqlite")
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
        events = events_of(self._rows(t0, t1, streams, kinds, overlapping, a))
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
        return None if row is None else next(events_of([row]))

    def add(self, stream: str, kind: str, t: int, a: int = 0, b: int = 0, body: Any = None,
            t_end: int | None = None) -> None:
        """閉じた記録に出来事を 1 つ足す（後から置く調査の印。recording_mark の t）。"""
        with self._table.transaction("BEGIN IMMEDIATE"):
            self._table.insert(self._table.encode([(stream, kind, t, a, b, body, t_end)]))
        self._writes += 1

    def replace_derived(self, stream: str, source: str, events: Iterable[Event]) -> int:
        """解析が作った出来事（派生イベント。例: 規則の違反）を書く。同じ stream で body の "source" が同じものを先に
        消す（検査し直すと前の結果が残らない）。各出来事の body（辞書）に source を入れる。書いた件数を返す。"""
        new = [(stream, e.kind, e.t, e.a, e.b, {**(e.body or {}), "source": source}, e.t_end, e.uncertainty)
               for e in events]
        with self._table.transaction("BEGIN IMMEDIATE"):
            self._table.delete([stream], "json_extract(body, '$.source') = ?", [source])
            rows = self._table.encode(new)
            self._table.insert(rows)
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

    def _chunks(self, t0: int | None = None, t1: int | None = None) -> list[Chunk]:
        sql, clauses, args = "SELECT t0, t1, path, ticks_per_sample, channels, length FROM chunks", [], []
        if t0 is not None:
            clauses.append("t1 > ?")
            args.append(t0)
        if t1 is not None:
            clauses.append("t0 < ?")
            args.append(t1)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [Chunk(*row) for row in self._db.execute(sql + " ORDER BY t0", args)]

    def waveforms(self, t0: int | None = None, t1: int | None = None) -> Iterator[tuple[int, float, Transitions]]:
        """範囲 [t0, t1) に重なる波形のかたまりを時刻順に: (サンプル 0 のタイムベース, 1 サンプルのタイムベース, 変化点)。
        かたまりは丸ごと返す（範囲で切るのは使う側）。"""
        for c in self._chunks(t0, t1):
            yield c.t0, c.ticks_per_sample, self._transitions(c)

    def _transitions(self, chunk: Chunk) -> Transitions:
        return self._decode_chunk(chunk, numpy=False)

    def _transitions_np(self, chunk: Chunk) -> Transitions:
        """_transitions と同じ中身を、写さずに numpy の配列（値 uint16・開始 uint64）で（numpy がある時だけ）。"""
        return self._decode_chunk(chunk, numpy=True)

    def _decode_chunk(self, chunk: Chunk, numpy: bool) -> Transitions:
        """かたまりのファイルを変化点にする（形式の分け方は recording_chunk.decode_chunk）。"""
        return decode_chunk(self._chunk_file(chunk).read_bytes(), chunk, numpy)

    def chunk_skew(self, chunk: Chunk) -> tuple[float | None, ...]:
        """かたまりの ch ごとのずれ（見出しの行だけを読む。変化点は読まない）。"""
        with self._chunk_file(chunk).open("rb") as f:
            return tuple(head_of(f.readline()).get("channel_skew", ()))

    def wave_chunks(self, t0: int, t1: int) -> list[tuple[Chunk, bool]]:
        """範囲に重なる波形のかたまりと、ファイルが残っているか（長期の記録は間引くと False）。時刻順。"""
        return [(c, True) for c in self._chunks(t0, t1)]

    def waveform(self, chunk: Chunk) -> Waveform:
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

    def _wave(self, chunk: Chunk) -> Waveform:
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

    def _chunk_file(self, chunk: Chunk) -> Path:
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
        """波形と FPGA の観測の信号を VCD に（fprobe.recording_export.export_vcd。時刻は ns か、丸めずに書ける最も粗い ps の単位）。"""
        export_vcd(self, path, channels)

    def export_csv(self, path: Path, **filters: Any) -> None:
        """出来事を CSV に（読みながら書く。本体の欄は JSON）。"""
        export_csv(self, path, **filters)

    def export_json(self, path: Path, **filters: Any) -> None:
        """出来事を JSON の並びに（読みながら書く）。"""
        export_json(self, path, **filters)
