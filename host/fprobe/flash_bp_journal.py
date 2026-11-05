"""Flash ブレークポイントの書き換えの記録（Journal。fprobe.flash_bp から分けた、ファイルだけを触る部分）。

Flash に BKPT を書く前にここへ記録しておき、異常終了で残った BKPT を次の接続で元に戻せるようにする。
fprobe.flash_bp が Journal・JournalError を再公開する。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Journal", "JournalError", "WrittenBkpt"]


@dataclass(frozen=True)
class WrittenBkpt:
    """Flash に書いた BKPT: 元の中身と、書いたときの周りの元の中身（記録の照合用）。"""
    address: int
    original: bytes
    window: int
    window_original: bytes

    def as_dict(self) -> dict:
        return {"address": self.address, "original": self.original.hex(), "window": self.window,
                "window_original": self.window_original.hex()}

    @classmethod
    def from_dict(cls, d: dict) -> "WrittenBkpt":
        return cls(d["address"], bytes.fromhex(d["original"]), d["window"], bytes.fromhex(d["window_original"]))


class JournalError(Exception):
    """記録（Journal）が読めない・形が違う。"""


class Journal:
    """書き換えの記録（JSON。書き換えの前に書く）。key: どのターゲットのものか（ターゲットの名前等）。"""

    def __init__(self, path: str | Path, key: str):
        self.path = Path(path)
        self.key = key

    def save(self, entries) -> None:
        entries = sorted(entries, key=lambda e: e.address)
        if not entries:
            self.clear()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "target": self.key,
                                         "entries": [e.as_dict() for e in entries]}, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)                       # 途中で落ちても前の記録か新しい記録のどちらか

    def load(self) -> list[WrittenBkpt] | None:
        """このターゲットの記録。無い・別のターゲットのものなら None。壊れていれば JournalError。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as e:                     # JSON が不正・文字コードが不正・読めない
            raise JournalError(f"記録 {self.path} を読めない: {e}") from e
        if not isinstance(data, dict):
            raise JournalError(f"記録 {self.path} の形が違う（JSON のオブジェクトでない）")
        if data.get("target") != self.key:
            return None
        try:
            return [WrittenBkpt.from_dict(e) for e in data.get("entries", [])]
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise JournalError(f"記録 {self.path} の中身が違う: {e!r}") from e

    def set_aside(self) -> Path:
        """壊れた記録を .bad に改名して退避する（捨てずに残す。次の保存は新しい記録に書く）。退避先を返す。"""
        bad = self.path.with_suffix(self.path.suffix + ".bad")
        os.replace(self.path, bad)
        return bad

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
