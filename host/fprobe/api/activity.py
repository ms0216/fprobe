"""操作の記録: 1 操作 = 1 件。JSON Lines のファイルに追記し、購読者へ配る。"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

__all__ = ["ActivityLog", "summarize"]


def summarize(value: Any, limit: int = 200) -> Any:
    """記録に残す形: 長い文字列・列は長さと先頭だけ（入れ子の中も）。"""
    if isinstance(value, (str, bytes)) and len(value) > limit:
        head = value[:limit] if isinstance(value, str) else value[:limit].hex()
        return {"length": len(value), "head": head}
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            return {"length": len(value), "head": [summarize(v, limit) for v in value[:8]]}
        return [summarize(v, limit) for v in value]
    if isinstance(value, dict):                                      # 入れ子の中の大きな値も（ソースの本文等）
        return {k: summarize(v, limit) for k, v in value.items()}
    return value


class ActivityLog:
    def __init__(self, directory: Path, on_entry: Callable[[dict], None]):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        self._on_entry = on_entry
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, entry: dict) -> dict:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "time": datetime.now().isoformat(timespec="milliseconds"), **entry}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self._on_entry(entry)
        return entry

    def read(self, before: int | None = None, limit: int = 100) -> list[dict]:
        """番号が before より小さい記録の、最後の limit 件（番号の小さい順）。"""
        # ponytail: 毎回ファイルを全部読む。1 回の起動の記録（数万件）なら十分速い。遅くなったら末尾から読む
        with self._lock:
            if not self.path.exists():
                return []
            entries = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        if before is not None:
            entries = [e for e in entries if e["seq"] < before]
        return entries[-limit:]
