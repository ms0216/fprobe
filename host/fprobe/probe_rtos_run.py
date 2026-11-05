"""RTOS の時間軸の実行状態と、その小さな道具（probe_rtos の RtosService・probe.formats・記録の解析が共有する）。

- _TimelineRun: 動いている（または取り込んだ）時間軸の取り方。_TimelineResult: 組み立てた時間軸と、その元の出来事・サンプル。
- _MemoryOnly: 動いているターゲットの読み手（メモリだけ）。
- _us / DURATIONS: タイムベースの刻み → µs と、区間の長さの列名。
"""

from dataclasses import dataclass, field

from .debug import PyocdAccess
from .dwarf.access import AccessError
from .probe_window import EventWindow
from .profile.exceptions import Activation
from .rtos.analysis import MemorySlot, MutexWatch
from .rtos.kernel_trace import KernelTrace, object_table
from .rtos.timeline import Timeline, TaskEvent

__all__ = ["DURATIONS", "_MemoryOnly", "_TimelineResult", "_TimelineRun", "_mutex_tasks", "_us"]

DURATIONS = ("total", "max_run", "min_run", "mean_run", "max_cut_run")
TRACE_FLAG = "fprobe_trace_enabled"      # fprobe_trace.h: デバッガが 1 を書くとトレースフックが ITM に書く
ITM_WORD_BYTES = 5                       # 4 バイトのスティミュラスのパケット（ヘッダ + 4）
SWO_BITS_PER_BYTE = {"uart": 10}         # 受信の方式 → 線の 1 バイトのビット数（UART 方式 = NRZ: スタート + 8 + ストップ）。
                                         # Manchester の枠のビット数は数えていない（分解能・帯域の使用率を出さない）
RUNS_LIMIT = 200                         # rtos_timeline_get が 1 回に返す区間の数の既定


def _us(ticks: float | None, hz: float) -> float | None:
    """タイムベースの刻み → µs（None = 分からない・無い、はそのまま）。"""
    return None if ticks is None else ticks * 1e6 / hz


class _MemoryOnly:
    """動いているターゲットの読み手: メモリは読み、コアのレジスタは読まない（AccessError）。"""

    def __init__(self, access: PyocdAccess):
        self.access = access

    def read_memory(self, address: int, length: int) -> bytes:
        return self.access.read_memory(address, length)

    def read_register(self, name: str) -> int:
        raise AccessError(f"レジスタ {name}: ターゲットが動いている")


@dataclass
class _TimelineRun:
    """動いている時間軸の取り方。"""
    method: str                          # sampled / itm
    window: EventWindow
    names: dict[int, str]                # TCB の番地 → 名前（始めたときの一覧 + 後で見つけた番地）
    timebase_hz: float
    address: int | None = None           # sampled: 実行中のタスクの変数の番地
    rate_hz: float | None = None         # sampled: サンプルの周期
    flag: int | None = None              # itm: fprobe_trace_enabled の番地
    resolution: float | None = None      # itm: 時刻の分解能（タイムベース。None = 分からない）
    warnings: list[str] = field(default_factory=list)
    order: int | None = None             # 優先度の向き（1 = 大きい方が高い、-1 = 小さい方が高い、None = 不明）
    priorities: dict[int, int] = field(default_factory=dict)     # TCB → 元の優先度（ジョブ・逆転の判定）
    idle: set[int] = field(default_factory=set)                  # アイドルのタスクの TCB
    slots: dict[int, MemorySlot] = field(default_factory=dict)   # memory: 語の番地 → 意味
    mutexes: list[MutexWatch] = field(default_factory=list)      # mutexes: 持ち主・待ちの先頭の語
    sampler: bool = False                # DAP サンプラを使っている（sampled は常に）
    memory_rate_hz: float | None = None  # itm でサンプラを使うときの周期
    exceptions: bool = False             # itm: 例外トレースも流している
    baud: float | None = None            # itm: SWO の速度（帯域の見積もり）
    bits_per_byte: int | None = None     # itm: SWO の線の 1 バイトのビット数（受信の方式から。分からなければ None）
    core_clock_hz: float | None = None   # itm: コアのクロック（フックの負荷の見積もり）
    objects: dict[int, dict] = field(default_factory=dict)       # itm: オブジェクトの番地 → {name, kind, length}
    restore: dict[int, int] = field(default_factory=dict)        # 止めるときに戻す（番地 → 元の値）
    vector_table: tuple[int, int] | None = None                  # 使う側が渡したベクタ表（番地, 大きさ）。割り込みの名前

    @property
    def words(self) -> set[int]:
        """いまのタスクの語のほかに、サンプラで読んで使う語。"""
        return set(self.slots) | {a for w in self.mutexes for a in (w.holder_word, w.head_word)}


@dataclass
class _TimelineResult:
    timeline: Timeline
    events: list[TaskEvent] | None       # itm の生の出来事
    run: _TimelineRun
    samples: list[tuple[int, int, int]] = field(default_factory=list)   # memory・mutexes の語の (時刻, 番地, 値)
    activations: list[Activation] | None = None                         # itm + exceptions: 例外の回
    exception_issues: dict | None = None                                # 並びの合わない数・あふれの数
    kernel: KernelTrace | None = None                                   # itm: カーネルの出来事（U10）

    @property
    def has_kernel(self) -> bool:
        """カーネルの出来事（ポート 31 以外の記録）がある。"""
        return self.kernel is not None and any(op.kind != "lost" for op in self.kernel.ops)

    def objects(self) -> dict[int, dict]:
        """オブジェクトの表（始めの一覧 + object_create の出来事）。"""
        return object_table(self.kernel, self.run.objects) if self.kernel is not None else dict(self.run.objects)


def _mutex_tasks(watches: list[MutexWatch], samples: list[tuple[int, int, int]]) -> set[int]:
    """ミューテックスの語のサンプルに現れたタスクの TCB: 持ち主の語の値と、待ちの先頭の語の値から待ちの項目の差を引いたもの
    （空の印と 0 は除く）。サンプル 1 回の走査で（ミューテックスの数 × サンプル数の二重の走査にしない）。"""
    if not watches:
        return set()
    heads: dict[int, list[MutexWatch]] = {}
    holders: set[int] = set()
    for w in watches:
        heads.setdefault(w.head_word, []).append(w)
        holders.add(w.holder_word)
    tasks: set[int] = set()
    for _, address, value in samples:
        for w in heads.get(address, ()):
            if value not in (0, w.empty):
                tasks.add(value - w.delta)
        if value and address in holders:
            tasks.add(value)
    return tasks
