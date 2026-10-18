"""画面の端末の口（terminal.sources / terminal.read）と画面の状態（ui.state）の実機試験: Nucleo F401RE に RAM で
走らせた rtt_demo（RTT のチャンネル 0 の tick と echo、100 ms ごとに ITM のポート 0 へ "itm <ms>"、セミホスティング）。

手順（本流の担当が走らせる。`pytest gateware/tests/hil/test_terminal_hw.py --hil`）:
1. RTT: rtt_start の後、terminal_read("rtt0") が tick の行を返し、rtt_read（AI の読み）の位置を動かさない。
2. SWO: swo_start の後、terminal_read("swo0") が ITM のポート 0 の行を返し、terminal_sources に swo0 が出る。
3. セミホスティング: 出力が terminal_read("semihosting") で読める。
4. 画面の状態: ui_state_set / ui_state_get（機器に触れない）。
"""

import time
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
    yield p
    p.target_reset()
    p.close()


def read_until(probe, source, needle, timeout_s=3.0):
    deadline, text, since = time.monotonic() + timeout_s, "", 0
    while needle not in text and time.monotonic() < deadline:
        r = probe.terminal_read(source, since=since, timeout_s=0.3)
        text, since = text + r["data"], r["next"]
    return text


def test_rtt_channel_is_read_without_moving_the_tool_cursor(probe):
    probe.program_ram(str(RTT_DEMO))
    probe.target_resume()
    probe.rtt_start()
    assert "rtt0" in [s["id"] for s in probe.terminal_sources()["sources"]]
    assert "tick" in read_until(probe, "rtt0", "tick")
    assert "tick" in probe.rtt_read(timeout_s=1)["data"]                      # AI の読みは最初から
    probe.rtt_write(text="hello\n")
    assert "echo: hello" in read_until(probe, "rtt0", "echo: hello")
    probe.rtt_stop()
    probe.target_halt()


def test_swo_port_text(probe):
    probe.program_ram(str(RTT_DEMO))
    probe.swo_start(2_000_000)
    probe.terminal_read("swo0")                                              # 背景の受信を始める
    probe.target_resume()
    text = read_until(probe, "swo0", "itm ")
    assert text.count("itm ") >= 2, text
    assert "swo0" in [s["id"] for s in probe.terminal_sources()["sources"]]
    probe.target_halt()


def test_semihosting_output(probe):
    probe.program_ram(str(RTT_DEMO))
    probe.target_resume()
    time.sleep(0.1)
    probe.target_halt()
    probe.variable_write("semihosting_enabled", 1)
    probe.variable_write("semihosting_exit", 4)
    probe.run_until_halt(timeout_s=5)
    assert "hello from semihosting" in probe.terminal_read("semihosting")["data"]


def test_screen_state(probe):
    out = probe.ui_state_set(view={"source": "capture", "t0": 0, "t1": 100}, cursors={"selection": 50}, frame=0)
    assert out["view"]["t1"] == 100 and probe.ui_state_get()["cursors"]["selection"] == 50
    probe.ui_state_set(clear=["view", "cursors"])
