"""ブレークポイントの表。ソフトウェア（ターゲットのメモリに BKPT を書く）とハードウェア（デバッグユニットの BRP）。

ソフトウェアのブレークポイントは**物理アドレス**で管理する。共有ページ（ライブラリのテキストなど）は複数のプロセスが別の
仮想アドレスでマップしており、同じ命令に 2 つ目の BKPT を「元の命令」として記録してしまうと、削除後に BKPT が残る。
所有者（pid, 仮想アドレス, 種別）を数え、最後の所有者が外れたときだけ元の命令を書き戻す。

方式（`sw`/`hw`）は呼び出し側が明示する。ハードウェアのスロットが無いとき、黙ってソフトウェアに切り替えない（NoFreeSlot）。
クラッシュ対策: BKPT を書く前に元のバイトをジャーナルに書き、`recover()` で書き戻せる。
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from .core_access import CoreAccess
from .mmu import Unmapped
from .usermem import UserMem

BKPT = {4: bytes.fromhex("700020e1"), 2: bytes.fromhex("00be")}      # ARM bkpt #0 / Thumb bkpt #0
UNMAPPED_POLICIES = ("error", "hw")


class NoFreeSlot(RuntimeError):
    """ハードウェアのブレークポイント（またはウォッチポイント）の空きが無い。"""


@dataclass(frozen=True)
class JournalEntry:
    pa: int
    width: int
    orig: bytes
    owners: tuple = ()                            # ((pid, va), ...) 後から調べるための印


@dataclass(frozen=True)
class RecoverResult:
    restored: int                                 # 元の命令を書き戻した数
    stale: int                                    # BKPT がもう無かった（メモリが再利用された）ので書かなかった数


@dataclass(frozen=True)
class Bp:
    """エンジンが見るブレークポイントの事実。user = 利用者が置いた、tmp = エンジンの内部（run-to 等）。"""
    va: int
    width: int
    hw: bool
    user: bool
    tmp: bool


@dataclass
class _Sw:
    pa: int
    width: int
    orig: bytes
    owners: set = field(default_factory=set)      # {(pid, va, tmp)}


def _write_journal(path: str | None, entries: list[JournalEntry]) -> None:
    if not path:
        return
    if not entries:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"entries": [{"pa": e.pa, "width": e.width, "orig": e.orig.hex(), "owners": [list(o) for o in e.owners]}
                               for e in entries]}, f)
    os.replace(tmp, path)


def recover(cores: CoreAccess, journal: str) -> RecoverResult:
    """死んだデバッガが残した BKPT を、ジャーナルの元の命令で書き戻す。**今も BKPT がある所だけ**書く: その物理ページが
    カーネルに回収されて別の用途に再利用されていたら、古い命令を書くと他人のメモリを壊す。書かなかった数も返す（隠さない）。"""
    if not os.path.exists(journal):
        return RecoverResult(0, 0)
    with open(journal) as f:
        entries = json.load(f)["entries"]
    restored = stale = 0
    for e in entries:
        width = e["width"]
        if cores.read_phys(e["pa"], width) == BKPT[width]:
            cores.write_phys(e["pa"], bytes.fromhex(e["orig"]))
            restored += 1
        else:
            stale += 1
    cores.sync_caches()
    os.remove(journal)
    return RecoverResult(restored, stale)


class BreakpointTable:
    def __init__(self, cores: CoreAccess, *, journal: str | None, hw_slots: int, unmapped: str):
        """hw_slots: コアごとのハードウェア BP の数（DBGDIDR から取った事実を呼び出し側が渡す）。
        unmapped: マップされていないページへの sw ブレークポイントの扱い。"error" = Unmapped を上げる／"hw" = ハードウェアに替える。
        既定なし（呼び出し側が選ぶ）。"""
        if unmapped not in UNMAPPED_POLICIES:
            raise ValueError("unmapped must be one of %s" % (UNMAPPED_POLICIES,))
        self.c, self.journal, self.hw_slots, self.unmapped = cores, journal, hw_slots, unmapped
        self._mems: dict[int, UserMem] = {}
        self._sw: dict[int, _Sw] = {}                      # 物理アドレス → 状態
        self._hw: dict[int, tuple[int, set]] = {}          # 仮想アドレス → (スロット, 所有者 {(pid, tmp)})
        self.code_dirty = False
        self.stale: list[JournalEntry] = []                # 書き戻そうとしたが BKPT がもう無かった所（メモリが再利用された）

    def attach(self, pid: int, mem: UserMem) -> None:
        self._mems[pid] = mem

    def mem(self, pid: int) -> UserMem:
        return self._mems[pid]

    def get_hw(self, va: int):
        return self._hw.get(va)

    def lookup(self, pid: int, va: int) -> Bp | None:
        """pid のプロセスの仮想アドレス va にあるブレークポイント。無ければ None。"""
        hw_tmps = []
        h = self._hw.get(va)
        if h is not None:
            hw_tmps = [tmp for p, tmp in h[1] if p == pid]
        sw_tmps, width = [], 4
        mem = self._mems.get(pid)
        if mem is not None:
            try:
                s = self._sw.get(mem.pa(va))
            except Unmapped:
                s = None
            if s is not None:
                sw_tmps = [tmp for p, v, tmp in s.owners if p == pid and v == va]
                width = s.width
        tmps = hw_tmps + sw_tmps
        if not tmps:
            return None
        # 同じ番地にハードウェアとソフトウェアが両方あれば、1 つの出来事として扱う（利用者のものがあれば利用者の停止）
        return Bp(va, width, bool(hw_tmps), any(not t for t in tmps), any(tmps))

    def entries(self, pid: int) -> list[Bp]:
        """pid のプロセスの全ブレークポイント（仮想アドレスの順）。monitor bps 用。"""
        out = []
        for va, (slot, owners) in self._hw.items():
            mine = [tmp for p, tmp in owners if p == pid]
            if mine:
                out.append(Bp(va, 4, True, any(not t for t in mine), any(mine)))
        for s in self._sw.values():
            for va in {v for p, v, _ in s.owners if p == pid}:
                tmps = [t for p, v, t in s.owners if p == pid and v == va]
                out.append(Bp(va, s.width, False, any(not t for t in tmps), any(tmps)))
        return sorted(out, key=lambda b: b.va)

    def sw_at_pa(self, pa: int) -> Bp | None:
        """物理アドレスにあるソフトウェアのブレークポイント（別のプロセスの pc を物理に直して引く）。"""
        s = self._sw.get(pa)
        if s is None:
            return None
        return Bp(0, s.width, False, any(not t for _, _, t in s.owners), any(t for _, _, t in s.owners))

    @contextmanager
    def lifted_pa(self, pa: int):
        s = self._sw.get(pa)
        if s is None:
            yield
            return
        self.c.write_phys(pa, s.orig)
        self.code_dirty = True
        try:
            yield
        finally:
            self.c.write_phys(pa, BKPT[s.width])
            self.code_dirty = True

    def pending_journal(self) -> list[JournalEntry]:
        return [JournalEntry(s.pa, s.width, s.orig, tuple(sorted((p, v) for p, v, _ in s.owners)))
                for s in self._sw.values()]

    def _save(self) -> None:
        _write_journal(self.journal, self.pending_journal())

    # --- 挿入 --------------------------------------------------------------------------------------------
    def insert(self, pid: int, va: int, *, kind: str, width: int, tmp: bool = False) -> None:
        if kind not in ("sw", "hw"):
            raise ValueError("kind must be 'sw' or 'hw'")
        if kind == "sw":
            try:
                pa = self._mems[pid].pa(va)
            except Unmapped:
                if self.unmapped != "hw":
                    raise
                kind = "hw"                                 # 呼び出し側が選んだ切り替え
            else:
                self._insert_sw(pid, va, pa, width, tmp)
                return
        self._insert_hw(pid, va, width, tmp)

    def _insert_sw(self, pid: int, va: int, pa: int, width: int, tmp: bool) -> None:
        s = self._sw.get(pa)
        if s is None:
            orig = self.c.read_phys(pa, width)
            s = _Sw(pa, width, orig, {(pid, va, tmp)})
            self._sw[pa] = s                                # ジャーナルは書く前に載せる
            try:
                self._save()
                self.c.write_phys(pa, BKPT[width])
            except Exception:
                del self._sw[pa]
                self._save()
                raise
            self.code_dirty = True
        s.owners.add((pid, va, tmp))

    def _insert_hw(self, pid: int, va: int, width: int, tmp: bool) -> None:
        h = self._hw.get(va)
        if h is None:
            used = {slot for slot, _ in self._hw.values()}
            free = [i for i in range(self.hw_slots) if i not in used]
            if not free:
                raise NoFreeSlot("no free hardware breakpoint (%d per core)" % self.hw_slots)
            slot = free[0]
            for n in range(self.c.n_cores):
                self.c.set_hw_bp(n, slot, va, width)
            h = self._hw[va] = (slot, set())
        h[1].add((pid, tmp))

    # --- 削除 --------------------------------------------------------------------------------------------
    def _restore(self, s: _Sw) -> None:
        """元の命令を書き戻す。ただし今も BKPT がある所だけ: ページが回収されて再利用されていたら、書くと他人のメモリを壊す。
        書かなかった所は `stale` に残す（隠さない）。"""
        if self.c.read_phys(s.pa, s.width) == BKPT[s.width]:
            self.c.write_phys(s.pa, s.orig)
            self.code_dirty = True
        else:
            self.stale.append(JournalEntry(s.pa, s.width, s.orig, tuple(sorted((p, v) for p, v, _ in s.owners))))

    def remove(self, pid: int, va: int, *, kind: str, tmp: bool = False) -> None:
        """kind（"sw" / "hw"）は insert と同じものを必ず言う。同じ番地にハードウェアとソフトウェアが両方ある場合に、片方が
        もう片方を巻き込んだり、取り残したりしないため。"""
        if kind not in ("sw", "hw"):
            raise ValueError("kind must be 'sw' or 'hw'")
        if kind == "hw":
            h = self._hw.get(va)
            if h is None:
                return
            h[1].discard((pid, tmp))
            if not h[1]:
                for n in range(self.c.n_cores):
                    self.c.clear_hw_bp(n, h[0])
                del self._hw[va]
            return
        # 所有者の組から探す（ページのウォークは要らない: ページが無くなっていても所有者は外せる）
        s = next((x for x in self._sw.values() if (pid, va, tmp) in x.owners), None)
        if s is None:
            return
        s.owners.discard((pid, va, tmp))
        if s.owners:
            return
        self._restore(s)
        del self._sw[s.pa]
        self._save()

    def clear(self) -> None:
        for s in list(self._sw.values()):
            s.owners.clear()
            self._restore(s)
            del self._sw[s.pa]
        for va, (slot, _) in list(self._hw.items()):
            for n in range(self.c.n_cores):
                self.c.clear_hw_bp(n, slot)
            del self._hw[va]
        self._save()

    # --- ステップ・読み書き -------------------------------------------------------------------------------
    @contextmanager
    def lifted(self, pid: int, va: int):
        """sw ブレークポイントの所で 1 命令進めるあいだだけ、元の命令を見せる。"""
        try:
            pa = self._mems[pid].pa(va)
        except Unmapped:
            yield
            return
        s = self._sw.get(pa)
        if s is None:
            yield
            return
        self.c.write_phys(pa, s.orig)
        self.code_dirty = True
        try:
            yield
        finally:
            self.c.write_phys(pa, BKPT[s.width])
            self.code_dirty = True

    def shadow(self, pid: int, va: int, data: bytes) -> bytes:
        """va から読んだ data の中の BKPT のバイトを、元の命令のバイトに置き換える（物理アドレスで突き合わせる）。"""
        b = bytearray(data)
        mem = self._mems[pid]
        for i in range(len(b)):
            try:
                pa = mem.pa(va + i)
            except Unmapped:
                continue
            for s in self._sw.values():
                if s.pa <= pa < s.pa + s.width:
                    b[i] = s.orig[pa - s.pa]
        return bytes(b)

    def write(self, pid: int, va: int, data: bytes) -> None:
        """入れてあるブレークポイントを壊さずに書く。新しいバイトが「元の命令」になり、BKPT は残る。メモリへの書き込みが
        成功してから「元の命令」を更新する（失敗したら、記録とメモリが食い違わない）。"""
        buf = bytearray(data)
        mem = self._mems[pid]
        updates = []
        for i in range(len(buf)):
            try:
                pa = mem.pa(va + i)
            except Unmapped:
                continue
            for s in self._sw.values():
                if s.pa <= pa < s.pa + s.width:
                    updates.append((s, pa - s.pa, buf[i]))
                    buf[i] = BKPT[s.width][pa - s.pa]
        mem.write(va, bytes(buf))
        for s, idx, byte in updates:
            orig = bytearray(s.orig)
            orig[idx] = byte
            s.orig = bytes(orig)
        self.code_dirty = True
        self._save()
