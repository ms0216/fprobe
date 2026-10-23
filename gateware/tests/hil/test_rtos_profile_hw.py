"""RTOS の認識（F6）と性能解析（F5）の実機試験: Nucleo F401RE の SRAM で freertos_demo・rtt_demo を走らせる。"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


def test_freertos_tasks_waits_and_backtraces(probe):
    probe.program_ram(str(FIRMWARE / "freertos_demo" / "freertos_demo.elf"))
    for _ in range(20):                                          # ctrl が待ちの瞬間に止まるまで（走る瞬間もある）
        probe.target_resume()
        time.sleep(0.1)
        probe.target_halt()
        tasks = {t["name"]: t for t in probe.rtos_tasks()["tasks"]}
        if tasks["ctrl"]["state"] == "blocked" and tasks["sensor"]["state"] == "blocked":
            break
    info = probe.rtos_info()
    assert info["rtos"] == "freertos" and info["tasks"] == 7 and info["scheduler"] == "running"   # タイマのタスクを含む
    assert tasks["dormant"]["state"] == "suspended"
    # ctrl はたいてい cmd_queue を待つ。worker が log_mutex を持つ間に受け取ったときは log_mutex を待つ（優先度の逆転）
    wait = tasks["ctrl"]["wait"]
    assert (wait["name"], wait["kind"]) in (("cmd_queue", "queue"), ("log_mutex", "mutex")), wait
    assert tasks["sensor"]["wait"]["reason"] == "delay"
    assert not any(t["stack"]["overflow"] for t in tasks.values())
    frames = [f["function"] for f in probe.rtos_backtrace("ctrl")["frames"]]
    assert frames[-1] == "ctrl_task" and ("xQueueReceive" in frames or "xQueueSemaphoreTake" in frames), frames


def test_task_timeline_by_sampling_the_current_tcb(probe):
    probe.program_ram(str(FIRMWARE / "freertos_demo" / "freertos_demo.elf"))
    probe.target_resume()
    time.sleep(0.2)
    probe.rtos_timeline_start(method="sampled", rate_hz=5_000)
    time.sleep(1.0)
    stats = probe.rtos_timeline_stop()["stats"]
    tasks = stats["tasks"]
    assert stats["switches"] > 500, stats                          # 抜けは既定で判定しない（gap_factor を明示したときだけ）
    # worker はミューテックスを持ったまま回る（優先度の逆転の試験用）ので、IDLE より多いこともある
    assert tasks["IDLE"]["cpu"] > 0.05 and tasks["worker"]["cpu"] > 0.05, {n: t["cpu"] for n, t in tasks.items()}
    assert 0.95 < sum(t["cpu"] for t in tasks.values()) <= 1.001
    assert {"ctrl", "sensor", "worker", "fpu"} <= set(tasks)


def test_profile_by_swo_pc_sampling_and_exception_trace(probe):
    probe.program_ram(str(FIRMWARE / "rtt_demo" / "rtt_demo.elf"))
    probe.target_resume()
    time.sleep(0.2)
    probe.peripheral_write("DBG.DBGMCU_CR", fields={"TRACE_IOEN": 1})               # SWO のピンを有効に（STM32）
    probe.profile_start(method="itm", core_clock_hz=16e6, rate_hz=10_000)
    time.sleep(1.0)
    itm = probe.profile_stop()
    assert itm["overflows"] == 0 and itm["total"] > 5000, itm
    systick = probe.profile_exceptions()["exceptions"][0]
    assert systick["name"] == "SysTick" and 950 <= systick["count"] <= 1050                # 1 ms ごと
    # 割合は、周期の割り込み（SysTick 16000 サイクル）と PC サンプルの間隔（2048）が噛み合うとエイリアシングで偏る
    # （17 サイクルのハンドラは 0% か 1/125 になる）。実際の負荷は例外トレースの時間で見る: 17 サイクル × 1000 回 ≒ 0.1%
    assert 0.0005 < systick["load"] < 0.002, systick
    main = next(r for r in itm["rows"] if r["function"] == "reset_handler")
    # DWT_PCSR（FPGA のサンプラ）: 大きい割合は SWO の結果と信頼区間の範囲で合う
    probe.profile_start(method="pcsr", rate_hz=4_733)
    time.sleep(2.0)
    pcsr = probe.profile_stop()
    other = next(r for r in pcsr["rows"] if r["function"] == "reset_handler")
    assert pcsr["total"] > 5000 and other["ci"][0] <= main["ci"][1] + 0.01 and main["ci"][0] - 0.01 <= other["ci"][1], (main, other)
    cover = probe.profile_coverage(functions=["systick_handler"])
    assert cover["functions"][0]["line_fraction"] == 1.0
