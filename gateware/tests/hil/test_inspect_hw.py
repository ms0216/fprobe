"""止めて調べる・止めずに見る道具（担当 E の組み込み）の実機試験: Nucleo F401RE に RAM で走らせた試験用ファーム。
DWARF（呼び出し履歴・式・書き込み）、逆アセンブル、SVD の周辺レジスタ、メモリの比較、RTT、セミホスティング。"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"
RTT_DEMO = FIRMWARE / "rtt_demo" / "rtt_demo.elf"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


def test_dwarf_backtrace_expressions_and_writes(probe):
    probe.program_ram(str(DWARF_O0))
    probe.breakpoint_set("probe_point")
    stop = probe.run_until_halt(timeout_s=3)
    assert stop["reason"] == "breakpoint", stop
    frames = probe.backtrace(variables=True)
    names = [f["function"] for f in frames]
    # reset_handler → walk(head, 3) → walk(2) → walk(1) → walk(0) → leaf_sum → probe_point
    assert names[:7] == ["probe_point", "leaf_sum", "walk", "walk", "walk", "walk", "reset_handler"], names
    depths = [next(v["value"] for v in f["arguments"] if v["name"] == "depth") for f in frames[2:6]]
    assert depths == [0, 1, 2, 3], depths
    assert probe.evaluate("head->next->name")["value"]["string"] == "beta"
    after = probe.write_expression("sensors[1].mode", "MODE_IDLE")
    assert probe.evaluate("sensors[1].mode")["value"]["enum"] == "MODE_IDLE", after
    text = probe.disasm_text()["text"]
    assert "=>" in text and probe.disasm_text()["memory"] == "target"
    probe.breakpoint_clear("probe_point")


def test_peripheral_registers_by_name(probe):
    rcc = probe.peripheral_read("RCC.CR")
    fields = {f["name"]: f["value"] for f in rcc["registers"][0]["fields"]}
    assert fields["HSION"] == 1 and fields["HSIRDY"] == 1, rcc
    usart = probe.peripheral_read("USART2")
    assert any("DR" in s for s in map(str, usart["skipped"])), usart["skipped"]
    probe.peripheral_write("RCC.AHB1ENR", fields={"GPIOAEN": 1})              # 試験用ファームは GPIOA の時計を入れない
    probe.peripheral_write("GPIOA.ODR", fields={"ODR5": 0})
    written = probe.peripheral_write("GPIOA.ODR", fields={"ODR5": 1})          # PA5 = Nucleo の LED
    assert written["before"] != written["after"], written


def test_memory_view_marks_changes(probe):
    sink = probe._symbols.variable("walk.visits").address         # 走っている間 増え続ける（dwarf_demo）
    probe.target_resume()
    probe.memory_view(sink, 16, unit=32, compare=True)
    for _ in range(20):                                           # 読んだ瞬間に同じ値のこともある
        second = probe.memory_view(sink, 16, unit=32, compare=True)
        if any(any(r["changed"]) for r in second["rows"]):
            break
    assert second["compared"] and any(any(r["changed"]) for r in second["rows"]), second   # 走っている変数が変わる


def test_rtt_lines_and_echo(probe):
    probe.program_ram(str(RTT_DEMO))
    probe.target_resume()
    started = probe.rtt_start()
    assert started, started
    got = probe.rtt_read(timeout_s=2)
    assert "tick" in got["data"], got
    probe.rtt_write(text="hello\n")
    deadline, text = time.monotonic() + 2, ""
    while "echo: hello" not in text and time.monotonic() < deadline:
        text += probe.rtt_read(timeout_s=0.5)["data"]
    assert "echo: hello" in text, text
    status = probe.rtt_status()
    probe.rtt_stop()
    assert status, status


def test_semihosting_output_and_exit(probe):
    probe.program_ram(str(RTT_DEMO))
    probe.target_resume()                                         # 起動で .bss が 0 になるので、起動の後に書く
    time.sleep(0.1)
    probe.target_halt()
    probe.variable_write("semihosting_enabled", 1)
    probe.variable_write("semihosting_exit", 4)
    stop = probe.run_until_halt(timeout_s=5)
    assert stop["reason"] == "exit" and stop["exit_code"] == 3, stop
    out = probe.rtt_read(channel="semihosting", timeout_s=0.5)
    assert "hello from semihosting" in out["data"], out


def test_fault_is_explained_with_the_faulting_line(probe):
    """FPU を切って浮動小数点の命令を実行させる（UsageFault NOCP → HardFault）。原因と故障した行を返す。"""
    probe.program_ram(str(DWARF_O0))
    probe.target_resume()
    time.sleep(0.05)
    probe.target.write32(0xE000ED88, 0)                         # CPACR: CP10・CP11 を切る
    stop = probe.run_until_halt(timeout_s=0.3)                  # 故障のハンドラは無限ループなので時間切れで止まる
    report = probe.target_fault()
    assert report["faulted"] and {"FORCED", "NOCP"} <= {c["bit"] for c in report["causes"]}, report
    assert report["frame"]["pc"]["where"].startswith("leaf_sum"), report["frame"]
    assert stop["reason"] == "timeout"


def test_conditional_and_log_breakpoints(probe):
    probe.program_ram(str(DWARF_O0))
    probe.breakpoint_set("leaf_sum", log=["depth", "s->name"], message="葉")
    probe.target_resume()
    time.sleep(0.5)
    probe.target_halt()
    entries = probe.breakpoint_log()["entries"]
    assert len(entries) >= 3 and all(e["values"]["depth"] == 0 for e in entries), entries[:3]
    probe.breakpoint_clear()
    probe.breakpoint_set("walk", condition="depth == 1 && s->mode == MODE_FAULT")
    stop = probe.run_until_halt(timeout_s=3)
    assert stop["reason"] == "breakpoint" and probe.evaluate("depth")["value"] == 1, stop
    probe.breakpoint_clear()


def test_swo_printf_lines_and_exception_trace(probe):
    """rtt_demo は自分で TPIU（NRZ 2 Mbaud）・ITM・例外トレースを設定し、100 ms ごとに ITM のポート 0 へ "itm <ms>"。"""
    probe.program_ram(str(RTT_DEMO))
    probe.swo_start(2_000_000)
    probe._swo_drain()                                           # 前の受信の残りを捨てる
    probe.target_resume()
    lines = probe.swo_text(port=0, timeout_s=1.0)["lines"]
    assert len(lines) >= 5 and all(x.startswith("itm ") for x in lines), lines
    packets = probe.swo_decode(timeout_s=0.3, limit=5000)["packets"]
    systick = [x for x in packets if x["type"] == "ExceptionTrace" and x["name"] == "SysTick"]
    assert len(systick) > 100, len(systick)
    probe.target_halt()
