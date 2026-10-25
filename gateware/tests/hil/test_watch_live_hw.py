"""変数の観測の拡充（U43）の実機試験: Nucleo F401RE に RAM で走らせた dwarf_demo（walk の再帰を回し続ける）。
- 100 kHz（10 µs 周期）でサンプラが読めること（CTX-01 の最小周期）
- 走行中の書き込み（止めない。CUBE-11・UCP-01）
- ポインタをたどる式を周期ごとに引き直すこと（head を差し替える）
- 値の条件で取得を始め、プリトリガ・ポストで切ること（CUBE-13・UCP-03）
ターゲット側のバッファの吸い出し（watch_buffer_*）は、配列と数を書く試験用ファームが要る（未作成。偽の機器の試験のみ）。"""

import time
from pathlib import Path
from tests.hil.values import num

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
DWARF_O0 = Path(__file__).resolve().parents[3] / "host" / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.program_ram(str(DWARF_O0))
    p.target_resume()
    yield p
    p.watch_stop()
    p.target_reset()
    p.close()


def test_the_sampler_reads_as_fast_as_it_can_when_asked_for_100_khz(probe):
    """100 kHz を頼む（SWD 4 MHz = 実際 3.857 MHz）。B17 の前は 1 巡回（SELECT・CSW・TAR・DRW・RDBUFF・CTRL/STAT・戻し）が
    約 120 µs（約 8.4 kHz）だった。B17（遅延の戻し）で 1 か所 1 語の定常は DRW・RDBUFF・CTRL/STAT の 3 転送
    = 計算で 37.3 µs（約 27 kHz）。周期を飛ばさず、間を空けずに巡回する（できるだけ速く）。速さを測って記録に写す。
    10.03 MHz（12 MHz を要求）で約 69 kHz（docs/superpowers/specs/2026-10-18-b17-sampler-lazy-restore-design.md §4）。"""
    visits = probe._symbols.variable("walk.visits").address
    probe.watch_set([f"0x{visits:08X}"], rate_hz=100_000)
    time.sleep(1.0)
    status = probe.watch_status()
    samples = probe.watch_series(f"0x{visits:08X}", last=4096)["samples"]
    gaps = [b["time_us"] - a["time_us"] for a, b in zip(samples, samples[1:])]
    mean = sum(gaps) / len(gaps)
    print(f"\n100 kHz を頼んだ: 実際 {1e6 / mean:.0f} Hz（間隔 平均 {mean:.1f} µs・最小 {min(gaps):.1f}・最大 {max(gaps):.1f}）",
          status)
    assert status["errors"] == 0, status
    assert mean < 1.2 * min(gaps) + 5, (min(gaps), mean)                        # 周期を飛ばさない（揃った間隔）
    assert 1e6 / mean > 22_000, mean                                            # B17: 計算で約 27 kHz（従来 8.4 kHz）
    assert num(samples[-1]["value"]) > num(samples[0]["value"])                           # 走っている
    probe.watch_stop()


def test_host_reads_while_the_sampler_holds_the_line(probe):
    """B17: サンプラが線を持ったまま（held）全速で読む間に、pyOCD でメモリを読む。ホストの転送の前にだけ SELECT・CSW・TAR を
    戻すので、ホストの読みは壊れず（ELF の命令の並びと一致）、サンプラの値も走り続ける。ホストが休んでいる間は戻さない。"""
    visits = probe._symbols.variable("walk.visits").address
    entry = probe._symbols.entry
    text = next(s for s in probe._symbols.segments if s.address <= entry < s.address + len(s.data))
    probe.watch_set([f"0x{visits:08X}"], rate_hz=100_000)
    time.sleep(0.2)
    assert probe.watch_status()["held"]                                          # 休んでいる間は線を持ったまま
    for _ in range(50):
        assert bytes.fromhex(probe.read_memory(text.address, 256)) == bytes(text.data[:256])
    status = probe.watch_status()
    samples = probe.watch_series(f"0x{visits:08X}", last=4096)["samples"]
    print("\nホストの読み 50 回の間:", status)
    assert status["errors"] == 0, status
    assert num(samples[-1]["value"]) > num(samples[0]["value"])
    probe.watch_stop()


def test_writes_while_running_do_not_stop_the_target(probe):
    done = probe.watch_write("sensors[1].scale", 3.5)
    assert num(done["value"]) == 3.5 and probe.target_status()["state"] == "RUNNING", done
    bits = probe.watch_write("sensors[1].flags.level", -2)
    assert num(bits["value"]) == -2 and not bits["atomic"]
    probe.watch_set(["sensors[1].scale"], rate_hz=1000)
    time.sleep(0.1)
    assert num(probe.watch_values()["values"][0]["value"]) == 3.5
    probe.watch_stop()


def test_pointer_expressions_follow_a_new_target(probe):
    probe.watch_set(["head->pos.x"], chase_rate_hz=200)
    time.sleep(0.3)
    assert num(probe.watch_values()["values"][0]["value"]) == 10                    # sensors[0]
    probe.watch_write("head", probe._symbols.variable("sensors").address + probe._watch_target("sensors[0]").size)
    time.sleep(0.3)
    assert num(probe.watch_values()["values"][0]["value"]) == -1                    # sensors[1]
    series = probe.watch_series("head->pos.x")["samples"]
    gaps = [b["time_us"] - a["time_us"] for a, b in zip(series, series[1:])]
    print("chase", len(series), sum(gaps) / len(gaps))
    probe.watch_write("head", probe._symbols.variable("sensors").address)
    probe.watch_stop()


def test_a_value_condition_starts_a_capture_with_pretrigger(probe):
    visits = probe._symbols.variable("walk.visits").address
    name = f"0x{visits:08X}"
    probe.watch_set([name], rate_hz=10_000)
    time.sleep(0.05)
    now = num(probe.watch_values()["values"][0]["value"])
    probe.watch_trigger(start={"variable": name, "op": ">=", "value": now + 50_000}, pre_s=0.001, post_s=0.002)
    result = probe.watch_capture(wait_s=10)
    assert result["state"] == "done" and result["reason"] == "post", result
    samples = result["samples"][name]
    assert samples[0]["time_us"] < result["t_start_us"] <= samples[-1]["time_us"]
    gaps = [b["time_us"] - a["time_us"] for a, b in zip(samples, samples[1:])]
    expected = 3000 / max(100.0, sum(gaps) / len(gaps))                         # 3 ms ÷ 実際の間隔（10 kHz に届かない）
    assert 0.8 * expected <= len(samples) <= 1.2 * expected + 1, (len(samples), expected)
    probe.watch_stop()
