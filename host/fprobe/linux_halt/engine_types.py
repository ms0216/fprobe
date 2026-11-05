"""`Engine` の型: 誤り `EngineError`、停止の事実 `Stop`、停止の報告と基準の比較 `StopResult`、数え `EngineStats`、シグナル番号。

どれも値だけを持つ（実行制御は `engine`、停止の分類は `engine_classify`）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .core_access import StopReport

SIGINT, SIGTRAP, SIGKILL = 2, 5, 9


class EngineError(RuntimeError):
    """操作を完了できなかった。メッセージが理由（デバッガの利用者に見せる）。"""


@dataclass
class Stop:
    kind: str           # trap（ステップ/run-to 完了）| swbreak | hwbreak | watch | int | exit | signal
    #                     | timeout（run() の待つ上限を過ぎた。全コアは走っている）
    tid: int = 0
    addr: int = 0       # watch: 当たった（であろう）範囲の先頭
    access: str = ""    # watch: "w" 書き込み | "r" 読み出し | "a" 両方（登録した種類）
    candidates: int = 0  # watch: 当たりうる登録の数（1 = 一意）
    code: int = 0        # exit/signal: カーネルの終了コード（(status << 8) | 終了させるシグナル）
    signal: int = 0      # signal: プロセスを終わらせるシグナル


@dataclass
class StopResult:
    report: StopReport
    skew_verdict: str   # "within"（上限が基準以下）| "exceeded"（超えた）| "unknown"（上限が分からない）


@dataclass
class EngineStats:
    counters: dict
    skew: dict          # 種類（"event" / "interrupt" / "request"）→ {"stops", "max_ns"(None=測れた回なし), "unknown", "exceeded"}
    resumes_cut_short: int
