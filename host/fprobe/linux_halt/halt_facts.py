"""`HaltSession` の応答に載せる事実の組み立て（コアには触らない純粋な関数と、gdb のログのファイル）。

停止の報告（`StopReport`）の dict 化・プロセス一覧の行・ブレークポイントのジャーナルのファイルの事実・gdb のパケットログ。
"""
from __future__ import annotations

import json
import os

from .kernel import Kernel


def report_dict(report) -> dict:
    """StopReport をそのまま（method・cores_halted・skew_upper_bound_ns・basis・assumptions・bound_without_assumptions_ns）。"""
    return {"method": report.method, "cores_halted": list(report.cores_halted),
            "skew_upper_bound_ns": report.skew_upper_bound_ns, "basis": report.basis,
            "assumptions": list(report.assumptions),
            "bound_without_assumptions_ns": report.bound_without_assumptions_ns}


def process_rows(kernel: Kernel) -> list[dict]:
    """スレッドグループのリーダー（idle は除く）。並びはカーネルのタスク一覧の順。has_mm = false はカーネルスレッド。"""
    return [{"tgid": t.tgid, "comm": t.comm, "has_mm": bool(t.mm)} for t in kernel.leaders() if t.pid]


def journal_facts(path: str) -> dict:
    """ブレークポイントのジャーナルのファイルの事実（読むだけ。書き戻すのは recover）。entries は、死んだデバッガが BKPT を
    書いたかもしれない物理番地（今もメモリに BKPT があるかは読まない）。"""
    if not os.path.exists(path):
        return {"path": path, "exists": False, "entries": []}
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        return {"path": path, "exists": True,
                "entries": [{"pa": e["pa"], "width": e["width"], "owners": e.get("owners", [])} for e in entries]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"path": path, "exists": True, "entries": None, "error": "%s: %s" % (type(e).__name__, e)}


class FileLog:
    """gdb のパケットのログのファイル（gdb のサーバが終わると閉じる）。"""

    def __init__(self, path: str):
        self._f = open(path, "w", buffering=1, encoding="utf-8")

    def __call__(self, message: str) -> None:
        print(message, file=self._f, flush=True)

    def close(self) -> None:
        self._f.close()
