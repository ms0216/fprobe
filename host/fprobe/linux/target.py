"""Linux のターゲット 1 台ぶんの状態の入れ物（Target）と、その既定を作る小道具。

LinuxService（fprobe.probe_linux）と、それに混ぜ込む部品（probe_linux_tools・probe_linux_ring）が同じ Target を読み書きする。
ここは状態の形だけを持つ（操作は持たない）。出来事の置き場の大きさ KEEP もここ。
"""
from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field

from .agent import Agent
from .clock import TargetClock
from .gdb_relay import GdbRelay
from .ring import RingReader
from .ring_cpu import new_ring_stats
from .ssh import SshTarget

__all__ = ["KEEP", "Target"]

KEEP = 100_000                                       # メモリに持つ出来事の数（あふれた数は dropped）


@dataclass
class Target:
    ssh: SshTarget
    sudo: bool
    caps: dict
    sudo_password_env: str | None = None             # sudo のパスワードを入れた環境変数の名前（パスワードは持たない）
    agent: Agent | None = None
    deployed: dict | None = None
    probes: dict[str, dict] = field(default_factory=dict)
    by_bp: dict[int, str] = field(default_factory=dict)
    next_bp: int = 0
    clock: TargetClock = field(default_factory=TargetClock)
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=KEEP))
    seq: int = 0
    pending: list = field(default_factory=list)   # (出来事, 受けたときの記録): 後ろに組ができるのを待つ（記録中に受けた分だけ）
    examined: int = 0                                # pending の先頭のうち、組の最後の時刻 examined_last で調べ済み（まだ待つ）の数
    examined_last: int | None = None
    not_recorded: int = 0                            # 記録中に受けたのに記録に入れられなかった数（組が足りない・記録が替わった）
    dropped: int = 0
    trace_clock: str | None = None                   # uprobes の出来事の時計（trace_clock の名前）
    sync_stop: threading.Event | None = None
    sync_error: str | None = None
    by_seq: collections.OrderedDict = field(default_factory=collections.OrderedDict)   # 通しの番号 → 出来事（A と B を 1 つに）
    ring: dict | None = None                         # ring_open の結果（phys・size・capacity・first）
    ring_reader: RingReader | None = None
    ring_stats: dict = field(default_factory=new_ring_stats)
    ring_stop: threading.Event | None = None
    gdb: GdbRelay | None = None                     # linux.gdb.serve の中継
    memap: object | None = None                      # linux.memap.connect の止めずに読む口（read_words・info・close）
    ftrace: set = field(default_factory=set)         # 有効にした tracefs のイベント（group/name）
    trace_unparsed: int = 0                          # trace_pipe の、形の分からない行の数（捨てた）
    trace_unrouted: int = 0                          # 有効にしていないイベントの行の数（捨てた。ほかの人の tracefs の使用等）
    lock: threading.Lock = field(default_factory=threading.Lock)
