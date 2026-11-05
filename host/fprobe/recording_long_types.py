"""長期の記録の読み手が使う部品: 分割 1 つの要約 _Segment（番号・時刻の範囲・件数・間引き済みか）と、開いた分割の
sqlite の接続を覚える _Reads。SegmentedRecording（recording_long.py）とその集計（recording_long_view.py）が使う。"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .recording_segments import connect_long

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
            self._dbs[seg.no] = connect_long(self.root / seg.path / "recording.sqlite")
        return self._dbs[seg.no]

    def close(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()
