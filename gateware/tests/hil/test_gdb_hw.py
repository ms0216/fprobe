"""デーモン内蔵の GDB サーバ（U04）の実機試験: Nucleo F401RE の SRAM で sram_demo・freertos_demo を走らせ、
GDB（RSP のクライアント、あれば arm-none-eabi-gdb）と API の利用者（AI 役）が同じデーモンを同時に使う。"""

import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import uvicorn

from fprobe.api.client import DaemonClient
from fprobe.api.daemon import GdbOptions, create_app
from fprobe.gdb import rsp
from fprobe.gdb.client import RspClient
from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"
SRAM_DEMO = FIRMWARE / "sram_demo" / "sram_demo.elf"
FREERTOS_DEMO = FIRMWARE / "freertos_demo" / "freertos_demo.elf"
ARM_GDB = shutil.which("arm-none-eabi-gdb") or shutil.which("gdb-multiarch")


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    program("fprobe")
    backend = Probe(SERIAL)
    app = create_app(backend, token=None, activity_dir=tmp_path_factory.mktemp("activity"), gdb_port=0,
                     gdb_options=GdbOptions("stm32f401xe", 4_000_000))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    d = app.state.daemon
    while not server.started or d.gdb_port is None:
        time.sleep(0.01)
    yield f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api", d.gdb_port
    server.should_exit = True
    thread.join(10)
    backend.close()


@pytest.fixture
def ai(daemon):
    client = DaemonClient(daemon[0], "claude", "ai")
    yield client
    client.close()


def connect_gdb(port: int) -> RspClient:
    gdb = RspClient("127.0.0.1", port)
    gdb.request("qSupported:swbreak+;hwbreak+;error-message+")
    gdb.no_ack()
    return gdb


def test_gdb_attaches_and_breaks_while_the_ai_records_dap_events(daemon, ai):
    _, port = daemon
    with connect_gdb(port) as gdb:
        assert gdb.request("?")[:1] == "T"                          # --gdb-target で接続して止めた
        root = ET.fromstring(gdb.xfer("features", "target.xml"))
        regs = [r.get("name") for f in root.findall("feature") for r in f.findall("reg")]
        assert "d15" in regs and "basepri" in regs                  # Cortex-M4F
        assert '<memory type="flash" start="0x8000000"' in gdb.xfer("memory-map")
        ai.call("target.program_ram", path=str(SRAM_DEMO))
        address = ai.call("debug.symbol.info", location="reached_milestone")["address"]
        ai.call("events.start")
        assert gdb.request(f"Z0,{address:x},2") == "OK"
        assert any(b["location"].startswith("gdb:") for b in ai.call("debug.breakpoint.list")["breakpoints"])
        reply = gdb.request("vCont;c", timeout_s=5)
        assert reply.startswith("T05") and rsp.le_hex(address, 32) in reply, reply
        events = ai.call("events.read", last=200)
        assert events["items"], "GDB の操作の DAP 転送が記録に無い"
        assert gdb.request(f"z0,{address:x},2") == "OK"
        assert gdb.request("D") == "OK"


def test_monitor_and_the_activity_log(daemon, ai):
    _, port = daemon
    with connect_gdb(port) as gdb:
        gdb.request("?")
        text, reply = gdb.monitor("target.status")
        assert reply == "OK" and "HALTED" in text
        text, reply = gdb.monitor("reset halt")
        assert reply == "OK"
        assert gdb.request("s").startswith("T05")
    methods = [e["method"] for e in ai.call("session.activity.list")["entries"]]
    assert {"gdb.attach", "gdb.reset", "gdb.step", "target.status"} <= set(methods)


def test_freertos_tasks_are_gdb_threads(daemon, ai):
    _, port = daemon
    ai.call("target.program_ram", path=str(FREERTOS_DEMO))
    ai.call("target.resume")
    time.sleep(1.0)
    with connect_gdb(port) as gdb:
        gdb.request("?")                                             # 動いているので止める
        threads = ET.fromstring(gdb.xfer("threads")).findall("thread")
        names = {t.get("name"): int(t.get("id"), 16) for t in threads}
        assert {"ctrl", "sensor", "worker", "IDLE"} <= set(names), names
        assert gdb.request(f"Hg{names['ctrl']:x}") == "OK"
        pc = rsp.from_le_hex(gdb.request("pf"))
        where = ai.call("debug.symbol.info", location=f"0x{pc:08X}")["where"]
        assert "xQueue" in where or "vPort" in where or "ctrl_task" in where, where
        gdb.request("D")


@pytest.mark.skipif(ARM_GDB is None, reason="arm-none-eabi-gdb / gdb-multiarch が無い")
def test_real_gdb_breaks_and_backtraces(daemon, ai):
    _, port = daemon
    ai.call("target.program_ram", path=str(SRAM_DEMO))
    out = subprocess.run([ARM_GDB, "--batch", "-nx", str(SRAM_DEMO), "-ex", "set remotetimeout 10",
                          "-ex", f"target remote 127.0.0.1:{port}", "-ex", f"monitor elf {SRAM_DEMO}",
                          "-ex", "break reached_milestone", "-ex", "continue", "-ex", "bt", "-ex", "info threads",
                          "-ex", "monitor debug.breakpoint.list", "-ex", "detach"],
                         capture_output=True, text=True, timeout=120)
    assert "reached_milestone" in out.stdout and "gdb:0x" in out.stdout, out.stdout + out.stderr
