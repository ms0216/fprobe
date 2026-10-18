"""実行の制御（U01・U02・U53）の実機試験: Nucleo F401RE（Cortex-M4F）の SRAM に dwarf_demo（-O0）を読み込む。
行単位のステップ（into / over / out / until、割り込みの踏み越え）、ベクタキャッチと故障の要約、リセットの種類、
ターゲットの関数の呼び出し。

配線: いつもの SWD（PMOD のデバッグポート）。hardware のリセットの試験だけ、プローブの nRESET（デバッグポートの
2 番）を Nucleo の NRST（CN4 の 5 番、または CN7 の 14 番）につなぐ（3.3V 系どうし。nRESET はオープンドレインで
Low を引くだけなので逆流は無い）。つないでいなければその試験は失敗する（pc がリセットの入口にならない）。
"""

from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"
SOURCE = (FIRMWARE / "dwarf_demo" / "dwarf_demo.c").read_text(encoding="utf-8").splitlines()
SYST_CSR, SYST_RVR = 0xE000E010, 0xE000E014


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.vector_catch(["hard_fault"])                              # pyOCD の既定に戻す
    p.target_reset()
    p.close()


def at_leaf_sum(probe) -> dict:
    probe.program_ram(str(DWARF_O0))
    probe.breakpoint_set("leaf_sum")
    stop = probe.run_until_halt(timeout_s=3)
    probe.breakpoint_clear("leaf_sum")
    assert stop["reason"] == "breakpoint" and stop["function"] == "leaf_sum", stop
    return stop


def test_step_over_and_until_follow_the_source_lines(probe):
    stop = at_leaf_sum(probe)
    assert stop["line"] == line_of("int total = 0;"), stop
    over = probe.step_line("over")
    assert (over["reason"], over["line"]) == ("step", line_of("for (int i = 0; i < 3; i++)")), over
    over = probe.step_line("over")
    assert over["line"] == line_of("total += s->samples"), over
    until = probe.step_line("until")                             # ループの残りを終えるまで後ろへ戻らない
    assert until["line"] not in (line_of("total += s->samples"),), until
    advanced = probe.step_line("until", location=f"dwarf_demo.c:{line_of('probe_point(total + corner.x);')}")
    assert advanced["line"] == line_of("probe_point(total + corner.x);"), advanced


def test_step_into_and_out_with_the_return_value(probe):
    at_leaf_sum(probe)
    probe.step_line("until", location=f"dwarf_demo.c:{line_of('probe_point(total + corner.x);')}")
    into = probe.step_line("into")
    assert (into["function"], into["line"]) == ("probe_point", line_of("sink = marker;")), into
    out = probe.step_line("out")
    assert out["function"] == "leaf_sum", out
    out = probe.step_line("out")                                 # leaf_sum から walk へ。戻り値は int
    assert out["function"] == "walk" and out["return_value"]["type"] == "int", out
    assert isinstance(out["return_value"]["value"], int), out


def test_an_interrupt_during_a_step_is_stepped_over(probe):
    at_leaf_sum(probe)
    ticks = probe.evaluate("s_ticks")["value"]
    probe.write_memory(SYST_RVR, (2000).to_bytes(4, "little").hex())     # 16 MHz で 125 µs ごと
    probe.write_memory(SYST_CSR, (7).to_bytes(4, "little").hex())        # 有効・割り込み・コアの時計
    try:
        masked = probe.step_line("over")                          # 既定: 割り込みは止める
        assert masked["reason"] == "step" and masked["function"] == "leaf_sum", masked
        unmasked = probe.step_line("over", mask_interrupts=False)
        assert unmasked["reason"] == "step" and unmasked["function"] == "leaf_sum", unmasked
        assert probe.evaluate("s_ticks")["value"] >= ticks, "割り込みの中の処理が走ったか"
    finally:
        probe.write_memory(SYST_CSR, (0).to_bytes(4, "little").hex())


def test_vector_catch_stops_at_the_fault_with_its_cause(probe):
    at_leaf_sum(probe)
    assert probe.vector_catch(["hard_fault", "state_error"])["unsupported"] == []
    probe.write_expression("callback", 0)                        # reset_handler が callback(sink) で 0 番地へ分岐する
    stop = probe.run_until_halt(timeout_s=3)
    assert stop["reason"] == "vector_catch", stop
    assert "INVSTATE" in stop["fault"]["causes"], stop
    probe.vector_catch(["hard_fault"])


def test_reset_kinds(probe):
    probe.program_ram(str(DWARF_O0))
    for kind in ("system", "core", "emulated"):
        status = probe.target_reset(halt=True, kind=kind)
        assert status["state"] == "HALTED" and status["kind"] == kind, status
    status = probe.target_reset(halt=True, kind="hardware")       # nRESET を NRST につないだとき
    assert status["state"] == "HALTED", status
    entry = int.from_bytes(bytes.fromhex(probe.read_memory(0x08000004, 4)), "little") & ~1
    assert status["pc"] == entry, (hex(status["pc"]), hex(entry))


def test_call_functions_and_keep_the_stopped_state(probe):
    stop = at_leaf_sum(probe)
    registers = probe.read_registers()
    out = probe.call_function("square", [7])
    assert out["returned"] and out["return"]["value"] == 49 and out["restored"], out
    out = probe.call_function("leaf_sum", ["&sensors[1]", 0])     # samples[0] = 100 + 200 + 300
    assert out["return"]["value"] == 600, out
    out = probe.call_function("default_handler", [], timeout_s=0.3)   # 戻らない関数: 打ち切って元に戻す
    assert out["reason"] == "timeout" and out["restored"], out
    assert probe.read_registers() == registers                    # 止まっていた状態のまま
    assert probe.target_status()["pc"] == stop["pc"]
    assert probe.step_line("over")["reason"] == "step"           # 続けてデバッグできる
