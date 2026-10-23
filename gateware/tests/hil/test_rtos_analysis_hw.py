"""RTOS のカーネルオブジェクトの一覧（U08）と時間軸の解析（U09）の実機試験: Nucleo F401RE の SRAM で freertos_demo。

freertos_demo（host/firmware/freertos_demo）: キュー cmd_queue・2 値セマフォ done_sem・ミューテックス log_mutex・
数えるセマフォ slots・タイマ tick_timer（10 ms）と oneshot（止まったまま）・イベントグループ sync_events・
ストリームバッファ log_stream・メッセージバッファ msg_buffer（読む人がいないので埋まる）。worker（1）が log_mutex を
約 6 ms 持つ間に ctrl（3）が待つ（20 ms ごと。継承で worker が 3 に上がる）。sensor は 5 ms ごと。
"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FREERTOS_DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "freertos_demo" / "freertos_demo.elf"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


@pytest.fixture
def running(probe):
    probe.program_ram(str(FREERTOS_DEMO))
    probe.target_resume()
    time.sleep(1.0)
    return probe


def test_kernel_objects_of_a_halted_target(running):
    probe = running
    probe.target_halt()
    out = probe.rtos_objects()
    assert out["consistent"] and not out["warnings"], out["warnings"]
    objects = {o["name"]: o for o in out["objects"]}
    kinds = {name: o["kind"] for name, o in objects.items()}
    assert {kinds[n] for n in ("cmd_queue", "TmrQ")} == {"queue"}
    assert kinds["done_sem"] == "binary_semaphore" and kinds["log_mutex"] == "mutex"
    assert objects["slots"]["fields"] == {"count": 3, "max": 3} and objects["slots"]["source"] == "variable"
    tick = objects["tick_timer"]["fields"]
    assert tick["active"] and tick["auto_reload"] and tick["period_ticks"] == 10
    assert tick["callback"] == "tick_timer_callback" and 0 <= tick["remaining_ticks"] <= 10
    assert not objects["oneshot"]["fields"]["active"]
    assert kinds["sync_events"] == "event_group"
    assert objects["log_stream"]["fields"]["used"] == 64                  # 読む人がいないので埋まったまま
    assert objects["msg_buffer"]["kind"] == "message_buffer" and objects["msg_buffer"]["fields"]["messages"] == 8
    assert "xTimerQueue" in objects["TmrQ"].get("aliases", [])
    heap = out["heap"]
    assert heap["scheme"] == "heap_4" and 0 < heap["min_free"] <= heap["free"] < heap["total"]
    assert heap["allocations"] > 10 and heap["free_blocks"] >= 1
    for inv in out["contention"]["inversions"]:                          # 止めた瞬間に ctrl が待っていれば継承済み
        assert inv["mutex"] == "log_mutex" and inv["inherited"], inv
    assert out["contention"]["deadlocks"] == []


def test_kernel_objects_and_tasks_while_running(running):
    probe = running
    out = probe.rtos_objects(live=True)
    assert not out["halted"] and not out["consistent"]
    assert {"cmd_queue", "tick_timer", "log_mutex"} <= {o["name"] for o in out["objects"]}
    # 走行中に読むとリストの移動（準備完了 ⇄ 遅延）と重なってタスクが欠けることがある（consistent = false の意味。
    # 実機で 3 つ欠けた回もある）。
    # 何度か読めば 7 つそろう
    counts = []
    for _ in range(10):
        tasks = probe.rtos_tasks(live=True)
        assert not tasks["consistent"]
        counts.append(len(tasks["tasks"]))
        if counts[-1] == 7:
            break
    assert 7 in counts and all(1 <= c <= 7 for c in counts), counts


def test_sampled_timeline_load_jobs_memory_and_inversions(running):
    probe = running
    probe.target_halt()                                                  # 一覧を一貫して取ってから始める
    started = probe.rtos_timeline_start(method="sampled", rate_hz=5_000, memory=True,
                                        tasks=["ctrl", "sensor", "worker"], mutexes=["log_mutex"])
    assert started["mutexes"] == ["log_mutex"] and started["memory"]["heap"]
    probe.target_resume()
    time.sleep(2.0)
    stopped = probe.rtos_timeline_stop()
    # 抜けは gap_factor を明示したときだけ（既定は判定しない）: 実際の間隔は分解能（resolution_us）として事実で返る
    print("resolution:", stopped.get("resolution_us"), stopped["warnings"])
    load = probe.rtos_timeline_load(window_us=100_000)
    assert 0.2 < load["mean_load"] < 0.8, load["mean_load"]               # worker が約 30 %（6 ms / 20 ms）
    jobs = probe.rtos_timeline_jobs(task="sensor")
    sensor = jobs["runs"]["sensor"]                                       # 出来事が無いので区間（ジョブは推定しない）
    # 方法 sampled は、サンプルの間隔（語を足すほど長い）より短い実行を取りこぼす: 回数は下限。sensor は 5 ms ごとに
    # 数 µs しか走らないので、見えた回の周期は 5 ms の倍数になる
    assert sensor["runs"] > 10, sensor
    median = sensor["period_us"]["median"]
    assert median > 4000, sensor                                          # 見えた回の始まりはサンプルの間隔だけずれる
    memory = probe.rtos_timeline_memory()
    assert 0 < memory["stacks"]["worker"]["max_used"] < memory["stacks"]["worker"]["size"]
    assert memory["heap"]["min_free"] > 0
    inv = probe.rtos_timeline_inversions()
    assert inv["available"] and inv["count"] >= 20, inv["count"]          # 20 ms ごとに ctrl が待つ
    assert inv["unbounded"] == 0, [(e["t0"], e["t1"], e["duration"], e["interference"]) for e in inv["episodes"] if e["unbounded"]]                              # 継承が効いている（間の sensor は割り込めない）
    assert all(e["holder"] == "worker" and e["waiter"] == "ctrl" for e in inv["episodes"])


def test_itm_timeline_with_exception_trace(running):
    probe = running
    probe.peripheral_write("DBG.DBGMCU_CR", fields={"TRACE_IOEN": 1})   # SWO のピンを有効に（STM32）
    started = probe.rtos_timeline_start(method="itm", swo_baud=4_000_000, core_clock_hz=16e6, exceptions=True,
                                        kernel=False)   # カーネルの出来事と例外を合わせると 4 Mbaud でも溢れる（台帳 B18）
    assert started["exceptions"]
    time.sleep(1.0)
    stopped = probe.rtos_timeline_stop()
    # SysTick → PendSV の出入りは数 µs に 6 パケット（約 18 バイト）を出し、4 Mbaud（1 バイト 2.5 µs）では ITM の FIFO が
    # あふれることがある（台帳 B18）。あふれは数えて知らせ、並びは崩さない
    issues = stopped["exceptions"]
    print("\nexceptions", issues)
    assert issues["inconsistencies"] == 0 or issues["overflows"], stopped    # 並びの崩れはあふれで欠けたときだけ
    assert not issues["overflows"] or any("あふれ" in w for w in stopped["warnings"]), stopped
    profile = probe.rtos_timeline_profile()
    systick = profile["interrupts"]["SysTick"]
    span_ms = (stopped["t1"] - stopped["t0"]) / stopped["timebase_hz"] * 1e3     # 1 ms ごと（窓は sleep より長い）
    assert 0.9 * span_ms <= systick["count"] <= 1.1 * span_ms + 2, (systick, span_ms)
    assert "PendSV" in profile["interrupts"]                              # タスクの切り替え
    share = profile["share"]
    assert share["idle"] > 0.2 and 0 < share["interrupt"] < 0.2, share
    jobs = probe.rtos_timeline_jobs()
    assert 4500 < jobs["runs"]["sensor"]["period_us"]["median"] < 5500
