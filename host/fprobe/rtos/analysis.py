"""タスクの時間軸の解析（U09）: 窓ごとの負荷・ジョブ・横取りを除いた区間・スタックとヒープの推移・割り込みとタスクの合成・
優先度の逆転の区間。入出力なし（時刻はすべてタイムベース。hz を渡すと µs も付ける）。

仕様: docs/superpowers/specs/2026-10-18-u08-u09-rtos-objects-timeline-design.md の 2 節。

    load_series(tl, window, idle={idle_tcb})                   # 窓ごとの CPU 負荷・タスクごとの割合
    job_summary(find_jobs(tl, idle=..., events=...))           # ジョブ（出来事で区切る）の実行時間・周期・応答時間
    run_summary(tl, idle=...)                                   # タスクの区間の回数・時間・周期・次に動いたタスク
    run_time_in(tl, pairs)                                     # 2 点の間で、そのタスクが実際に使った時間
    memory_series(samples, slots)                              # 保存された SP・ヒープの残量の推移
    execution_profile(tl, activations, idle=...)               # スレッド・割り込み・アイドルの割合
    inversion_episodes(tl, samples, watches, priorities, order)       # ミューテックスの持ち主・待ちの先頭の語から逆転の区間

割り込みの区間（isr）は方法 itm の例外トレースから（`exception_activations`）。無ければ割り込みはタスクの時間に含まれる。
"""
from .analysis_inversion import MutexWatch, inversion_episode, inversion_episodes, inversion_summary
from .analysis_jobs import Job, KernelEvent, find_jobs, job_summary, run_summary, run_time_in
from .analysis_series import (BLOCK_KINDS, Intervals, MemorySlot, exception_activations, execution_profile,
                              isr_intervals, load_series, memory_series, runs_by_task)

__all__ = ["BLOCK_KINDS", "Intervals", "Job", "KernelEvent", "MemorySlot", "MutexWatch", "exception_activations",
           "execution_profile", "find_jobs", "inversion_episode", "inversion_episodes", "inversion_summary",
           "isr_intervals", "job_summary", "load_series", "memory_series", "run_summary", "run_time_in", "runs_by_task"]
