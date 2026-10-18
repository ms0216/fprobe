"""止めた瞬間の取り合い（U08）: ミューテックスの優先度の逆転と、待ちの閉路（デッドロック）。

入力は RTOS に依らない形（Task・KernelObject）。ミューテックスは fields["holder"]（持ち主の番地・優先度・元の優先度）と
waiters（待っているタスク）を持つオブジェクト。優先度の向きは order（1 = 大きい方が高い、-1 = 小さい方が高い）。

    contention(rtos.tasks(), rtos.objects(), rtos.priority_order)
    # {"inversions": [{mutex, holder, waiter, inherited, interferers}], "deadlocks": [[{task, waits_for, held_by}]]}
"""

from __future__ import annotations

from .model import KernelObject, Task, TaskState

__all__ = ["contention"]


def contention(tasks: list[Task], objects: list[KernelObject], order: int = 1) -> dict:
    """優先度の逆転: 持ち主の元の優先度より高いタスクが待っている。継承で持ち主の優先度が上がっているか（inherited）と、
    その間に動ける中間の優先度のタスク（実行中・実行可能。継承が無いと逆転を際限なく伸ばす: interferers）。
    デッドロック: 「待つタスク → 待つミューテックスの持ち主」を辿って閉じる輪。"""
    by_handle = {t.handle: t for t in tasks}

    def urgency(priority: int | None) -> int | None:
        return None if priority is None else priority * order
    inversions = []
    waits_for: dict[int, tuple[KernelObject, int]] = {}            # 待つタスク → (ミューテックス, 持ち主)
    for obj in objects:
        holder = obj.holder
        if holder is None:
            continue
        h = obj.fields["holder"]
        effective = h.get("priority")
        base = h.get("base_priority", effective)
        base = effective if base is None else base
        for w in obj.waiters:
            waits_for[w.handle] = (obj, holder)
            if w.priority is None or base is None or urgency(w.priority) <= urgency(base):
                continue
            current = effective if effective is not None else base
            interferers = [{"task": t.name, "handle": t.handle, "priority": t.priority, "state": t.state.value}
                           for t in tasks
                           if t.state in (TaskState.RUNNING, TaskState.READY) and t.handle not in (holder, w.handle)
                           and urgency(current) < urgency(t.priority) < urgency(w.priority)]
            inversions.append({
                "mutex": obj.name, "handle": obj.handle,
                "holder": {"task": h.get("task"), "handle": holder, "priority": effective, "base_priority": base},
                "waiter": {"task": w.task, "handle": w.handle, "priority": w.priority},
                "inherited": urgency(current) > urgency(base), "interferers": interferers})
    return {"inversions": inversions, "deadlocks": _cycles(waits_for, by_handle)}


def _cycles(waits_for: dict[int, tuple[KernelObject, int]], by_handle: dict[int, Task]) -> list[list[dict]]:
    """待ちのグラフ（各タスクは高々 1 つを待つ）の閉路。同じ輪は 1 度だけ。"""
    def name(handle: int) -> str:
        t = by_handle.get(handle)
        return t.name if t is not None else f"0x{handle:08X}"
    found: list[list[dict]] = []
    seen: set[frozenset[int]] = set()
    for start in waits_for:
        path: list[int] = []
        node = start
        while node in waits_for and node not in path:
            path.append(node)
            node = waits_for[node][1]
        if node not in path:
            continue
        cycle = path[path.index(node):]
        key = frozenset(cycle)
        if key in seen:
            continue
        seen.add(key)
        found.append([{"task": name(t), "handle": t, "waits_for": waits_for[t][0].name,
                       "held_by": name(waits_for[t][1])} for t in cycle])
    return found
