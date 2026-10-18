"""PC サンプリングのエイリアシングの検出。

PC サンプルの間隔と周期的な割り込み（SysTick 等）の周期が噛み合うと、サンプルはハンドラの同じ位相ばかりに当たり、
割合が 0 か何倍にも偏る（2026-10-18 に実機で、SysTick の本当の負荷 0.1% が 0% や 0.8% になった）。ITM の方式では
同じ記録に例外トレース（入る・出るの時刻）があるので、その本当の負荷とサンプルの割合（信頼区間）を突き合わせて知らせる。
"""

from math import sqrt

__all__ = ["aliasing_notes"]


def aliasing_notes(sampled: list[dict], traced: list[dict], total: int) -> list[str]:
    """sampled: 例外ごとのサンプルの表（exception・name・samples・ci）、traced: 例外トレースの負荷（exception・name・
    load）、total: サンプルの総数。本当の負荷がサンプルの信頼区間の外にあれば、その例外を挙げた注意を返す。"""
    rows = {r["exception"]: r for r in sampled}
    notes = []
    for e in traced:
        load, name = e["load"], e.get("name") or f"例外 {e['exception']}"
        row = rows.get(e["exception"])
        if row is None:
            expected = load * total
            # サンプルが 0 回: 期待値が十分に大きい（ポアソンで 0 回の確率が 1% 未満 ≒ 期待 4.6 回以上）のに 0 なら偏り
            if expected >= 4.6:
                notes.append(f"{name}: 例外トレースの負荷は {load:.3%} なのに PC サンプルは 0 回（期待 {expected:.1f} 回）。"
                             "サンプルの間隔と割り込みの周期が噛み合っている（エイリアシング）: 負荷は例外トレースの値を使う"
                             "（rate_hz を少し変えると偏りが変わる）")
            continue
        low, high = row["ci"]
        slack = 2 / sqrt(max(total, 1)) * sqrt(max(load, 1e-9))     # 例外トレースの側の揺らぎの分の余裕
        if load < low - slack or load > high + slack:
            notes.append(f"{name}: 例外トレースの負荷は {load:.3%} なのに PC サンプルの割合は {row['samples'] / max(total, 1):.3%}"
                         f"（信頼区間 {low:.3%}〜{high:.3%}）。サンプルの間隔と割り込みの周期が噛み合っている"
                         "（エイリアシング）: 負荷は例外トレースの値を使う（rate_hz を少し変えると偏りが変わる）")
    return notes
