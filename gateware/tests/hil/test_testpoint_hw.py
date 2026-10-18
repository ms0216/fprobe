"""テストポイント（F7、DT+ 方式）の実機試験: 挿入済みの試験用ファーム（2 線の GPIO: CLK = PA5 → ch4、DAT = PA9 → ch5。
今の配線のまま）を Nucleo F401RE の SRAM で走らせ、深いバッファで捕捉して解読・解析する。"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from fprobe.testpoint import ProbeTable, analyze
from fprobe.testpoint.decode import decode_gpio_serial
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "testpoint_demo"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


def test_two_wire_testpoints_are_decoded_and_analyzed(probe):
    probe.program_ram(str(DEMO / "testpoint_demo_serial.elf"))
    probe.target_resume()
    time.sleep(0.1)
    probe.la_deep_capture(rate_hz=50e6, rise=1 << 4, before_beats=0, after_beats=100_000)
    tr = probe._capture
    decoded = decode_gpio_serial(tr, 4, 5)
    report = analyze(decoded.items, ProbeTable.load(DEMO / "testpoints.json"), hz=decoded.hz)
    losses = [a for a in report.anomalies if a.type == "loss"]
    assert report.hits > 1000 and len(losses) <= 2, (report.hits, losses[:3])   # 端で切れた記録だけ
    assert {"step", "fib", "validate", "classify", "sum_until", "parse"} <= set(report.functions)
    other = {(a.type, a.function) for a in report.anomalies if a.type not in ("loss", "no_exit", "no_entry")}
    assert other <= {("long_call", "fib")}, other                                # fib は引数で仕事が変わる
    assert report.functions["step"].calls > 10
