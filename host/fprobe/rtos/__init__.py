"""RTOS の認識（F6）: 止まったターゲットのタスクの一覧・タスクごとの呼び出し履歴と、止めずに見るタスクの時間軸。

仕様: docs/superpowers/specs/2026-10-17-f6-rtos-design.md

    info = DebugInfo("app.elf")
    rtos = open_rtos(info, access, symbols=Symbols("app.elf"))     # FreeRTOS / Zephyr / RTX5 / ThreadX / µC/OS-III / NuttX（無ければ None）
    rtos.tasks()                                                   # [Task]（to_dict() で JSON に）
    rtos.backtrace("worker", variables=True)

    timeline = timeline_from_samples(samples, names)               # 方法 A: DAP サンプラの pxCurrentTCB
    timeline = timeline_from_events(task_events(itm_packets(swo)), names)   # 方法 B: トレースフック → ITM
    write_timeline(recorder, timeline)                             # 共通の時間軸の記録（stream "rtos"）
"""

from ..dwarf.info import DebugInfo
from .freertos import FreeRTOS
from .model import StackInfo, Task, TaskState, WaitInfo
from .nuttx import NuttX
from .rtx5 import RTX5
from .threadx import ThreadX
from .timeline import (TaskEvent, Timeline, itm_packets, samples_from_recording, swo_bytes_from_recording,
                       task_events, timeline_from_events, timeline_from_samples, write_task_events, write_timeline)
from .ucos3 import UCOS3
from .zephyr import Zephyr

__all__ = ["FreeRTOS", "NuttX", "RTX5", "StackInfo", "Task", "TaskEvent", "TaskState", "ThreadX", "Timeline", "UCOS3", "WaitInfo", "Zephyr", "detect",
           "itm_packets", "open_rtos", "samples_from_recording", "swo_bytes_from_recording", "task_events",
           "timeline_from_events", "timeline_from_samples", "write_task_events", "write_timeline"]

_KINDS = {"freertos": FreeRTOS, "zephyr": Zephyr, "rtx5": RTX5, "threadx": ThreadX, "ucos3": UCOS3, "nuttx": NuttX}


def detect(info: DebugInfo) -> str | None:
    """ELF の DWARF から RTOS の種類（"freertos" / "zephyr" / "rtx5" / "threadx" / "ucos3" / "nuttx"）。分からなければ None。"""
    for kind, cls in _KINDS.items():
        if cls.detect(info):
            return kind
    return None


def open_rtos(info: DebugInfo, access, symbols=None, **options):
    """RTOS を見つけて、その読み手を作る（無ければ None）。"""
    kind = detect(info)
    return None if kind is None else _KINDS[kind](info, access, symbols, **options)
