"""タスクごとの呼び出し履歴: F2 の Inspector を、タスクの文脈の読み手と「ここで止める」戻り先で作る。"""

from __future__ import annotations

from collections.abc import Iterable

from ..dwarf.access import TargetAccess
from ..dwarf.info import DebugInfo
from ..dwarf.inspector import Inspector
from ..dwarf.unwind import FrameState, Unwinder

__all__ = ["TaskUnwinder", "task_inspector"]


class TaskUnwinder:
    """Unwinder を包み、戻り先が stop_pcs（タスクの関数が戻ってしまったときの罠 = 呼び出し元ではない）のフレームで止める。"""

    def __init__(self, inner: Unwinder, stop_pcs: Iterable[int]):
        self.inner = inner
        self.stop_pcs = frozenset(pc & ~1 for pc in stop_pcs)

    def unwind(self, access: TargetAccess) -> list[FrameState]:
        """止めたフレームの stop は {"reason": "stop_pc", "pc": 戻り先}（戻り先がタスクの関数の戻り先の罠）。"""
        frames = self.inner.unwind(access)
        for i, frame in enumerate(frames):
            if i > 0 and frame.pc in self.stop_pcs:
                frames[i - 1].stop = {"reason": "stop_pc", "pc": frame.pc}
                return frames[:i]
        return frames


def task_inspector(info: DebugInfo, access: TargetAccess, symbols=None, stop_pcs: Iterable[int] = ()) -> Inspector:
    inspector = Inspector(info, access, symbols)
    inspector.unwinder = TaskUnwinder(inspector.unwinder, stop_pcs)
    return inspector
