"""テストポイントの観測の単位: Hit（時刻・ID・値）と Loss（取りこぼし・壊れた記録）。

時刻は整数（ロジアナのサンプル番号・ターゲットのクロック数）で、単位は `Decoded.hz` が持つ。
"""

from dataclasses import dataclass

__all__ = ["Decoded", "Hit", "Item", "Loss"]


@dataclass(frozen=True)
class Hit:
    t: int
    id: int
    value: int | None = None


@dataclass(frozen=True)
class Loss:
    t: int
    reason: str


Item = Hit | Loss


@dataclass
class Decoded:
    items: list[Item]
    hz: float                       # 時刻の単位（1 秒あたりの数）
    wraps: int | None = None        # 時刻の回り込みを仮定して戻した回数（RTT の CYCCNT。回り込みの無い方式は None）

    @property
    def hits(self) -> list[Hit]:
        return [i for i in self.items if isinstance(i, Hit)]

    @property
    def losses(self) -> list[Loss]:
        return [i for i in self.items if isinstance(i, Loss)]
