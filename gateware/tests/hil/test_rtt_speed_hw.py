"""RTT の速さ（U50）の実機試験: Nucleo F401RE に RAM で走らせた rtt_demo。ホストが毎周期読む方式と、FPGA の DAP
サンプラが WrOff を見張って変化したときだけ読む方式で、受けたバイト・デバッグポートの読みの数を比べる。
数値は print する（-s で見る。台帳の速度の記録に写す）。"""

from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
RTT_DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "rtt_demo" / "rtt_demo.elf"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.program_ram(str(RTT_DEMO))
    p.target_resume()
    yield p
    p.rtt_stop()
    p.target_reset()
    p.close()


@pytest.mark.parametrize("interval_s", [0.01, 0.001])
def test_host_polling_rate_and_reads(probe, interval_s):
    probe.rtt_start(wake="poll", interval_s=interval_s)
    m = probe.rtt_measure(duration_s=3.0)
    print("poll", interval_s, m)
    assert m["bytes"] > 0 and "tick" in probe.rtt_read(timeout_s=1)["data"], m
    assert m["reads_per_s"] >= m["polls_per_s"] * 0.9, m       # 毎周期、記述子を読む


def test_sampler_wake_reads_only_on_changes(probe):
    probe.rtt_start(wake="poll", interval_s=0.001)
    polled = probe.rtt_measure(duration_s=3.0)
    probe.rtt_start(wake="sampler", interval_s=0.001, sampler_rate_hz=10_000)
    sampled = probe.rtt_measure(duration_s=3.0)
    print("poll", polled)
    print("sampler", sampled)
    assert sampled["bytes"] > 0 and sampled["wake"]["wakes"] > 0, sampled
    assert sampled["wake"]["idle"] > 0 and sampled["reads_per_s"] < polled["reads_per_s"] / 2, (polled, sampled)
    assert abs(sampled["bytes_per_s"] - polled["bytes_per_s"]) <= 0.2 * polled["bytes_per_s"] + 50   # 取りこぼさない
    status = probe.rtt_status()
    assert status["wake"]["lost_events"] == 0, status
    probe.rtt_stop()
    assert probe._sampler_user is None


def test_rtt_echo_still_works_with_the_sampler(probe):
    probe.rtt_start(wake="sampler")
    probe.rtt_read(timeout_s=0.5)
    probe.rtt_write(text="hello\n")
    text = ""
    for _ in range(8):
        text += probe.rtt_read(timeout_s=0.5)["data"]
        if "echo: hello" in text:
            break
    assert "echo: hello" in text, text
    probe.rtt_stop()
