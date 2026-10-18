"""fprobe-daemon（実機）: AI 役と人間役の 2 つの利用者が同じ fprobe を使い、記録が流れ、一時停止で AI が待つ。
ボードは USB 版（fprobe）。環境変数 FPROBE_ETH_HOST があれば Ethernet 版。"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from fprobe.api.client import DaemonClient
from fprobe.api.daemon import create_app
from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
ELF = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"
ETH_HOST = os.environ.get("FPROBE_ETH_HOST")


@pytest.fixture(scope="module")
def url(tmp_path_factory):
    if ETH_HOST:
        subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "fprobe_eth", "--program"], check=True)
        backend = Probe(eth=ETH_HOST)
    else:
        program("fprobe")
        backend = Probe(SERIAL)
    app = create_app(backend, token=None, activity_dir=tmp_path_factory.mktemp("activity"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api"
    server.should_exit = True
    thread.join(10)
    backend.close()


def test_ai_debugs_while_human_watches_and_pauses(url):
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    seen = []
    gui.subscribe(["activity"], lambda topic, data: seen.append(data["method"]))
    ai.call("target.connect", target="stm32f401xe", frequency_hz=4_000_000)
    ai.call("target.program_ram", path=str(ELF))
    ai.call("debug.breakpoint.set", location="reached_milestone")
    stop = ai.call("debug.run_until_halt", timeout_s=2)
    assert stop["reason"] == "breakpoint" and stop["where"].startswith("reached_milestone")

    gui.call("session.control.pause")
    result = []
    t = threading.Thread(target=lambda: result.append(ai.call("debug.variable.read", name="counter")))
    t.start()
    time.sleep(0.3)
    assert result == []                                          # 一時停止中は AI の操作が保留される
    assert gui.call("debug.variable.read", name="counter")["value"] == 256   # 人間は操作できる（交代）
    gui.call("session.control.resume")
    t.join(5)
    assert result[0]["value"] == 256

    ai.call("debug.breakpoint.clear")
    ai.call("target.reset")
    time.sleep(0.2)
    assert {"target.connect", "debug.run_until_halt", "debug.variable.read"} <= set(seen)
    gui.close(); ai.close()


def test_the_screen_follows_the_target_state(url):
    # 画面（第 3 段）が使う見張り: 止まった場所（関数名・行）と実行中への変化が通知で届き、見張りの読みは記録に残らない
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    states = []
    gui.subscribe(["target.state"], lambda topic, data: states.append(data))
    ai.call("target.connect", target="stm32f401xe", frequency_hz=4_000_000)
    ai.call("target.program_ram", path=str(ELF))
    ai.call("debug.breakpoint.set", location="reached_milestone")
    ai.call("debug.run_until_halt", timeout_s=2)
    deadline = time.monotonic() + 3
    while not any(s.get("where", "").startswith("reached_milestone") for s in states) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert any(s["state"] == "HALTED" and s["where"].startswith("reached_milestone") for s in states)
    ai.call("debug.breakpoint.clear")
    ai.call("target.resume")
    deadline = time.monotonic() + 3
    while states[-1]["state"] != "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert states[-1]["state"] == "RUNNING"
    assert all(e["method"] != "target.status" for e in gui.call("session.activity.list", limit=500)["entries"])
    ai.call("target.reset")
    gui.close(); ai.close()
