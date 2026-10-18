"""記録の SQL 問い合わせ（読み取り専用）: 共通の時間軸の記録（recording.sqlite）を AI・人が SQL で集計する。

    query(directory, "SELECT kind, COUNT(*) FROM events WHERE stream = 'uart' GROUP BY kind")

安全の作り（記録は調査の証拠なので、問い合わせで変えない・機器の道具を待たせない）:
- 読み取り専用の接続（URI の mode=ro）に加えて、承認の関数（authorizer）で読み（SELECT・表の読み・関数・再帰の WITH）
  以外をすべて断る。PRAGMA・ATTACH・一時表・トランザクションも断る。文は 1 つだけ。
- 時間の上限（進みの関数で打ち切る）と件数の上限（それ以上は読まずに truncated）。

表（recording.py の _SCHEMA が正）:
- events(t, t_end, stream, kind, a, b, body, unc): 出来事。時刻はタイムベース、body は JSON（json_extract で読む）。
- chunks(t0, t1, path, ticks_per_sample, channels, length): 波形のかたまりの索引。
関数: timebase_hz()（記録のタイムベースの Hz）、seconds(ticks)（タイムベース → 秒）。

長期の記録（分割した記録。fprobe.recording_long）は、t0 / t1 に重なる分割（max_segments 個まで）を読み取り専用で
つなぎ、events・chunks を分割をまたぐ一時のビューにする（chunks の path は記録のディレクトリからの位置）。
表 segments（分割の一覧）と summary（粗い縮図）も読める。t0 / t1 を省くと新しい分割から max_segments 個
（それより多ければ partial）。範囲に max_segments より多くの分割があれば断る（範囲を絞る）。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_LIMIT", "DEFAULT_TIMEOUT_S", "MAX_LIMIT", "MAX_SEGMENTS", "MAX_TIMEOUT_S", "QueryError", "query"]

DEFAULT_LIMIT = 1000
MAX_LIMIT = 100_000
DEFAULT_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 60.0
MAX_SEGMENTS = 8                           # 一度につなぐ分割の数（SQLite の ATTACH の上限 10 − 索引）
PROGRESS_STEPS = 1000                      # 進みの関数を呼ぶ間隔（SQLite の仮想機械の命令数）

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


class QueryError(ValueError):
    """問い合わせが誤り・読み以外・時間切れ。"""


def _authorize(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def _cell(value: Any) -> Any:
    return value.hex() if isinstance(value, bytes) else value


def _ro(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _segmented(directory: Path, t0: int | None, t1: int | None, max_segments: int) -> tuple[sqlite3.Connection, dict]:
    """分割をつないだ接続（承認の関数を付ける前に ATTACH と一時のビューを作る）。"""
    if not 1 <= max_segments <= MAX_SEGMENTS:
        raise ValueError(f"max_segments は 1〜{MAX_SEGMENTS}")
    index = sqlite3.connect(_ro(directory / "index.sqlite"), uri=True)
    try:
        rows = index.execute("SELECT no, path, t0, t1 FROM segments WHERE state != 'deleted' ORDER BY no").fetchall()
    finally:
        index.close()
    ranged = t0 is not None or t1 is not None
    # 開いている分割は索引に範囲がまだ無い（いつも含める）
    chosen = [r for r in rows if r[2] is None or ((t0 is None or r[3] >= t0) and (t1 is None or r[2] < t1))]
    if ranged and len(chosen) > max_segments:
        raise QueryError(f"範囲に分割が {len(chosen)} 個ある（一度に {max_segments} 個まで）: t0 / t1 で絞る"
                         "（全体の件数は recording_overview / recording_density）")
    partial = len(chosen) > max_segments
    chosen = chosen[-max_segments:]
    db = sqlite3.connect(":memory:", uri=True, check_same_thread=False)
    db.execute("ATTACH DATABASE ? AS idx", (_ro(directory / "index.sqlite"),))
    for i, (no, path, *_rest) in enumerate(chosen):
        db.execute(f"ATTACH DATABASE ? AS s{i}", (_ro(directory / path / "recording.sqlite"),))
    if chosen:
        db.execute("CREATE TEMP VIEW events AS " + " UNION ALL ".join(
            f"SELECT t, t_end, stream, kind, a, b, body, unc FROM s{i}.events" for i in range(len(chosen))))
        db.execute("CREATE TEMP VIEW chunks AS " + " UNION ALL ".join(
            f"SELECT t0, t1, '{path}/waveform/' || path AS path, ticks_per_sample, channels, length FROM s{i}.chunks"
            for i, (_, path, *_r) in enumerate(chosen)))
    else:
        db.execute("CREATE TEMP VIEW events AS SELECT NULL AS t, NULL AS t_end, NULL AS stream, NULL AS kind, "
                   "NULL AS a, NULL AS b, NULL AS body, NULL AS unc WHERE 0")
        db.execute("CREATE TEMP VIEW chunks AS SELECT NULL AS t0, NULL AS t1, NULL AS path, NULL AS ticks_per_sample, "
                   "NULL AS channels, NULL AS length WHERE 0")
    db.execute("CREATE TEMP VIEW segments AS SELECT * FROM idx.segments")
    db.execute("CREATE TEMP VIEW summary AS SELECT * FROM idx.summary")
    return db, {"segments": [no for no, *_ in chosen], "partial": partial}


def query(directory: Path, sql: str, params: list | dict | None = None, *, limit: int = DEFAULT_LIMIT,
          timeout_s: float = DEFAULT_TIMEOUT_S, t0: int | None = None, t1: int | None = None,
          max_segments: int = MAX_SEGMENTS) -> dict:
    """読み取り専用の SQL を 1 文。{"columns", "rows"（列の並び）, "count", "truncated", "elapsed_s"}。
    長期の記録なら t0 / t1 で読む分割を選び、"segments"（つないだ分割の番号）と "partial" も返す。"""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit は 1〜{MAX_LIMIT}")
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise ValueError(f"timeout_s は 0〜{MAX_TIMEOUT_S} 秒")
    directory = Path(directory)
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    hz = float(meta["timebase_hz"])
    extra: dict = {}
    if meta.get("layout") == "segmented":
        db, extra = _segmented(directory, t0, t1, max_segments)
    else:
        db = sqlite3.connect(_ro(directory / "recording.sqlite"), uri=True, check_same_thread=False)
    try:
        db.create_function("timebase_hz", 0, lambda: hz, deterministic=True)
        db.create_function("seconds", 1, lambda t: None if t is None else t / hz, deterministic=True)
        db.set_authorizer(_authorize)                       # 関数の登録の後（登録そのものは承認を通らない）
        started = time.monotonic()
        deadline = started + timeout_s
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_STEPS)
        try:
            cursor = db.execute(sql, params if params is not None else ())
            if cursor.description is None:
                raise QueryError("結果の無い文（読みの SELECT・WITH だけ）")
            rows = cursor.fetchmany(limit + 1)
        except sqlite3.OperationalError as e:
            if str(e) == "interrupted":
                raise QueryError(f"時間の上限（{timeout_s} 秒）を超えたので打ち切った") from None
            raise QueryError(str(e)) from None
        except (sqlite3.DatabaseError, sqlite3.ProgrammingError, sqlite3.Warning) as e:
            raise QueryError(str(e)) from None
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {"columns": [d[0] for d in cursor.description], "rows": [[_cell(v) for v in r] for r in rows],
                "count": len(rows), "truncated": truncated, "elapsed_s": time.monotonic() - started, **extra}
    finally:
        db.close()
