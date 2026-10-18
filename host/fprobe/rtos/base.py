"""RTOS の読み手の共通の部分: タスクの引き方・タスクごとの読み手と呼び出し履歴・カーネルの変数の番地。

RTOS ごとの読み手（FreeRTOS・Zephyr・RTX5・ThreadX・µC/OS-III・NuttX）は tasks() と saved_context() を作り、残りはここを使う。
"""

from __future__ import annotations

from ..dwarf.access import TargetAccess
from ..dwarf.info import DebugInfo
from ..dwarf.inspector import Inspector
from ..dwarf.location import MemoryPlace
from .cortexm import ContextAccess, SavedContext
from .frames import task_inspector
from .model import HeapInfo, KernelObject, Task
from .structs import Memory

__all__ = ["KernelAwareness"]

MAX_CHAIN = 4096                  # 辿る列の長さの上限（壊れたリストでも止まる）


class KernelAwareness:
    name = "?"
    priority_order = 1            # 優先度の向き: 1 = 大きい方が高い（FreeRTOS）、-1 = 小さい方が高い（Zephyr）

    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None):
        self.info = info
        self.access = access
        self.symbols = symbols
        self.mem = Memory(access.read_memory)
        self.warnings: list[str] = []
        self._names: dict[int, str] = {}

    # --- RTOS ごとに作る ------------------------------------------------------------------------

    def tasks(self) -> list[Task]:
        raise NotImplementedError

    def saved_context(self, key: Task | str | int) -> SavedContext | None:
        """止まっているタスクの保存された文脈（実行中のタスクは None = 実レジスタ）。"""
        raise NotImplementedError

    def objects(self, kinds: list[str] | None = None, tasks: list[Task] | None = None) -> list[KernelObject]:
        """カーネルのオブジェクトの一覧（kinds で絞る。tasks は待ち手の名前に使う。省けば tasks() を読む）。"""
        raise NotImplementedError

    def heap(self) -> HeapInfo | None:
        """RTOS のヒープの要約（無ければ None）。"""
        return None

    def saved_sp_address(self, handle: int) -> int:
        """タスクの保存された SP の語の番地（切り替えのたびに書かれる。スタックの推移をサンプラで読む）。"""
        raise NotImplementedError

    def mutex_words(self, obj: KernelObject) -> tuple[int, int, int] | None:
        """ミューテックスの (持ち主の語の番地, 待ちの先頭の語の番地, 先頭の値 → タスクの番地の差) を返す。
        サンプラで読み、持ち主 = 語の値、待ちの先頭のタスク = 値 − 差（値が空のときの印は mutex_empty_head）。"""
        return None

    def mutex_empty_head(self, obj: KernelObject) -> int:
        """待ちが空のときの、待ちの先頭の語の値（リストの終わりの番地）。"""
        raise NotImplementedError

    def handle_priority(self, handle: int) -> int:
        """TCB の番地 → 元の優先度（継承の前。一覧を取った後に作られたタスクを、動作中の読みで）。"""
        raise NotImplementedError

    def heap_free_word(self) -> tuple[int, int | None] | None:
        """ヒープの残量の語の番地と総量（サンプラで推移を読む）。無ければ None。"""
        return None

    def idle_handles(self) -> set[int]:
        """アイドルのタスクの TCB（負荷に数えない）。分からなければ空。"""
        return set()

    def stop_pcs(self) -> list[int]:
        """巻き戻しを止める戻り先（タスクの関数が戻ってしまったときの罠）。"""
        return []

    def current_address(self) -> int:
        """実行中のタスクの TCB（スレッドの構造体）を指す変数の番地。止めずに見る時間軸の方法 A でサンプラが読む 1 語。"""
        raise NotImplementedError

    def handle_name(self, handle: int) -> str:
        """TCB の番地 → タスクの名前（一覧を取った後に作られたタスクを、動作中の読みで名付ける）。"""
        raise NotImplementedError

    def current(self) -> int:
        """いま CPU を使っているタスクの TCB の番地。"""
        return self.mem.u32(self.current_address())

    def tick_count(self) -> int | None:
        """RTOS のティック（分からなければ None）。"""
        return None

    def scheduler_state(self) -> str:
        """running / not_started / suspended（分からなければ unknown）。"""
        return "unknown"

    # --- 共通 ----------------------------------------------------------------------------------

    @staticmethod
    def select(objects: list[KernelObject], kinds: list[str] | None) -> list[KernelObject]:
        """種類で絞って並べる（kind か、その後ろの部分: "semaphore" は binary_semaphore・counting_semaphore も）。"""
        if kinds:
            objects = [o for o in objects if any(o.kind == k or o.kind.endswith("_" + k) for k in kinds)]
        return sorted(objects, key=lambda o: (o.kind, o.name))

    def address(self, name: str) -> int:
        """カーネルの変数（大域・ファイルの static）の番地。"""
        var = self.info.global_variable(name)
        place = var.place()
        if not isinstance(place, MemoryPlace):
            raise KeyError(f"{self.name} の変数 {name} の場所が分からない（最適化で消えた?）")
        return place.address

    def optional_address(self, name: str) -> int | None:
        try:
            return self.address(name)
        except KeyError:
            return None

    def chain(self, head: int, link: int, what: str, count: int | None = None) -> list[int]:
        """head から link（次を指すメンバーのバイト位置）を NULL まで辿った列。輪になる・多すぎるときは止めて warnings
        に書く。count は期待する数（違えば警告）。"""
        out: list[int] = []
        node, seen = head, set()
        while node:
            if node in seen or len(seen) >= MAX_CHAIN:
                self.warnings.append(f"{what} の列が壊れている（{len(seen)} 個で止めた）")
                return out
            seen.add(node)
            out.append(node)
            node = self.mem.u32(node + link)
        if count is not None and len(out) != count:
            self.warnings.append(f"{what} の数が合わない（数 {count}、列 {len(out)}）")
        return out

    def function_address(self, name: str) -> int | None:
        if self.symbols is not None:
            try:
                return self.symbols.symbol(name).address
            except KeyError:
                pass
        for f in self.info.functions():
            if f.name == name:
                return f.low
        return None

    def task_names(self) -> dict[int, str]:
        """タスクの番地 → 名前（時間軸の名前の表）。"""
        if not self._names:
            self.tasks()
        return dict(self._names)

    def task(self, key: Task | str | int) -> Task:
        if isinstance(key, Task):
            return key
        for t in self.tasks():
            if t.name == key or t.handle == key:
                return t
        raise KeyError(f"タスク {key!r} は無い")

    def task_access(self, key: Task | str | int) -> TargetAccess:
        """そのタスクのレジスタとメモリの読み手（実行中のタスクは渡された読み手そのもの）。"""
        context = self.saved_context(key)
        return self.access if context is None else ContextAccess(self.access, context.registers)

    def inspector(self, key: Task | str | int) -> Inspector:
        """そのタスクの文脈の Inspector（呼び出し履歴・変数・C の式）。"""
        return task_inspector(self.info, self.task_access(key), self.symbols, self.stop_pcs())

    def backtrace(self, key: Task | str | int, variables: bool = False, max_elements: int = 64) -> list[dict]:
        return self.inspector(key).backtrace(variables=variables, max_elements=max_elements)
