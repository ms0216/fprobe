"""mixin が Probe に要求する共通の部品の型（typing.Protocol）。

probe_*.py の mixin は、単独では動かず、Probe（fprobe.probe）に混ぜて使う。mixin が頼る共通の属性・メソッド
（機器のロック・セッション・接続中のターゲット・記録・ロジアナの捕捉・通知の送り先）をここに型で並べる。
各 mixin の docstring には、ここに無い相手（ほかの mixin のメソッド等）だけを書く。
実行時は何もしない（Protocol を継承しない。型検査と読む人のための約束）。import の輪を作らないよう、
他の probe_* を import しない。
"""

import threading
from typing import Any, Callable, Protocol

from .collector import Collector
from .debug import Debugger
from .la import LogicAnalyzer, Transitions
from .recording import Recorder, Recording
from .session import Session
from .symbols import Symbols


class ProbeHost(Protocol):
    """Probe の核（fprobe.probe.Probe.__init__ が作る状態と、機器の開閉）。"""

    _lock: threading.RLock                      # 機器に触るときは必ずこの中で
    _symbols: Symbols | None                    # elf_load で読んだ ELF
    _recorder: Recorder | None                  # 記録中の記録
    _collector: Collector | None                # 記録へイベントを取り込む流れ
    _view: Recording | None                     # 読む対象の記録（記録中のもの、または開いたもの）
    _la: LogicAnalyzer | None                   # 仕掛けた内蔵のロジアナの捕捉
    _capture: Transitions | None                # 最後の捕捉
    _capture_info: dict | None
    _link: dict | None                          # つないでいる先（target・frequency_hz・wire）
    _publish: Callable[[str, Any], None] | None    # デーモンの通知の送り先（attach_publisher）

    @property
    def session(self) -> Session: ...

    @property
    def target(self) -> Any: ...

    @property
    def dbg(self) -> Debugger: ...

    def _caps(self) -> Any: ...
