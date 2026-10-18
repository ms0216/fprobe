"""NuttX のカーネルオブジェクト: semaphore・mutex（sem_t の SEM_TYPE_MUTEX）・message_queue（struct mqueue_inode_s）。

仕様: docs/superpowers/specs/2026-10-19-u45-nuttx-design.md
NuttX はオブジェクトの一覧を持たない（セマフォはどこにでも置け、メッセージキューはヒープの inode）ので、
- 型が sem_t・mutex_t・rmutex_t・struct mqueue_inode_s の変数（大域・ファイルの static。TCB の変数の中は辿らない）
- タスクが待っているもの（waitobj）
を合わせる（同じ番地は 1 つ）。名前は変数（source "variable"。残りの式は aliases）→ シンボル → 番地（source "waiter"）。
待ち手は sem の waitlist、mq の cmn.waitfornotempty（受け取り）・waitfornotfull（送り）。どれも優先度順の列で、列の要素は
TCB そのもの（先頭の flink）を指す。ミューテックスの持ち主は val.mholder の TID（bit 31 は待ち手がいる印）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError
from ..dwarf.typesys import ArrayType, StructType, strip
from .model import KernelObject, Task, Waiter
from .objects import typed_variables

__all__ = ["MHOLDER_MASK", "MRESET", "NO_MHOLDER", "NuttXObjects", "SEM_PRIO_INHERIT", "SEM_TYPE_MUTEX"]

# sem_t の flags と val.mholder（include/semaphore.h・include/nuttx/semaphore.h）
SEM_PRIO_INHERIT, SEM_TYPE_MUTEX = 1, 4
MHOLDER_MASK, NO_MHOLDER, MRESET = 0x7FFF_FFFF, 0x7FFF_FFFE, 0x7FFF_FFFF
# 型の名前 → 分類（mutex_t・rmutex_t の先頭は sem_t なので、番地はその sem_t）
STRUCTS = {"sem_t": "sem", "sem_s": "sem", "mutex_t": "sem", "mutex_s": "sem", "rmutex_t": "sem", "rmutex_s": "sem",
           "mqueue_inode_s": "message_queue"}


class NuttXObjects:
    def __init__(self, rtos):
        self.rtos = rtos
        self.mem = rtos.mem

    def objects(self, tasks: list[Task]) -> list[KernelObject]:
        self._tasks = {t.handle: t for t in tasks}
        rtos = self.rtos
        structs = {name: family for name, family in STRUCTS.items()
                   if (family == "sem" and rtos.sem is not None) or (family == "message_queue" and rtos.mq is not None)}
        found: dict[int, str] = {}                                        # 番地 → 分類（見つけた順）
        names: dict[int, list[str]] = {}
        for f in sorted(typed_variables(rtos.info, self.mem, structs, exclude=self._tcb_variables()),
                        key=lambda f: f.pointer):
            found.setdefault(f.address, f.family)
            names.setdefault(f.address, []).append(f.expression)
        for task in tasks:
            w = task.wait
            if w is not None and w.object and w.kind in ("semaphore", "mutex", "message_queue"):
                found.setdefault(w.object, "message_queue" if w.kind == "message_queue" else "sem")
        return [self._named(handle, family, names.get(handle, [])) for handle, family in found.items()]

    def _tcb_variables(self) -> frozenset[str]:
        """TCB（struct tcb_s か、その配列）の変数: 中の join_sem 等はタスクの部品なので辿らない。"""
        out = set()
        for v in self.rtos.info.global_variables():
            t = strip(v.type)
            while isinstance(t, ArrayType):
                t = strip(t.element)
            if isinstance(t, StructType) and t.name == "tcb_s":
                out.add(v.name)
        return frozenset(out)

    def _named(self, handle: int, family: str, names: list[str]) -> KernelObject:
        try:
            obj = self._message_queue(handle) if family == "message_queue" else self._sem(handle)
        except AccessError as e:
            obj = KernelObject(handle, family, "", "", notes=[f"読めない: {e}"])
        if names:
            obj.name, obj.source, obj.aliases = names[0], "variable", sorted(set(names[1:]) - {names[0]})
        else:
            obj.name, obj.source = self.rtos.object_label(handle), "waiter"
        return obj

    # --- 読み ------------------------------------------------------------------------------------

    def _u(self, layout, handle: int, path: str, signed: bool = False) -> int:
        return self.mem.uint(handle + layout.offset(path), layout.member_size(path), signed)

    def _sem(self, h: int) -> KernelObject:
        s = self.rtos.sem
        flags = self._u(s, h, "flags")
        waiters = self._waiters(h + s.offset("waitlist.head"), "receive")
        if not flags & SEM_TYPE_MUTEX:
            count = self._u(s, h, "val.semcount", signed=True)
            return KernelObject(h, "semaphore", "", "", {"count": max(count, 0)}, waiters, wait_lists=(h,))
        holder = self.rtos.mutex_holder(h)
        fields: dict = {"available": holder is None, "holder": None,
                        "priority_inherit": bool(flags & SEM_PRIO_INHERIT)}
        if holder is not None:
            task = self._tasks.get(holder)
            priority = task.priority if task is not None else self.rtos._field(holder, "sched_priority")
            base = self.rtos.handle_priority(holder)
            fields["holder"] = {"task": task.name if task is not None else self.rtos.task_name(holder),
                                "handle": holder, "pid": self.rtos._field(holder, "pid", signed=True),
                                "priority": priority, "base_priority": base, "inherited": priority != base}
        return KernelObject(h, "mutex", "", "", fields, waiters, wait_lists=(h,))

    def _message_queue(self, h: int) -> KernelObject:
        q = self.rtos.mq
        fields = {"count": self._u(q, h, "nmsgs", signed=True), "max_msgs": self._u(q, h, "maxmsgs", signed=True),
                  "msg_size": self._u(q, h, "maxmsgsize")}
        waiters = self._waiters(h + q.offset("cmn.waitfornotempty.head"), "receive") + \
            self._waiters(h + q.offset("cmn.waitfornotfull.head"), "send")
        return KernelObject(h, "message_queue", "", "", fields, waiters, wait_lists=(h,))

    def _waiters(self, head_address: int, reason: str) -> list[Waiter]:
        link = self.rtos.tcb.offset("flink")
        out = []
        for tcb in self.rtos.chain(self.mem.u32(head_address), link, "待ちの列"):
            task = self._tasks.get(tcb)
            name = task.name if task is not None else self.rtos.task_name(tcb)
            priority = task.priority if task is not None else self.rtos._field(tcb, "sched_priority")
            out.append(Waiter(name, tcb, priority, reason))
        return out
