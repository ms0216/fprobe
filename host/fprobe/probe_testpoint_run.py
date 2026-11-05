"""テストポイントの受け取り 1 回分の状態（_Run）。方式（gpio / itm / rtt）の部品と TestpointService が共有する。"""

import threading
from dataclasses import dataclass, field

from .la import DeepAnalyzer, LaStream
from .probe_window import EventWindow
from .testpoint import ItmDecoder, LiveAnalysis, ParallelDecoder, RttDecoder, SerialDecoder, TimedSwo

__all__ = ["_Run"]


@dataclass
class _Run:
    """動いている（止めた）テストポイントの受け取り。"""
    method: str
    capture: str | None                  # gpio: deep / stream
    live: LiveAnalysis
    map_path: str
    decoder: SerialDecoder | ParallelDecoder | ItmDecoder | RttDecoder
    settings: dict
    started: float                       # ホストの単調時計
    duration_s: float | None = None
    warnings: list[str] = field(default_factory=list)
    stopped: float | None = None
    # gpio deep / stream
    deep: DeepAnalyzer | None = None
    stream: LaStream | None = None
    pump: threading.Thread | None = None  # 背景のスレッド（stream: 受け取り、itm: 窓の読み）
    halt: threading.Event = field(default_factory=threading.Event)     # itm: 背景の読みを止める
    error: str | None = None             # 背景のスレッドの誤り
    # itm
    window: EventWindow | None = None
    swo: TimedSwo | None = None
    last_t: int | None = None            # 解析に渡した最後の SWO のバイトの時刻
    at_last: int = 0                     # その時刻のバイトのうち渡した数
    ring_seen: int = 0                   # 記録していないとき: 前回までに読んだイベント記録の総数
    restore: dict[int, int] = field(default_factory=dict)
    pulling: threading.Lock = field(default_factory=threading.Lock)
    # rtt
    rtt_channel: int | None = None
    rtt_listener: object = None
    started_rtt: bool = False
    synced: bool = False                 # rtt: 最初の正しい記録を見た（受け始めの途中の記録は数えない）
    skipped_before_sync: int = 0         # rtt: 最初の正しい記録より前に捨てた解読の項目の数
    host_clock: tuple[float, int, float] | None = None     # (ホストの時刻, タイムベース, 不確かさ) 記録していないとき

    @property
    def running(self) -> bool:
        return self.stopped is None
