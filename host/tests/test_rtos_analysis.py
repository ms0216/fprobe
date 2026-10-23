"""fprobe.rtos.analysis（U09）: 時間軸の解析。窓ごとの負荷・ジョブ・横取りを除いた区間・スタックとヒープの推移・
割り込みとタスクの合成・優先度の逆転の区間。入出力なし（時間軸とサンプルを組み立てて渡す）。"""

import pytest

from fprobe.itm import ExceptionAction, ExceptionTrace, Overflow
from fprobe.profile.exceptions import Activation
from fprobe.rtos.analysis import (Intervals, KernelEvent, MemorySlot, MutexWatch, exception_activations,
                                  execution_profile, find_jobs, inversion_episodes, run_summary, isr_intervals, job_summary,
                                  load_series, memory_series, run_time_in)
from fprobe.rtos.timeline import Gap, Run, Timeline

H, M, L, IDLE = 1, 2, 3, 9
NAMES = {H: "high", M: "mid", L: "low", IDLE: "IDLE"}
PRIORITIES = {H: 3, M: 2, L: 1, IDLE: 0}


def timeline(runs, t1=None, gaps=(), method="itm"):
    rs = [Run(h, NAMES[h], t0, t1_) for h, t0, t1_ in runs]
    return Timeline(method, rs[0].t0, t1 if t1 is not None else rs[-1].t1, 1, method == "sampled", rs, [],
                    [Gap(a, b, "lost") for a, b in gaps])


# --- 部品 ----------------------------------------------------------------------------------------

def test_intervals_overlap():
    iv = Intervals([(10, 20), (30, 40)])
    assert iv.overlap(0, 100) == 20 and iv.overlap(15, 35) == 10 and iv.overlap(20, 30) == 0
    assert iv.overlap(12, 14) == 2 and Intervals([]).overlap(0, 10) == 0


def test_isr_intervals_merge_nested_activations():
    acts = [Activation(15, 10, 20, 10, 8), Activation(16, 12, 14, 2, 2), Activation(16, 30, 35, 5, 5)]
    assert isr_intervals(acts) == [(10, 20), (30, 35)]


def test_exception_activations_from_timed_packets():
    packets = [(100, ExceptionTrace(15, ExceptionAction.ENTER)), (130, ExceptionTrace(15, ExceptionAction.EXIT)),
               (131, ExceptionTrace(0, ExceptionAction.RETURN)), (200, Overflow()),
               (300, ExceptionTrace(16, ExceptionAction.ENTER)), (310, ExceptionTrace(16, ExceptionAction.EXIT))]
    acts, inconsistencies, overflows = exception_activations(packets)
    assert [(a.number, a.start, a.end) for a in acts] == [(15, 100, 130), (16, 300, 310)]
    assert overflows == 1 and inconsistencies == 0


# --- 窓ごとの負荷 ------------------------------------------------------------------------------------

def test_load_series_per_window_without_idle():
    tl = timeline([(L, 0, 30), (IDLE, 30, 100), (L, 100, 180), (IDLE, 180, 200)])
    out = load_series(tl, 100, idle={IDLE})
    w0, w1 = out["windows"]
    assert (w0["t0"], w0["t1"], w0["covered"]) == (0, 100, 100) and w0["load"] == pytest.approx(0.3)
    assert w0["tasks"] == {"low": pytest.approx(0.3), "IDLE": pytest.approx(0.7)}
    assert w1["load"] == pytest.approx(0.8)
    assert out["peak"]["t0"] == 100 and out["peak"]["load"] == pytest.approx(0.8)
    assert out["mean_load"] == pytest.approx(0.55)
    assert out["idle_tasks"] == ["IDLE"]                                 # 負荷から除いたタスク（事実として添える）


def test_load_series_excludes_gaps_and_counts_interrupts():
    tl = timeline([(L, 0, 30), (IDLE, 30, 100), (L, 100, 150), (IDLE, 170, 200)], gaps=[(150, 170)])
    out = load_series(tl, 100, idle={IDLE}, isr=[(10, 20)])
    w0, w1 = out["windows"]
    assert w0["isr"] == pytest.approx(0.1) and w0["tasks"]["low"] == pytest.approx(0.2)
    assert w0["load"] == pytest.approx(0.3)                             # 割り込みは負荷に入る
    assert w1["covered"] == 80 and w1["load"] == pytest.approx(50 / 80)


def test_load_series_needs_a_positive_window_and_says_nothing_without_runs():
    with pytest.raises(ValueError):
        load_series(timeline([(L, 0, 10)]), 0)
    empty = load_series(Timeline("sampled", 0, 0, 0, True), 100)
    assert empty["windows"] == [] and empty["mean_load"] is None


# --- ジョブ -----------------------------------------------------------------------------------------

def test_jobs_need_kernel_events_and_are_not_inferred_from_priorities():
    # 「次に動いたタスクの優先度が高ければ横取り、低ければ待った」と推定しない（固定優先度・継承なしの仮定に頼る）。
    # 出来事（ready / block）の無いタスクのジョブは作らない
    tl = timeline([(L, 0, 10), (H, 10, 20), (L, 20, 30), (IDLE, 30, 50), (H, 50, 60), (IDLE, 60, 100)])
    assert find_jobs(tl, idle={IDLE}, isr=[(12, 14)]) == []


def test_run_summary_is_the_facts_of_each_run():
    tl = timeline([(L, 0, 10), (H, 10, 20), (L, 20, 30), (IDLE, 30, 50), (H, 50, 60), (IDLE, 60, 100)])
    out = run_summary(tl, idle={IDLE}, isr=[(12, 14)], hz=1e6)
    assert "IDLE" not in out
    high, low = out["high"], out["low"]
    assert high["runs"] == 2 and high["exec"]["max"] == 10 and high["exec"]["min"] == 8      # 割り込みを除く
    assert high["period"]["mean"] == 40 and high["exec_us"]["max"] == pytest.approx(10)
    assert high["worst_exec"] == {"start": 50, "end": 60, "exec": 10}
    assert high["followed_by"] == {"low": 1, "IDLE": 1}                 # 次に動いたタスク（事実。理由は推定しない）
    assert low["runs"] == 2 and low["followed_by"] == {"high": 1, "IDLE": 1}


def test_run_summary_counts_runs_that_end_at_a_gap_or_the_edge():
    tl = timeline([(H, 0, 10), (M, 10, 20), (L, 20, 30), (L, 40, 50)], gaps=[(30, 40)])
    out = run_summary(tl)
    assert out["low"]["followed_by"] == {"(gap)": 1, "(end)": 1} and out["high"]["followed_by"] == {"mid": 1}


def test_jobs_from_kernel_events_have_response_times():
    tl = timeline([(IDLE, 0, 10), (H, 10, 15), (M, 15, 18), (H, 18, 20), (IDLE, 20, 100)])
    events = [KernelEvent(5, "ready", H), KernelEvent(20, "block", H)]
    (job,) = [j for j in find_jobs(tl, idle={IDLE}, events=events) if j.task == "high"]
    assert (job.release, job.start, job.end, job.exec_time, job.preemptions) == (5, 10, 20, 7, 1)
    assert job.response == 15 and job.end_reason == "blocked"
    assert job_summary([job])["high"]["response"]["max"] == 15


# --- 横取りを除いた区間 -------------------------------------------------------------------------------

def test_run_time_in_excludes_other_tasks_and_interrupts():
    tl = timeline([(L, 0, 10), (H, 10, 20), (L, 20, 30), (IDLE, 30, 100)])
    out = run_time_in(tl, [(0, 50), (60, 10)], isr=[(25, 27)])
    first, second = out["intervals"]
    assert (first["task"], first["wall"], first["own"], first["preempted"]) == ("low", 50, 18, 32)
    assert second["task"] == "IDLE" and second["own"] == 10
    only_low = run_time_in(tl, [(0, 50)], task=L)
    assert only_low["intervals"][0]["own"] == 20 and only_low["own"]["max"] == 20
    assert run_time_in(tl, [(95, 20)])["intervals"][0]["incomplete"]     # 時間軸の外にはみ出す


# --- スタック・ヒープの推移 -------------------------------------------------------------------------

def test_memory_series_from_saved_stack_pointers_and_heap_free():
    end = 0x2000_1000
    slots = {0x100: MemorySlot("stack", "low", L, base=end - 512, end=end), 0x200: MemorySlot("heap", "heap", total=1000)}
    samples = [(0, 0x100, end - 100), (10, 0x100, end - 300), (12, 0x100, end - 200), (20, 0x200, 900),
               (30, 0x200, 700), (31, 0x300, 1)]
    out = memory_series(samples, slots, points=2)
    low = out["stacks"]["low"]
    assert (low["size"], low["max_used"], low["min_used"], low["last_used"]) == (512, 300, 100, 200)
    assert low["max_at"] == 10 and low["points"] == [[0, 300], [16, 200]]       # 窓ごとの最大
    heap = out["heap"]
    assert (heap["min_free"], heap["last_free"], heap["total"]) == (700, 700, 1000) and heap["min_at"] == 30


def test_memory_series_flags_a_pointer_outside_the_stack():
    end = 0x2000_1000
    slots = {0x100: MemorySlot("stack", "low", L, base=end - 512, end=end)}
    out = memory_series([(0, 0x100, end - 600)], slots)
    assert out["stacks"]["low"]["overflow"]


# --- 割り込みとタスクの合成 -------------------------------------------------------------------------

def test_execution_profile_splits_thread_interrupt_and_idle():
    tl = timeline([(L, 0, 50), (IDLE, 50, 100)])
    acts = [Activation(15, 10, 20, 10, 10), Activation(16, 60, 65, 5, 5)]
    out = execution_profile(tl, acts, idle={IDLE}, names={16: "IRQ0 (uart)"})
    assert out["covered"] == 100
    assert out["threads"]["low"]["time"] == 40 and out["threads"]["IDLE"]["time"] == 45
    assert out["interrupts"]["SysTick"] == {"count": 1, "time": 10, "share": pytest.approx(0.1)}
    assert out["interrupts"]["IRQ0 (uart)"]["time"] == 5
    assert out["share"] == {"thread": pytest.approx(0.4), "interrupt": pytest.approx(0.15),
                            "idle": pytest.approx(0.45), "unattributed": pytest.approx(0)}
    assert execution_profile(tl, None, idle={IDLE})["interrupts"] is None


# --- 優先度の逆転 ----------------------------------------------------------------------------------

def test_inversion_episodes_from_sampled_mutex_words():
    watch = MutexWatch("log_mutex", holder_word=0x10, head_word=0x14, delta=0, empty=0x99)
    samples = [(0, 0x10, L), (1, 0x14, 0x99), (10, 0x14, H), (11, 0x10, L), (36, 0x14, H), (40, 0x10, H),
               (41, 0x14, 0x99)]
    tl = timeline([(L, 0, 20), (M, 20, 35), (L, 35, 40), (H, 40, 50)])
    out = inversion_episodes(tl, samples, [watch], PRIORITIES, names=NAMES)
    assert out["available"] and out["count"] == 1
    (ep,) = out["episodes"]
    assert (ep["mutex"], ep["holder"], ep["waiter"], ep["t0"], ep["t1"]) == ("log_mutex", "low", "high", 10, 40)
    assert ep["duration"] == 30 and ep["holder_time"] == 15 and ep["interference"] == {"mid": 15}
    assert ep["unbounded"] and out["max"]["duration"] == 30


def test_a_task_after_the_last_sample_that_saw_the_inversion_is_not_interference():
    """終わりのサンプルの手前（逆転を最後に見た後）に中間のタスクが動いても、逆転が解けた後かもしれないので数えない。"""
    watch = MutexWatch("log_mutex", holder_word=0x10, head_word=0x14, delta=0, empty=0x99)
    samples = [(0, 0x10, L), (1, 0x14, 0x99), (10, 0x14, H), (11, 0x10, L), (30, 0x14, H), (40, 0x10, H),
               (41, 0x14, 0x99)]
    tl = timeline([(L, 0, 32), (M, 32, 35), (H, 35, 50)])
    (ep,) = inversion_episodes(tl, samples, [watch], PRIORITIES, names=NAMES)["episodes"]
    assert ep["interference"] == {} and not ep["unbounded"] and ep["t1"] == 40


def test_inversion_without_mutex_words_is_not_guessed():
    out = inversion_episodes(timeline([(L, 0, 10)]), [], [], PRIORITIES)
    assert not out["available"] and out["reason"]
