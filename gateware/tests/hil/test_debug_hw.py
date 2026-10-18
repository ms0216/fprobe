"""ソースの言葉でのデバッグ（MCP の道具）: ELF のシンボル・ブレークポイント・ウォッチポイント・実行して待つ・変数・
呼び出し履歴を、Nucleo の SRAM に読み込んだ試験用ファームウェア（host/firmware/sram_demo）で確かめる。Flash には書かない。

ボードは USB 版（fprobe）で開く。環境変数 FPROBE_ETH_HOST があれば Ethernet 版（fprobe_eth）で開く。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil

ELF = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"
ETH_HOST = os.environ.get("FPROBE_ETH_HOST")


@pytest.fixture(scope="module")
def probe():
    if ETH_HOST:
        subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "fprobe_eth", "--program"], check=True)
        p = Probe(eth=ETH_HOST)
    else:
        program("fprobe")
        p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.program_ram(str(ELF))
    yield p
    p.breakpoint_clear()
    p.target_reset()                                   # Flash の元のファームウェアに戻す
    p.close()


def test_breakpoint_by_function_name_reports_where_it_stopped(probe):
    probe.breakpoint_set("reached_milestone")
    first = probe.run_until_halt(2)
    # 関数名のブレークポイントはプロローグの後（GDB と同じ。Symbols.after_prologue）: 引数が読める場所で止まる
    assert first["reason"] == "breakpoint" and first["where"].startswith("reached_milestone+"), first
    assert "(sram_demo.c:" in first["where"], first
    assert probe.variable_read("counter")["value"] == 256
    second = probe.run_until_halt(2)                   # ブレークポイントの上から再開しても止まり直さない
    assert second["reason"] == "breakpoint" and probe.variable_read("counter")["value"] == 512
    frames = probe.backtrace()
    assert frames[0]["where"].startswith("reached_milestone") and len(frames) >= 2
    probe.breakpoint_clear("reached_milestone")
    assert probe.breakpoint_list()["breakpoints"] == []


def test_watchpoint_on_a_variable_stops_after_the_write(probe):
    probe.watchpoint_set("milestone")
    stop = probe.run_until_halt(2)
    assert stop["reason"] == "watchpoint"
    counter, milestone = probe.variable_read("counter")["value"], probe.variable_read("milestone")["value"]
    assert milestone == counter and milestone % 256 == 0
    probe.watchpoint_clear("milestone")


def test_variables_by_name_and_timeout(probe):
    assert probe.variable_read("table", index=2)["value"] == 0x33333333
    probe.variable_write("table", 0xCAFEF00D, index=3)
    assert probe.variable_read("table")["value"].endswith("0df0feca")
    info = probe.symbol_info("tick")
    assert info["kind"] == "function" and info["where"].startswith("tick+0")
    stop = probe.run_until_halt(0.2)                   # 何も置いていなければ時間切れで止まる
    assert stop["reason"] == "timeout" and probe.target_status()["state"] == "HALTED"
    assert probe.target_status()["where"].split("+")[0] in ("tick", "reset_handler", "reached_milestone")


def test_resume_from_a_breakpoint_in_a_loop_moves_on(probe):
    # 画面の「実行」（target_resume）でブレークポイントの上から再開しても、同じ場所で止まり直さず次の回へ進む
    probe.breakpoint_set("reached_milestone")
    probe.run_until_halt(2)
    first = probe.variable_read("counter")["value"]
    probe.target_resume()
    deadline = time.monotonic() + 2
    while probe.target_status()["state"] != "HALTED" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe.variable_read("counter")["value"] == first + 256
    probe.breakpoint_clear("reached_milestone")
