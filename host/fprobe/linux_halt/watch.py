"""ハードウェアのウォッチポイントを全コアに入れる。

ウォッチポイントは仮想アドレスで一致するので、当たりはどのプロセスからでも起こりうる（誰の当たりかは engine が決める）。
デバッグ対象のスレッドはどのコアでも走るので、全コアに入れる。途中で失敗したら（あるコアに空きが無い）戻して、コア間で食い違わせない。
"""
from __future__ import annotations

from dataclasses import dataclass

from .bp import NoFreeSlot
from .core_access import CoreAccess

KINDS = ("w", "r", "a")                         # 書き込み・読み出し・両方（RSP の Z2, Z3, Z4）
_CORE_KIND = {"w": "w", "r": "r", "a": "rw"}


@dataclass(frozen=True)
class Watch:
    addr: int
    length: int
    kind: str


class WatchTable:
    def __init__(self, cores: CoreAccess, *, hw_slots: int):
        self.c, self.hw_slots = cores, hw_slots
        self.t: dict[Watch, int] = {}              # ウォッチ → スロット

    def __iter__(self):
        return iter(list(self.t))

    def __len__(self) -> int:
        return len(self.t)

    def _set(self, n: int, w: Watch, slot: int) -> None:
        self.c.set_hw_watch(n, slot, w.addr, w.length, _CORE_KIND[w.kind])

    def add(self, addr: int, length: int, kind: str) -> Watch:
        if kind not in KINDS:
            raise ValueError("watch kind must be one of %s" % (KINDS,))
        w = Watch(addr, length, kind)
        if w in self.t:
            return w
        free = [i for i in range(self.hw_slots) if i not in self.t.values()]
        if not free:
            raise NoFreeSlot("no free hardware watchpoint (%d per core)" % self.hw_slots)
        slot = free[0]
        done = []
        try:
            for n in range(self.c.n_cores):
                self._set(n, w, slot)
                done.append(n)
        except Exception:
            for n in done:                          # 全コアか、無しか
                self.c.clear_hw_watch(n, slot)
            raise
        self.t[w] = slot
        return w

    def remove(self, addr: int, length: int, kind: str) -> None:
        w = Watch(addr, length, kind)
        slot = self.t.get(w)
        if slot is None:
            return
        for n in range(self.c.n_cores):
            self.c.clear_hw_watch(n, slot)
        del self.t[w]

    def clear(self) -> None:
        for w in list(self.t):
            self.remove(w.addr, w.length, w.kind)

    # 1 コアだけ: 同期ウォッチポイントの命令を、ウォッチを外して 1 命令進めるために使う
    def suspend(self, n: int) -> None:
        for slot in self.t.values():
            self.c.clear_hw_watch(n, slot)

    def restore(self, n: int) -> None:
        for w, slot in self.t.items():
            self._set(n, w, slot)
