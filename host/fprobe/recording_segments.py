"""長期の記録の部品（recording_long・recording_long_write が共有する）: 方針（Policy）・索引の表・容量の数え方。
全体の仕組みは fprobe.recording_long の説明。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .la_wave import _np
from .recording_model import connect_db

__all__ = ["DEFAULT_DECIMATE", "INDEX_LEVEL", "INDEX_SCHEMA", "SEGMENT_SUMMARY", "WAVEFORM", "Policy", "bytes_of", "bytes_of_index",
           "connect_long", "toggles"]

INDEX_LEVEL = 3                      # この段（約 4 秒）以上の縮図は索引に置く（分割を消しても残る）
DEFAULT_DECIMATE = ("dap", "sample", "swo", "clock", "waveform")    # 間引くときに消す既定（"waveform" = 波形のファイル）
WAVEFORM = "waveform"

INDEX_SCHEMA = """
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
SEGMENT_SUMMARY = """
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


def connect_long(path: Path, schema: str = "") -> sqlite3.Connection:
    return connect_db(path, schema, timeout=10)


def bytes_of(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()) if directory.exists() else 0


def toggles(values) -> int:
    """値の列の中で変化したビット。あるビットが変化する = 最初の値と違う値が出る（異なる値だけ見れば足りる）。
    values は array・列・numpy の配列（numpy なら一括で）。"""
    if not len(values):
        return 0
    first = int(values[0])
    if _np is not None and isinstance(values, _np.ndarray):
        return int(_np.bitwise_or.reduce(values ^ values.dtype.type(first)))
    out = 0
    for v in set(values):
        out |= int(v) ^ first
    return out


def bytes_of_index(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.glob("index.sqlite*") if p.is_file())
