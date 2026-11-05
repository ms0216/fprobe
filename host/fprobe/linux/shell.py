"""ターゲットの shell に渡す・ターゲットの出力を読むための小さな道具（probe_linux から）。"""
from __future__ import annotations


def text_of(data: bytes) -> str:
    """ターゲットの出力（バイト列）を文字に（読めないバイトは置き換える）。"""
    return data.decode(errors="replace")


def perf_where(pid: int | None) -> str:
    """perf の対象: プロセス pid か、無ければ全体 -a。"""
    return f"-p {int(pid)}" if pid is not None else "-a"


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
