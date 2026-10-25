"""RTOS・ヒープの解析の不具合の再現（検出専用。strict の xfail: 直ると XPASS で失敗して気づく）。
台帳: docs/reports/bugs/rtos.md"""

import pytest

from fprobe.heap import AllocationTracker
from fprobe.rtos.analysis import (KernelEvent, MemorySlot, execution_profile, find_jobs, load_series,
                                  memory_series, run_summary)
from fprobe.rtos.flow import blocking, inversions_from_ops
from fprobe.rtos.kernel_trace import KernelOp, KernelTrace
from fprobe.rtos.stackcheck import untouched_bytes
from fprobe.rtos.timeline import Gap, Run, Timeline


def tl_of(runs, t1=None, gaps=()):
    rs = [Run(h, n, a, b) for h, n, a, b in runs]
    return Timeline("itm", rs[0].t0, t1 if t1 is not None else rs[-1].t1, 1, False, rs, [],
                    [Gap(a, b, "lost") for a, b in gaps])


TWO_SAME_NAME = [(1, "worker", 0, 10), (2, "worker", 10, 20), (3, "IDLE", 20, 30)]


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-01: run_summary はタスク名をキーにするので、同じ名前の 2 タスクの一方が消える")
def test_run_summary_same_name_tasks_both_kept():
    out = run_summary(tl_of(TWO_SAME_NAME), idle={3})
    assert sum(row["runs"] for row in out.values()) == 2


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-02: load_series の tasks は名前をキーにした辞書内包で、同じ名前のタスクの割合が上書きで消える")
def test_load_series_same_name_tasks_shares_add_up():
    out = load_series(tl_of(TWO_SAME_NAME), 30, idle={3})
    w = out["windows"][0]
    assert abs(w["tasks"]["worker"] - 20 / 30) < 1e-9


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-03: execution_profile の threads が名前で束ねられ、別タスクの時間が 1 行に混ざる（handle は先頭のもの）")
def test_execution_profile_same_name_tasks_separate_rows():
    out = execution_profile(tl_of(TWO_SAME_NAME), None, idle={3})
    assert len(out["threads"]) == 3


def _mutex_trace(prios_waiters):
    """持ち主 H(=1) が mutex M(=0x100) を取り、待ち手が順に block する。"""
    ops = [KernelOp(1, 0, "receive", 0, 1, 0, 0x100, "mutex")]
    for i, w in enumerate(prios_waiters):
        ops.append(KernelOp(2 + i, 1 + i, "block", 0, w, 0, 0x100, "mutex"))
    return KernelTrace(ops)


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-04: inversions_from_ops は urgency(w) or -inf で、urgency が 0 の待ち手を最低に扱う（order=-1 で優先度 0 = 最高が落ちる）")
def test_inversions_from_ops_order_minus1_priority0_is_top_waiter():
    # 小さい方が高い（order=-1）。持ち主 1 は優先度 5、待ち手 2 は優先度 0（最高）、待ち手 3 は優先度 3
    tl = tl_of([(1, "holder", 0, 100), (2, "w0", 100, 110), (3, "w3", 110, 120)], t1=120)
    prios = {1: 5, 2: 0, 3: 3}
    out = inversions_from_ops(tl, _mutex_trace([3, 2]), prios, order=-1)
    # 待ち手 3 (優先度 3) の block で逆転が始まり、より高い 2 (優先度 0) が block したら待ち手が 2 に変わる
    assert [e["waiter"] for e in out["episodes"]] == ["w3", "w0"]


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-05: 打ち切り (truncated) のジョブの exec_time が release より前の時間と割り込みを含み、blocked のジョブと定義が違う")
def test_truncated_job_exec_time_same_definition_as_blocked():
    tl = tl_of([(1, "a", 0, 100)], t1=100)
    ev = [KernelEvent(50, "ready", 1)]                    # release=50、終わりの出来事なし
    job = find_jobs(tl, events=ev)[0]
    assert job.end_reason == "truncated"
    assert job.exec_time == 50                            # release(50)〜100 の実行。実際は 100（0〜100 の全区間）


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-06: 抜け (gap) をまたぐジョブが blocked のまま統計に入る（truncated の定義は「抜け・観測の端」）")
def test_job_across_gap_is_truncated():
    tl = tl_of([(1, "a", 0, 10), (1, "a", 50, 60)], t1=60, gaps=[(10, 50)])
    ev = [KernelEvent(0, "ready", 1), KernelEvent(60, "block", 1)]
    job = find_jobs(tl, events=ev)[0]
    assert job.end_reason == "truncated"


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-07: memory_series はヒープの slot が複数あると最後の 1 つで上書きする（先のヒープが黙って消える）")
def test_memory_series_two_heap_slots_kept():
    slots = {0x10: MemorySlot("heap", "h1", total=100), 0x20: MemorySlot("heap", "h2", total=200)}
    out = memory_series([(0, 0x10, 50), (1, 0x20, 150)], slots)
    heaps = out["heap"]
    assert isinstance(heaps, (list, dict)) and ("h1" in str(heaps) and "h2" in str(heaps))


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-08: blocking は同じ名前の別オブジェクトの待ちを objects で束ねる（名前がキー）")
def test_blocking_same_name_objects_separate():
    ops = []
    seq = 0
    for obj, base in ((0x100, 0), (0x200, 10)):
        ops.append(KernelOp(base, seq, "block", 0, 1, 0, obj, "queue")); seq += 1
        ops.append(KernelOp(base + 5, seq, "ready", 0, 9, 0, 0, None, 1)); seq += 1
    objs = {0x100: {"name": "q", "kind": "queue"}, 0x200: {"name": "q", "kind": "queue"}}
    out = blocking(KernelTrace(ops), {1: "t"}, objs)
    assert len(out["objects"]) == 2


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-09: untouched_bytes は読みが要求より短く返っても要求の長さを数える（短い読みで未使用の深さを過大に出す）")
def test_untouched_bytes_short_read_not_overcounted():
    def short(addr, n):
        return bytes([0xA5]) * min(n, 10)
    assert untouched_bytes(short, 0, 100, 0xA5) <= 10


@pytest.mark.xfail(strict=True, reason="BUG-RTOS-10: AllocationTracker.report の top に負を渡すと [:top] で末尾が欠ける（検証が無い）")
def test_report_negative_top_rejected():
    t = AllocationTracker()
    t.alloc(0, 0x1000, 8, "a")
    t.alloc(1, 0x2000, 16, "b")
    with pytest.raises(ValueError):
        t.report(2, top=-1)
