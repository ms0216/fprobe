"""PC サンプリングと例外トレースの突き合わせ。

PC サンプルの間隔と周期的な割り込み（SysTick 等）の周期が噛み合うと、サンプルはハンドラの同じ位相ばかりに当たり、
割合が 0 か何倍にも偏ることがある（2026-10-18 に実機で、SysTick の本当の負荷 0.1% が 0% や 0.8% になった）。ITM の方式では
同じ記録に例外トレース（入る・出るの時刻）があるので、その負荷がサンプルの割合の信頼区間の外にあれば、その事実を書く。
原因（噛み合い）は断定しない。経験則の余裕（「期待 4.6 回以上」等）は使わず、0 回のときも同じ信頼区間で比べる。
"""

from .stats import wilson_interval

__all__ = ["aliasing_notes"]


def aliasing_notes(sampled: list[dict], traced: list[dict], total: int, confidence: float = 0.95) -> list[str]:
    """sampled: 例外ごとのサンプルの表（exception・name・samples）、traced: 例外トレースの負荷（exception・name・
    load）、total: サンプルの総数、confidence: 信頼区間の信頼度（プロファイルと同じ値を渡す）。例外トレースの負荷が
    サンプルの割合の信頼区間の外にある例外ごとに 1 行。"""
    rows = {r["exception"]: r for r in sampled}
    notes = []
    for e in traced:
        load, name = e["load"], e.get("name") or f"例外 {e['exception']}"
        hits = rows[e["exception"]]["samples"] if e["exception"] in rows else 0
        low, high = wilson_interval(hits, total, confidence)
        if not low <= load <= high:
            notes.append(f"{name}: 例外トレースで測った負荷 {load:.3%} は、PC サンプルの割合 {hits / max(total, 1):.3%}"
                         f"（{hits} / {total} 回）の {confidence:.0%} 信頼区間 {low:.3%}〜{high:.3%} の外")
    return notes
