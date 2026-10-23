"""ヒープの解析（U27）の実機試験: Nucleo F401RE の SRAM で freertos_demo（heap_4、20 KB）。

freertos_demo は起動時にタスク（TCB・スタック）・キュー・タイマ・ストリームバッファを pvPortMalloc で確保し、その後は
確保しない。記録を入口から始めれば、起動の確保がすべて記録に入り、走査の使用中のブロックと一致する。
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


def test_trace_from_the_entry_matches_the_scan(probe):
    probe.program_ram(str(FREERTOS_DEMO))                         # 入口で止まっている
    started = probe.heap_trace_start()
    assert {f["function"] for f in started["functions"]} == {"pvPortMalloc", "vPortFree"}, started
    probe.target_resume()
    time.sleep(2.0)                                               # 起動の確保は最初の数 ms（当たるたびに数 ms 止まる）
    probe.target_halt()
    report = probe.heap_trace_report(min_age_s=0.5)
    probe.heap_trace_stop()
    assert report["allocations"] >= 10 and not report["issues"], report
    scan = probe.heap_scan(blocks=200)["heaps"][0]
    assert scan["scheme"] == "heap_4" and not scan["issues"], scan["issues"]
    assert scan["used_blocks"] == report["live"]["count"], (scan, report)
    assert scan["counters"]["allocations"] - scan["counters"]["frees"] == scan["used_blocks"]
    callers = {c["caller"].split("+")[0] for c in scan["callers"]} if "callers" in scan else set()
    print("呼び出し元:", callers, "生きている確保の呼び出し元:", [c["caller"] for c in report["live_by_caller"]])
    assert any(c.startswith(("xTaskCreate", "xQueue", "xTimer", "xStream", "prvInitialise")) for c in
               (lc["caller"] for lc in report["live_by_caller"])), report["live_by_caller"]


def test_scan_of_a_running_target_and_compare(probe):
    probe.program_ram(str(FREERTOS_DEMO))
    probe.target_resume()
    time.sleep(1.0)
    first = probe.heap_scan(live=True, compare=True)["heaps"][0]
    second = probe.heap_scan(live=True, compare=True)["heaps"][0]
    assert not first["issues"] and second["changes"]["new_count"] == 0       # 起動の後は確保しない
    assert 0 <= second["fragmentation"] < 1 and second["used"] + second["free"] <= 20 * 1024
