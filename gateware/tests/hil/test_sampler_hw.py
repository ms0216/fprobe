"""F4 DAP サンプラ（実機）: RAM で走る sram_demo の counter を FPGA が 10 kHz で読み、時刻付きのイベントにする。
同時にホスト（pyOCD）がメモリを読んでも、どちらも壊れない。"""

import time
from pathlib import Path

import pytest

from fprobe.events import EventLog, KIND_SAMPLE
from fprobe.mcp_server import Probe
from fprobe.sampler import Sampler
from fprobe.symbols import Symbols
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
ELF = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.program_ram(str(ELF))
    yield p
    p.target_reset()
    p.close()


def test_counter_is_sampled_at_10khz_while_the_host_reads_memory(probe):
    dev = probe.session.device
    hz = dev.capabilities().timebase_hz
    counter = Symbols(ELF).variable("counter")
    table = Symbols(ELF).variable("table")
    s = Sampler(dev)
    s.configure([(counter.address, 4)], period_s=1e-4)
    log = EventLog(dev)
    probe.target_resume()
    log.start()
    s.start()
    reads = []
    t_end = time.monotonic() + 0.3
    while time.monotonic() < t_end:                              # ホストの読みと交互に
        reads.append(probe.read_memory(table.address, 16))
    s.stop()
    time.sleep(0.01)
    log.stop()
    status = s.status()
    samples = [e for e in log.read() if e.kind == KIND_SAMPLE]
    assert status["errors"] == 0 and len(samples) > 1000, (status, len(samples))
    assert all(e.address == counter.address for e in samples)
    values = [e.data for e in samples]
    assert all(b >= a for a, b in zip(values, values[1:])) and values[-1] > values[0]      # 走っているので増える
    gaps = [(b.time - a.time) / hz for a, b in zip(samples, samples[1:])]
    late = [g for g in gaps if not 0.9e-4 < g < 1.1e-4]
    assert len(late) <= max(status["overruns"], len(gaps) // 100), (late[:5], status)      # 周期どおり（ホストの読みで遅れる回は少ない）
    assert reads and all(r.startswith("11111111") for r in reads), reads[:3]              # ホストの読みも壊れない


def test_watch_reads_a_variable_by_name_without_stopping(probe):
    """watch API: 名前 → DWARF の番地と型 → サンプラ → 型の値。ターゲットは走ったまま。"""
    probe.target_resume()
    set_ = probe.watch_set(["counter"], rate_hz=1000)
    assert set_["watches"][0]["size"] == 4
    time.sleep(0.2)
    first = probe.watch_values()["values"][0]
    time.sleep(0.1)
    series = probe.watch_series("counter", last=1000)["samples"]
    status = probe.watch_stop()
    assert status["errors"] == 0 and first["value"] is not None, (status, first)
    assert len(series) > 50 and series[-1]["value"] > first["value"]                     # 走っているので増える
    gaps = [b["time_us"] - a["time_us"] for a, b in zip(series, series[1:])]
    assert sorted(gaps)[len(gaps) // 2] == pytest.approx(1000, rel=0.02)                   # 1 kHz
    assert probe.target_status()["state"] == "RUNNING"
