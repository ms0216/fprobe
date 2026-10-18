"""スナップショット（U06）の実機試験: Nucleo F401RE に RAM で走らせた試験用ファーム。
止めて保存 → 切断して開き、呼び出し履歴・C の式・周辺レジスタ・故障の解析がその時と同じか。
接続し直して書き戻すと RAM・レジスタが戻り、再開して走り続けるか。ELF のコアファイルの書き出し。"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from fprobe.snapshot import Snapshot
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"
RTT_DEMO = FIRMWARE / "rtt_demo" / "rtt_demo.elf"
TARGET = "stm32f401xe"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect(TARGET, frequency_hz=4_000_000)
    yield p
    p.snapshot_close()
    if p._debug is None:
        p.target_connect(TARGET, frequency_hz=4_000_000)
    p.target_reset()
    p.close()


def test_a_snapshot_answers_the_same_questions_without_the_target(probe, tmp_path):
    probe.program_ram(str(DWARF_O0))
    probe.breakpoint_set("probe_point")
    assert probe.run_until_halt(timeout_s=3)["reason"] == "breakpoint"
    live_bt = probe.backtrace(variables=True)
    live_expr = probe.evaluate("head->next->name")
    live_rcc = probe.peripheral_read("RCC.CR")
    started = time.monotonic()
    saved = probe.snapshot_save(name="hil", peripherals=["RCC", "GPIOA", "USART2"])
    print(f"保存 {saved['bytes']} バイト、{time.monotonic() - started:.2f} 秒")
    assert any("USART2.DR" == s["path"] for s in saved["skipped"])           # データの口は読まない
    probe.breakpoint_clear()
    probe.target_disconnect()

    opened = probe.snapshot_open(saved["path"])
    assert opened["elf_path"] and opened["halt"]["function"] == "probe_point"
    assert probe.backtrace(variables=True) == live_bt
    assert probe.evaluate("head->next->name") == live_expr
    assert probe.peripheral_read("RCC.CR")["registers"][0]["value"] == live_rcc["registers"][0]["value"]
    assert probe.target_fault()["faulted"] is False
    core = probe.snapshot_export_core(out=str(tmp_path / "hil.core"))
    assert Path(core["path"]).stat().st_size > 96 * 1024                    # F401 の SRAM 96 KB を含む
    print("GDB で確かめる:", core["gdb"])
    probe.snapshot_close()
    probe.target_connect(TARGET, frequency_hz=4_000_000)


def test_restore_puts_ram_and_registers_back_and_the_firmware_keeps_running(probe):
    probe.program_ram(str(RTT_DEMO))
    probe.target_resume()
    time.sleep(0.3)
    probe.target_halt()
    path = probe.snapshot_save(name="rtt")["path"]
    saved = Snapshot.load(path)
    ticks = probe.evaluate("ticks")["value"]
    probe.target_resume()
    time.sleep(0.3)
    probe.target_halt()
    assert probe.evaluate("ticks")["value"] > ticks + 100
    restored = probe.snapshot_restore(path)
    assert restored["code_checked"] and restored["state"] == "HALTED"
    assert probe.evaluate("ticks")["value"] == ticks
    assert probe.read_registers(["pc"])["pc"] == saved.registers["pc"]
    assert "VTOR" in restored["system"] and "SYST_CSR" in restored["system"]
    probe.target_resume()
    time.sleep(0.3)
    probe.target_halt()
    assert probe.evaluate("ticks")["value"] > ticks + 100                     # SysTick も戻って走り続ける
