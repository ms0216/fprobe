"""ログ・トレースの取り込みの受け口（_Feed）・定数・小さな関数（FormatService = fprobe.probe_formats の下層）。

_Feed は 1 つの取り込み（ログか、トレースか）の状態の入れ物。FormatService は 2 つ（ログ・トレース）まで持つ。
"""

from __future__ import annotations

import collections
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .probe_window import WindowCursor
from .recording import ClockMap
from .testpoint import TimedSwo
from .tracefmt.model import TargetClock, Trace
from .tracefmt.stream import CHANNELS
from .tracefmt.systemview import COMMAND_START as SV_START, COMMAND_STOP as SV_STOP
from .tracefmt.tracealyzer import COMMAND_START as TZ_START, COMMAND_STOP as TZ_STOP, CONTROL_NAME as TZ_CONTROL


SOURCES = ("rtt", "swo", "uart")
LOG_LIMIT = 10_000                       # 貯めておく行の数（それより古いものは捨てる。全部は記録の stream "log"）
EVENTS_LIMIT = 200_000                   # トレースの出来事を持っておく数
PULL_S = 0.1                             # SWO・UART の窓・Event Recorder の RAM を読む周期
_START_COMMANDS = {"systemview": (CHANNELS["systemview"], bytes([SV_START]), bytes([SV_STOP])),
                   "tracealyzer": (TZ_CONTROL, TZ_START, TZ_STOP)}
# 優先度の向き（1 = 大きい方が高い）。形式そのものが 1 つの RTOS のもの（ctf = Zephyr の subsys/tracing/ctf、
# tracex = ThreadX）だけ、その RTOS のソースで決まる向き（rtos.zephyr・rtos.threadx の priority_order と同じ）。
# ほかの形式（SystemView の系の説明 O=... はアプリが書く自由な文字列）では OS 名から推さず、向きは不明（None）
_PRIORITY_ORDER = {"ctf": -1, "tracex": -1}


def _order_name(order: int | None) -> str | None:
    """優先度の向きの名前（rtos.objects の priority_order と同じ書き方）。不明なら None。"""
    if order is None:
        return None
    return "higher_is_more_urgent" if order > 0 else "lower_is_more_urgent"


def _idle_tasks(names: dict[int, str], idle_tasks: list[str] | None) -> tuple[set[int], dict | None]:
    """使う側が明示したアイドルのタスクの名前 → タスクの ID の集合と、結果に出す対応（名前 → ID、合わなかった名前）。
    名前の完全一致だけ（同じ名前のタスクが複数なら全部）。idle_tasks が無ければアイドルは不明（空の集合と None）。"""
    if idle_tasks is None:
        return set(), None
    matched = {n: sorted(i for i, name in names.items() if name == n) for n in idle_tasks}
    found = {n: ids for n, ids in matched.items() if ids}
    return ({i for ids in found.values() for i in ids},
            {"matched": found, "unmatched": [n for n, ids in matched.items() if not ids]})


@dataclass
class _Feed:
    """1 つの受け取り（ログかトレース）。"""
    kind: str                                        # log / trace
    format: str
    source: str                                      # rtt / swo / uart / memory
    settings: dict
    decoder: object                                  # LogStream / TraceStream / None（memory）
    started: float
    timebase_hz: float | None = None
    rtt_channel: int | None = None
    listener: object = None
    started_rtt: bool = False
    cursor: WindowCursor | None = None
    itm: TimedSwo | None = None
    port: int | None = None
    restore: dict[int, int] = field(default_factory=dict)
    pump: threading.Thread | None = None
    halt: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    stopped: float | None = None
    host_clock: ClockMap | None = None               # 記録していないときのホストの時計 → タイムベース
    bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    # 解読した結果の渡し先 deliver(結果の列, 受けた時刻, 不確かさ, 時刻の出どころ)（受け取りを作った側が決める）と、
    # source が memory のときの RAM の読み poll(feed)（背景のスレッドと、止める前の最後の読みが呼ぶ）
    deliver: Callable[[list, int | None, float | None, str], None] | None = None
    poll: Callable[[_Feed], None] | None = None
    # ログ
    records: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_LIMIT))
    seq: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    # トレース
    trace: Trace | None = None
    clock: TargetClock | None = None
    written: int = 0                                 # 記録に書いた出来事の数
    er_init_count: int | None = None                 # Event Recorder の init_count（前の読み。変われば時刻の区切り）
    since: int | None = None                         # eventrecorder: 次に読む通し番号
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.stopped is None


def with_error_and_window(feed: _Feed, out: dict, window_info: Callable[[object, float], dict]) -> dict:
    """ログ・トレースの状態の末尾に共通の欄（背景の読みの誤りと、SWO・UART の窓の様子）。window_info は
    窓の様子を返す道具 (窓, タイムベースの周波数) → dict。"""
    if feed.error is not None:
        out["error"] = feed.error
    if feed.cursor is not None:
        out["window"] = window_info(feed.cursor.window, feed.timebase_hz or 1.0)
    return out


def timebase_hz(host) -> float | None:
    """機器のタイムベースの周波数（host.session を引いて読む）。機器が無ければ（オフライン）None。"""
    try:
        return host.session.device.capabilities().timebase_hz
    except Exception:
        return None
