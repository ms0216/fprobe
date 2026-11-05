"""デバッグ対象の Linux プロセス（スレッドグループ）: タスク一覧と、そのアドレス空間。"""
from __future__ import annotations

import struct

from .core_access import CoreAccess
from .kernel import Kernel, KernelLayoutError, ProcessNotFound, Task
from .usermem import UserMem


class ProcessExited(RuntimeError):
    """デバッグ対象は消えた（task_struct が別の持ち主に使い回されている。中のメモリは別のタスクのもの）。"""


class Process:
    AUXV_MAX = 0x200

    def __init__(self, kernel: Kernel, cores: CoreAccess, tgid: int, *, fmt: str, tsz: int | None = None,
                 region: str | None = None):
        self.k, self.c, self.tgid = kernel, cores, tgid
        kernel.verify()                                  # オフセットが動いているカーネルのものだと確かめてから読む
        leader = kernel.find_leader(tgid)
        if leader is None:
            raise ProcessNotFound("pid %d not found in the kernel task list (the process has exited, or the pid is wrong)" % tgid)
        if leader.mm == 0:
            raise ProcessNotFound("pid %d is a kernel thread (no address space)" % tgid)
        self.leader, self.mm = leader, leader.mm
        self.pgd = kernel.u32(self.mm + kernel.o["pgd"])           # カーネルの仮想アドレス
        self.mem = UserMem(cores, kernel.v2p(self.pgd), fmt=fmt, tsz=tsz, region=region)
        self.threads: list[Task] = []
        self.refresh_threads()

    @property
    def comm(self) -> str:
        return self.threads[0].comm if self.threads else ""

    def refresh_threads(self) -> None:
        """スレッド一覧を読み直す。リーダーの task_struct が自分のものでなければ ProcessExited。"""
        leader = self.k.read_task(self.leader.addr)
        if not self._is_ours(leader):
            raise ProcessExited("pid %d exited" % self.tgid)
        self.threads = self.k.threads(self.leader.addr, leader)

    def _is_ours(self, t: Task) -> bool:
        return t.pid == self.tgid and t.tgid == self.tgid and t.mm == self.mm

    def tids(self) -> set[int]:
        return {t.pid for t in self.threads}

    def task(self, tid: int) -> Task | None:
        return next((t for t in self.threads if t.pid == tid), None)

    def alive(self) -> bool:
        return self._is_ours(self.k.read_task(self.leader.addr))

    def auxv(self) -> bytes:
        """exec のときにカーネルが保存した ELF の補助ベクタ（gdb が PIE/ASLR/共有ライブラリのために使う）。"""
        raw = self.k.read(self.mm + self.k.o["saved_auxv"], self.AUXV_MAX)
        for i in range(0, len(raw), 8):
            if struct.unpack_from("<I", raw, i)[0] == 0:             # AT_NULL
                return raw[:i + 8]
        raise KernelLayoutError("no AT_NULL in the first %d bytes of mm->saved_auxv: wrong offset for this kernel, or "
                                "memory is corrupt (not truncating it silently)" % self.AUXV_MAX)
