"""RTOS に依らないタスク・カーネルオブジェクト・ヒープの形（結果はすべて JSON にできる: `to_dict()`）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

__all__ = ["HeapInfo", "KernelObject", "StackInfo", "Task", "TaskState", "WaitInfo", "Waiter"]


class TaskState(str, Enum):
    RUNNING = "running"           # いま CPU を使っている
    READY = "ready"               # 実行可能（CPU を待っている）
    BLOCKED = "blocked"           # 待ち（時間・キュー・セマフォ・通知等）
    SUSPENDED = "suspended"       # 停止（明示的に止められた）
    DELETED = "deleted"           # 削除待ち（片付ける前）


@dataclass
class StackInfo:
    """下へ伸びるスタック。base は最も低い番地、end は最も高い番地の次（分からなければ None）。"""
    base: int
    end: int | None = None
    pointer: int | None = None    # いまの SP（止まっているタスクは保存した SP）
    untouched: int | None = None  # base から上へ、塗りつぶしの値が残っていたバイト数（塗られていなければ None）
    fill: str = "unknown"         # intact（底が塗りのまま）/ none（底が塗られていない）/ unknown（読めない）
    overflow: bool = False        # SP が base より下（RTOS 自身の印: RTX5 のマジックワードが壊れた、も）
    fill_above: bool | None = None  # 底が塗りでないとき、底の少し上に塗りの並びが残っているか（事実。あふれと推定しない）

    @property
    def size(self) -> int | None:
        return None if self.end is None else self.end - self.base

    @property
    def used(self) -> int | None:
        """いまの使用量。"""
        return None if self.end is None or self.pointer is None else self.end - self.pointer

    @property
    def peak_used(self) -> int | None:
        """最大の使用量（塗りが残っていた位置から）。"""
        return None if self.size is None or self.untouched is None else self.size - self.untouched

    def to_dict(self) -> dict:
        return {**asdict(self), "size": self.size, "used": self.used, "peak_used": self.peak_used}


@dataclass
class WaitInfo:
    """待っているもの。reason: delay / receive / send / event / notification / suspended。"""
    reason: str
    object: int | None = None     # 待っているもの（キュー・セマフォ等）の番地
    name: str | None = None       # その名前（登録簿・シンボル）
    kind: str | None = None       # queue / mutex / counting_semaphore / binary_semaphore / recursive_mutex
    holder: str | None = None     # ミューテックスの持ち主（タスク名）
    list: int | None = None       # 待ちのリストの番地
    wake_tick: int | None = None  # 時間切れ（起きる）ティック。時間切れ無しは None
    remaining_ticks: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Task:
    handle: int                   # TCB（スレッドの構造体）の番地
    name: str
    priority: int
    state: TaskState
    stack: StackInfo
    wait: WaitInfo | None = None
    base_priority: int | None = None
    number: int | None = None     # RTOS の通し番号（FreeRTOS の uxTCBNumber）
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"handle": self.handle, "name": self.name, "priority": self.priority, "state": self.state.value,
               "stack": self.stack.to_dict()}
        if self.base_priority is not None:
            out["base_priority"] = self.base_priority
        if self.number is not None:
            out["number"] = self.number
        if self.wait is not None:
            out["wait"] = self.wait.to_dict()
        if self.notes:
            out["notes"] = list(self.notes)
        return out


# --- カーネルオブジェクト（U08） ---------------------------------------------------------------------

@dataclass
class Waiter:
    """オブジェクトを待っているタスク。reason: receive / send（キュー・セマフォ・ミューテックス・バッファ）/ bits
    （イベントグループ）/ pend（Zephyr の待ちキュー）。detail は種類ごと（イベントグループの待つビット等）。"""
    task: str
    handle: int
    priority: int | None
    reason: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"task": self.task, "handle": self.handle, "priority": self.priority, "reason": self.reason}
        out.update(self.detail)
        return out


@dataclass
class KernelObject:
    """カーネルのオブジェクト（キュー・セマフォ・ミューテックス・タイマ・イベントグループ・バッファ等）。

    name は 登録簿 → オブジェクト自身の名前 → 変数 → 番地（"0x..."）の順。source はそれを見つけた所
    （registry / kernel / variable / waiter）。fields は種類ごとの中身（JSON にできる値）。
    wait_lists はこのオブジェクトの待ちのリスト（キュー）の番地（タスクの待ちとの照合用。to_dict には出さない）。"""
    handle: int
    kind: str
    name: str
    source: str
    fields: dict = field(default_factory=dict)
    waiters: list[Waiter] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    wait_lists: tuple[int, ...] = ()

    @property
    def holder(self) -> int | None:
        """ミューテックスの持ち主（TCB・スレッドの番地）。持ち主がいない・ミューテックスでなければ None。"""
        holder = self.fields.get("holder")
        return holder["handle"] if isinstance(holder, dict) else None

    def to_dict(self) -> dict:
        out = {"handle": self.handle, "kind": self.kind, "name": self.name, "source": self.source,
               "fields": dict(self.fields), "waiters": [w.to_dict() for w in self.waiters]}
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.notes:
            out["notes"] = list(self.notes)
        return out


@dataclass
class HeapInfo:
    """RTOS のヒープの要約。scheme: heap_1 / heap_2 / heap_4 / heap_5（FreeRTOS）/ sys_heap（Zephyr）。
    分からない値は None。free_blocks・largest_free_block は空きのリストを辿った結果（断片化の目安）。"""
    scheme: str
    total: int | None = None
    free: int | None = None
    min_free: int | None = None           # これまでの最小の残量
    allocations: int | None = None
    frees: int | None = None
    free_blocks: int | None = None
    largest_free_block: int | None = None
    address: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def used(self) -> int | None:
        return None if self.total is None or self.free is None else self.total - self.free

    @property
    def peak_used(self) -> int | None:
        return None if self.total is None or self.min_free is None else self.total - self.min_free

    def to_dict(self) -> dict:
        return {**asdict(self), "used": self.used, "peak_used": self.peak_used}
