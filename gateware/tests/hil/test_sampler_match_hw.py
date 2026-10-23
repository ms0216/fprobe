"""R21（実機）: サンプラが DHCSR を 10 kHz で読み、MCU が止まった瞬間（S_HALT が立った標本）にクロストリガでロジアナの
深いバッファを捕捉する。止めるのはホスト（pyOCD）。仕様 docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from fprobe.sampler import Sampler
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
ELF = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"
DHCSR, S_HALT = 0xE000EDF0, 1 << 17


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.program_ram(str(ELF))
    yield p
    p.sampler_trigger_clear()
    p.target_reset()
    p.close()


def test_halt_seen_by_the_sampler_triggers_the_analyzer(probe):
    dev = probe.session.device
    hz = dev.capabilities().timebase_hz
    s = Sampler(dev)
    s.configure([(DHCSR, 4)], period_s=1e-4)
    probe.target_resume()
    s.start()
    probe.sampler_trigger_set(address=DHCSR, mask=S_HALT, value=S_HALT, halt=False, la=True)
    probe.la_deep_arm(external_only=True, rle=False)
    time.sleep(0.05)
    before = probe.sampler_trigger_status()
    assert before["seen"] > 100 and before["count"] == 0 and not before["matched"], before
    probe.target_halt()
    time.sleep(0.05)
    st = probe.sampler_trigger_status()
    cap = probe.la_deep_collect(timeout_s=10)
    s.stop()
    gap_us = (st["time"] - st["prev_time"]) * 1e6 / hz
    print("status", st, f"標本の間 {gap_us:.1f} µs", "LA のトリガ", cap.get("trigger_time"))
    assert st["count"] == 1 and st["matched"] and st["prev_time"] < st["time"]
    assert gap_us < 1000                                    # 直前の標本との間（サンプラの周期 100 µs の数倍まで。事実を出す）
    assert cap.get("trigger_time") is not None and cap["trigger_time"] >= st["time"]
    print(f"一致の標本 → LA のトリガ: {(cap['trigger_time'] - st['time']) * 1e9 / hz:.0f} ns")
