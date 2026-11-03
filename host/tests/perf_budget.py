"""速さの約束の試験の共通の測り方。host/tests の「この量をこの時間以内」の試験（性能の約束）はこれを使う。

方針（2026-11-03。PC が混んでいると 1 回の測りが上限を超えて落ちた: test_recording の 100 万件が 11.1 秒）:
- 約束は上限（budget_s）。上限は要求から決める（例: 届く速さに追いつく = 量 ÷ 届く速さ）か、要求の無いものは「この
  実装が保つべき桁」として決めて、手元の値と余裕（上限 ÷ 手元の値）を試験の注に書く。測った値に倍率を掛けて決めない。
- 時計: 1 つのスレッドで走る計算は CPU 時間（clock="cpu" = time.process_time。ほかのプロセスに CPU を取られて待った
  時間を数えない）。スレッドで並列に走る計算・入出力の待ちが本質の計算は経過時間（clock="wall" = time.perf_counter。
  CPU 時間は全スレッドの和になり、並列の速さを測れない）。
- 揺れ: 上限を超えた回だけ測り直し、attempts 回（既定 3）の最良で比べる。上限以内の回が 1 つあれば、その実装は約束を
  満たせる（遅くなった実装はどの回も超える）。超えたまま終われば、すべての回の値を誤りの文に出す。
- 準備（データ・記録を作る等）は測る中に入れない: setup が毎回の準備をし、その戻り値を run に渡す。
これと別に、「待たずに返る・時間切れで止まる」を確かめる試験（決めた時間で誤りにする等）は機能の試験で、ここの対象外
（経過時間の上限は、待ってしまったときとの違いが十分に大きい幅にする）。
"""

import time
from collections.abc import Callable
from typing import Any

CLOCKS = {"cpu": time.process_time, "wall": time.perf_counter}


def within(budget_s: float, run: Callable[..., Any], *, clock: str = "cpu", attempts: int = 3,
           setup: Callable[[], Any] | None = None) -> tuple[float, Any]:
    """run を測り、最良の秒が budget_s 以内であることを確かめる。(最良の秒, その回の run の戻り値)。
    setup があれば毎回その戻り値を run の引数にする（測らない）。"""
    if clock not in CLOCKS:
        raise ValueError(f"clock は {' / '.join(CLOCKS)}")
    now = CLOCKS[clock]
    tries: list[float] = []
    best: tuple[float, Any] | None = None
    for _ in range(attempts):
        args = () if setup is None else (setup(),)
        t0 = now()
        value = run(*args)
        seconds = now() - t0
        tries.append(seconds)
        if best is None or seconds < best[0]:
            best = (seconds, value)
        if seconds <= budget_s:
            break
    assert best[0] <= budget_s, (f"{clock} の時間 {', '.join(f'{s:.3f}' for s in tries)} 秒のどれも上限 {budget_s} 秒を"
                                 f"超えた（{attempts} 回）")
    return best
