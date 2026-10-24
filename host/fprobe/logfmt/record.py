"""解読したログの 1 行（`LogRecord`）と、記録（stream "log"）への書き方。

形式（defmt・Trice・compact・text）に依らず同じ形にする。記録では kind = 重要度（trace / debug / info / warn /
error / println。重要度の無い形式は "log"）、解読できなかったバイトは kind "undecoded"（body に hex と理由）。
a = 書式の ID（分かれば）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .addresses import AddressHit

__all__ = ["LEVELS", "LogRecord", "STREAM", "level_at_least"]

STREAM = "log"
LEVELS = ("trace", "debug", "info", "warn", "error")          # 低い順。println 等の重要度の無いものは絞り込みで常に通す
UNDECODED = "undecoded"


def level_at_least(level: str | None, minimum: str | None) -> bool:
    if minimum is None or level not in LEVELS:
        return True
    return LEVELS.index(level) >= LEVELS.index(minimum)


@dataclass(frozen=True)
class LogRecord:
    text: str                                   # 整形した本文（解読できなければ空）
    format: str                                 # defmt / trice / compact / text
    level: str | None = None
    id: int | None = None                       # 書式の番号（defmt の表の番号・Trice の ID・文字列の番地）
    template: str | None = None                 # 書式の文字列
    args: tuple = ()
    timestamp: int | float | None = None        # ターゲットが付けた時刻（形式の単位のまま）
    timestamp_text: str | None = None           # 形式の決まりで整形した時刻（defmt の {=u64:us} 等）
    location: dict | None = None                # file・line・module・function（分かるもの）
    channel: str | None = None                  # Trice の "info:" 等のチャンネル
    addresses: tuple[AddressHit, ...] = ()      # 本文の中の 0x の数のうち実行区画に入る値
    error: str | None = None                    # 解読できなかった理由（このとき raw に生のバイト）
    raw: bytes | None = None
    notes: tuple[str, ...] = field(default=())
    cut_at: int | None = None                   # text: 改行の来ないままこのバイト数（使った上限）で切った行。
                                                # defmt・trice: 区切りの来ないまま上限（このバイト数）を超えて捨てた
    continued: bool = False                     # text: 前に上限で切った行の続き

    @property
    def kind(self) -> str:
        if self.error is not None:
            return UNDECODED
        return self.level or "log"

    def to_dict(self) -> dict:
        out: dict = {"format": self.format, "kind": self.kind, "text": self.text}
        for key in ("level", "id", "template", "timestamp", "timestamp_text", "location", "channel", "error"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.args:
            out["args"] = [_jsonable(a) for a in self.args]
        if self.addresses:
            out["addresses"] = [h.to_dict() for h in self.addresses]
        if self.raw is not None:
            out["raw"] = self.raw.hex()
        if self.notes:
            out["notes"] = list(self.notes)
        if self.cut_at is not None:
            out["cut_at"] = self.cut_at
        if self.continued:
            out["continued"] = True
        return out


def _jsonable(value):
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and value != value:
        return "nan"
    return value
