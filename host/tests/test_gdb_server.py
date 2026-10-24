"""デーモン内蔵の GDB サーバ（fprobe.gdb）: RSP のクライアント（GDB の代わり）と API の利用者（AI・画面の代わり）が
同じデーモンを同時に使う。機器は偽の Cortex-M（gdb_fakes）。arm-none-eabi-gdb があれば本物の GDB でも試す。"""

import shutil
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
import uvicorn
from gdb_fakes import FakeCore, make_probe
from pyocd.core.target import Target
from rtos_kernel import FreeRTOSBuilder, shared

from fprobe import probe_gdb
from fprobe.api.client import DaemonClient
from fprobe.api.daemon import GdbOptions, create_app
from fprobe.gdb import rsp
from fprobe.gdb.client import RspClient
from fprobe.symbols import Symbol


def serve(probe, tmp_path, **kw):
    app = create_app(probe, token=None, activity_dir=tmp_path / "activity", gdb_port=0,
                     gdb_options=GdbOptions(poll_s=0.005), watch_s=0.05, **kw)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    daemon = app.state.daemon
    while not server.started or daemon.gdb_port is None:
        time.sleep(0.01)
    url = f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api"

    def stop():
        server.should_exit = True
        thread.join(5)
    return url, daemon, stop


@pytest.fixture
def core():
    return FakeCore()


@pytest.fixture
def probe(core):
    return make_probe(core)


@pytest.fixture
def daemon(probe, tmp_path):
    url, d, stop = serve(probe, tmp_path)
    yield url, d
    stop()


@pytest.fixture
def gdb(daemon):
    _, d = daemon
    with RspClient("127.0.0.1", d.gdb_port) as client:
        features = client.request("qSupported:multiprocess+;swbreak+;hwbreak+;error-message+;vContSupported+")
        assert "PacketSize=4000" in features and "qXfer:features:read+" in features
        client.no_ack()
        yield client


@pytest.fixture
def ai(daemon):
    url, _ = daemon
    client = DaemonClient(url, "claude", "ai")
    yield client
    client.close()


def wait_for(condition, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "時間切れ"
        time.sleep(0.005)


# --- 始まり・記述 -------------------------------------------------------------------------------

def test_handshake_target_description_and_memory_map(gdb, core):
    core.state = Target.State.RUNNING                             # 動いているターゲットにつなぐと止める
    assert gdb.request("?").startswith("T02")                     # 接続で止めた: SIGINT
    assert core.state == Target.State.HALTED
    root = ET.fromstring(gdb.xfer("features", "target.xml"))
    regs = [r.get("name") for f in root.findall("feature") for r in f.findall("reg")]
    assert regs[15] == "pc" and "d15" in regs and "basepri" in regs
    memory = ET.fromstring(gdb.xfer("memory-map").split("\n", 2)[2]).findall("memory")
    flash = [m for m in memory if m.get("type") == "flash"]
    assert flash[0].get("start") == "0x8000000" and memory[-1].get("length") is not None
    assert gdb.request("qAttached") == "1" and gdb.request("vMustReplyEmpty") == ""


def test_registers_and_memory(gdb, core):
    core.registers.update(r0=0x1234_5678, s0=1, s1=2)
    regs = gdb.request("g")
    assert regs[:8] == "78563412"
    assert regs[15 * 8:16 * 8] == rsp.le_hex(0x0800_0100, 32)
    assert gdb.request("p11") == rsp.le_hex(0, 32)                 # msp（17 番）
    d0 = 23                                                      # r0..xpsr（17）+ m-system（6）
    assert gdb.request(f"p{d0:x}") == rsp.le_hex(2 << 32 | 1, 64)
    assert gdb.request(f"P1={rsp.le_hex(0xCAFE, 32)}") == "OK" and core.registers["r1"] == 0xCAFE
    regs = gdb.request("g")
    changed = rsp.le_hex(0xBEEF, 32) + regs[8:]
    assert gdb.request("G" + changed) == "OK" and core.registers["r0"] == 0xBEEF
    assert gdb.request("M20000000,3:010203") == "OK"
    assert gdb.request(b"X20000003,2:#}") == "OK"                 # バイナリ（線上では逃がされる # と }）
    assert gdb.read_memory(0x2000_0000, 5) == b"\x01\x02\x03#}"
    assert gdb.request("X20000000,0:") == "OK"                   # X の対応の確かめ
    assert gdb.request("p99").startswith("E.")                   # 無いレジスタ: 理由つきの誤り


def test_bad_checksum_is_nacked(daemon):
    _, d = daemon
    with socket.create_connection(("127.0.0.1", d.gdb_port), timeout=5) as s:
        s.sendall(b"$g#00")
        assert s.recv(1) == b"-"


# --- ブレークポイントと実行 -----------------------------------------------------------------------

def test_gdb_breakpoints_are_seen_by_the_ai_and_the_stop_reaches_gdb(gdb, ai, core):
    assert gdb.request("Z0,8000200,2") == "OK"
    listed = ai.call("debug.breakpoint.list")["breakpoints"]
    assert listed[0]["location"] == "gdb:0x08000200"
    gdb.send("vCont;c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    core.hit(0x0800_0200)
    reply = gdb.receive(5).decode()
    assert reply.startswith("T05") and "hwbreak:;" in reply and "thread:1;" in reply
    assert f"0f:{rsp.le_hex(0x0800_0200, 32)};" in reply
    assert gdb.request("z0,8000200,2") == "OK"
    assert ai.call("debug.breakpoint.list")["breakpoints"] == []


def test_interrupt_and_a_halt_by_another_user(gdb, ai, core):
    gdb.request("?")
    gdb.send("c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    gdb.interrupt()
    assert gdb.receive(5).decode().startswith("T02")
    gdb.send("c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    ai.call("target.halt")                                          # AI が止めても GDB に停止が届く
    assert gdb.receive(5).decode().startswith("T02")


def test_step_and_watchpoint_stop(gdb, core):
    gdb.request("?")
    assert gdb.request("s").startswith("T05") and core.registers["pc"] == 0x0800_0102
    assert gdb.request("Z2,20000400,4") == "OK"
    gdb.send("c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    core.write32(probe_gdb.DWT_CTRL, 4 << 28)                     # DWT の比較器 0 が成立した（MATCHED）
    core.write32(probe_gdb.DWT_COMP0, 0x2000_0400)
    core.write32(probe_gdb.DWT_FUNCTION0, probe_gdb.DWT_MATCHED | 6)
    core.hit(0x0800_0300, Target.HaltReason.WATCHPOINT)
    assert "watch:20000400;" in gdb.receive(5).decode()
    assert gdb.request("Z3,20000401,3").startswith("E.")           # DWT が取れない大きさ


def test_semihosting_output_goes_to_the_console_and_exit_is_W(gdb, probe, core):
    gdb.request("?")
    gdb.send("c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    probe._received("semihosting", "こんにちは\n".encode(), 0.0)
    probe._sh_exit = {"code": 1, "reason": "ApplicationExit"}
    core.hit(0x0800_0400)
    assert gdb.receive(5) == b"W01"
    assert b"".join(gdb.console).decode() == "こんにちは\n"


def test_gdb_operations_are_recorded_and_gdb_is_a_client(gdb, ai, core):
    gdb.request("?")
    gdb.request("s")
    gdb.request("M20000000,1:ff")
    status = ai.call("session.status")
    assert any(c["via"] == "gdb" and c["role"] == "human" for c in status["clients"])
    assert status["gdb"]["port"] > 0
    methods = [e["method"] for e in ai.call("session.activity.list")["entries"]]
    assert {"gdb.attach", "gdb.step", "gdb.memory.write"} <= set(methods)
    assert "gdb.breakpoint" not in " ".join(methods)


def test_detach_releases_breakpoints_and_resumes(daemon, gdb, core):
    _, d = daemon
    gdb.request("?")
    gdb.request("Z0,8000200,2")
    gdb.request("Z1,20000100,2")
    assert gdb.request("D") == "OK"
    wait_for(lambda: core.state == Target.State.RUNNING)
    assert core.breakpoints == {}
    wait_for(lambda: not d.guests)                                 # 利用者の一覧から消える


def test_disconnect_releases_breakpoints_and_leaves_the_target(daemon, core):
    _, d = daemon
    with RspClient("127.0.0.1", d.gdb_port) as client:
        client.request("?")
        client.request("Z0,8000200,2")
    wait_for(lambda: core.breakpoints == {})
    assert core.state == Target.State.HALTED


# --- monitor ------------------------------------------------------------------------------------

def test_monitor_calls_the_api(gdb, core):
    text, reply = gdb.monitor("help")
    assert reply == "OK" and "monitor reset" in text
    text, reply = gdb.monitor("target.status")
    assert reply == "OK" and '"state": "HALTED"' in text
    text, reply = gdb.monitor('debug.breakpoint.set location=0x08000500')
    assert reply == "OK" and 0x0800_0500 in core.breakpoints
    text, reply = gdb.monitor('debug.breakpoint.clear {"location": "0x08000500"}')
    assert reply == "OK" and 0x0800_0500 not in core.breakpoints
    _, reply = gdb.monitor("no.such.method")
    assert reply.startswith("E.") and "no.such.method" in reply
    text, reply = gdb.monitor("reset halt")
    assert reply == "OK" and core.resets == [True]


def test_monitor_pause_holds_the_ai(gdb, ai, core):
    _, reply = gdb.monitor("pause")
    assert reply == "OK"
    done = []
    t = threading.Thread(target=lambda: done.append(ai.call("target.halt")))
    t.start()
    time.sleep(0.2)
    assert done == []                                              # GDB（人）が止めている間、AI の操作は保留
    assert gdb.request("s").startswith("T05")                      # GDB（人）は通る
    gdb.monitor("resume")
    t.join(5)
    assert done


def test_gdb_with_the_ai_role_is_held_while_a_human_pauses(probe, core, tmp_path):
    url, d, stop = serve(probe, tmp_path, gdb_role="ai")
    try:
        human = DaemonClient(url, "gui", "human")
        with RspClient("127.0.0.1", d.gdb_port) as client:
            client.request("?")
            human.call("session.control.pause")
            assert client.read_memory(0x2000_0000, 1) == b"\x00"   # 読みは通る
            client.send("M20000000,1:aa")
            time.sleep(0.2)
            assert core.memory.read_memory(0x2000_0000, 1) == b"\x00"
            human.call("session.control.resume")
            assert client.receive(5) == b"OK"
        human.close()
    finally:
        stop()


# --- Flash ----------------------------------------------------------------------------------------

def test_load_writes_flash_through_the_memory_loader(monkeypatch, gdb):
    written = []

    class Loader:
        def __init__(self, session, keep_unwritten=True):
            pass

        def add_data(self, address, data):
            written.append((address, bytes(data)))

        def commit(self):
            pass

    monkeypatch.setattr(probe_gdb, "MemoryLoader", Loader)
    gdb.request("?")
    assert gdb.request("vFlashErase:08000000,4000") == "OK"
    assert gdb.request(b"vFlashWrite:8000000:\x01\x02") == "OK"
    assert gdb.request(b"vFlashWrite:8000002:#") == "OK"           # 続きの番地はまとめる（線上では逃がされる）
    assert gdb.request(b"vFlashWrite:8001000:\xff") == "OK"
    assert gdb.request("vFlashDone") == "OK"
    assert written == [(0x0800_0000, b"\x01\x02#"), (0x0800_1000, b"\xff")]
    assert "wrote 4 bytes" in b"".join(gdb.console).decode()


# --- トレースポイント（U48） ---------------------------------------------------------------------------

TP = 0x0800_0200
RAM = 0x2000_0000


def define_tracepoint(gdb, *more):
    """1 番: r0 と pc、RAM の 4 バイトを集める。"""
    assert gdb.request("QTinit") == "OK"
    assert gdb.request(f"QTDP:1:{TP:x}:E:0:0-") == "OK"
    assert gdb.request(f"QTDP:-1:{TP:x}:R8001-") == "OK"
    assert gdb.request(f"QTDP:-1:{TP:x}:MFFFFFFFF,{RAM:x},4") == "OK"
    for packet in more:
        assert gdb.request(packet) == "OK"


def hit_and_continue(core, pc=TP):
    """走っているコアが pc のブレークポイントで止まり、集めて続けるのを待つ。"""
    resumes = core.resumes
    core.hit(pc)
    wait_for(lambda: core.state == Target.State.RUNNING and core.resumes > resumes)


def test_tracepoints_collect_at_a_brief_halt_and_gdb_reads_the_frames(gdb, ai, core):
    features = gdb.request("qSupported:error-message+")
    assert "ConditionalTracepoints+" in features and "qXfer:traceframe-info:read+" in features
    assert gdb.request("qTStatus").startswith("T0;tnotrun:0;")
    gdb.request("?")
    define_tracepoint(gdb, "QTDV:1:0:0:" + b"n".hex())
    assert gdb.request("QTStart") == "OK"
    assert gdb.request("qTStatus").startswith("T1;tunknown:0;tframes:0;")
    listed = ai.call("debug.breakpoint.list")["breakpoints"]
    assert listed[0]["location"] == f"gdb-trace:0x{TP:08X}" and listed[0]["tool"] == "gdb.trace"
    gdb.send("c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    for value in (0x11, 0x22):                                     # GDB には止まりを返さず、集めて続ける
        core.memory.write_memory(RAM, bytes([value, 0, 0, 0]))
        core.registers["r0"] = value
        hit_and_continue(core)
    gdb.interrupt()
    assert gdb.receive(5).decode().startswith("T02")
    assert gdb.request("qTStatus").startswith("T1;tunknown:0;tframes:2;tcreated:2;")
    assert gdb.request(f"qTP:1:{TP:x}").startswith("V2:")
    assert gdb.request("QTStop") == "OK" and TP not in core.breakpoints
    assert gdb.request("qTStatus").startswith("T0;tstop::0;tframes:2;")
    # フレームを見る: 集めたレジスタ・メモリだけ。ほかは見えない（x・誤り）
    assert gdb.request("QTFrame:0") == "F0T1"
    regs = gdb.request("g")
    assert rsp.from_le_hex(regs[:8]) == 0x11 and regs[8:16] == "x" * 8
    assert rsp.from_le_hex(regs[15 * 8:16 * 8]) == TP
    assert gdb.read_memory(RAM, 4) == b"\x11\0\0\0"
    assert gdb.request(f"m{RAM + 0x100:x},4").startswith("E")
    assert gdb.request("M20000000,1:ff").startswith("E.")            # フレームを見ている間は書けない
    info = gdb.xfer("traceframe-info")
    assert f'<memory start="0x{RAM:x}" length="0x4"/>' in info
    assert gdb.request(f"QTFrame:pc:{TP:x}") == "F1T1" and gdb.read_memory(RAM, 4) == b"\x22\0\0\0"
    assert gdb.request(f"QTFrame:pc:{TP:x}") == "F-1"
    assert gdb.request("QTFrame:ffffffff") == "OK"
    assert gdb.read_memory(RAM, 4) == b"\x22\0\0\0" and rsp.from_le_hex(gdb.request("g")[8:16]) == 0   # 実物
    # アップロード（GDB がつなぎ直したとき）
    lines = [gdb.request("qTfP")]
    while lines[-1] != "l":
        lines.append(gdb.request("qTsP"))
    assert lines == [f"T1:{TP:x}:E:0:0", f"A1:{TP:x}:R8001", f"A1:{TP:x}:MFFFFFFFF,{RAM:x},4", "l"]
    assert gdb.request("qTfV") == "1:0:0:" + b"n".hex() and gdb.request("qTsV") == "l"
    methods = [e["method"] for e in ai.call("session.activity.list")["entries"]]
    assert {"gdb.trace.start", "gdb.trace.stop"} <= set(methods)


def test_a_gdb_breakpoint_at_a_tracepoint_still_stops(gdb, core):
    gdb.request("?")
    define_tracepoint(gdb)
    assert gdb.request("QTStart") == "OK"
    assert gdb.request(f"Z0,{TP:x},2") == "OK"                       # 同じ番地の GDB のブレークポイント
    gdb.send("c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    core.hit(TP)
    assert gdb.receive(5).decode().startswith("T05")                 # 集めて、止める
    assert "tframes:1;" in gdb.request("qTStatus")
    assert gdb.request(f"z0,{TP:x},2") == "OK" and TP in core.breakpoints   # トレースポイントの分は残る
    assert gdb.request("QTStop") == "OK" and TP not in core.breakpoints


def test_pass_count_stops_the_trace_and_frees_the_breakpoint(gdb, core):
    gdb.request("?")
    assert gdb.request("QTinit") == "OK"
    assert gdb.request(f"QTDP:2:{TP:x}:E:0:2-") == "OK"
    assert gdb.request(f"QTDP:-2:{TP:x}:R1") == "OK"
    assert gdb.request("QTStart") == "OK"
    gdb.send("c")
    wait_for(lambda: core.state == Target.State.RUNNING)
    hit_and_continue(core)
    hit_and_continue(core)
    wait_for(lambda: TP not in core.breakpoints)
    gdb.interrupt()
    gdb.receive(5)
    assert gdb.request("qTStatus").startswith("T0;tpasscount:2;tframes:2;")


def test_clearing_all_breakpoints_stops_the_trace_with_a_reason(gdb, ai, core):
    gdb.request("qSupported:error-message+")
    gdb.request("?")
    define_tracepoint(gdb)
    assert gdb.request("QTStart") == "OK"
    ai.call("debug.breakpoint.clear")
    status = gdb.request("qTStatus")
    assert status.startswith("T0;terror:") and TP not in core.breakpoints
    assert "breakpoint_clear" in bytes.fromhex(status.split(";")[1].split(":")[1]).decode()
    assert ai.call("debug.breakpoint.list")["breakpoints"] == []


def test_names_in_the_elf_do_not_refuse_tracepoints_by_default(probe, core, daemon):
    # 名前の一致（halt_risk）だけを根拠に既定で断らない: 断るかは使う側が monitor trace deny-halt で明示する
    _, d = daemon
    probe._symbols = SimpleNamespace(names=lambda: [Symbol("RADIO_IRQHandler", 0x100, 4, "function")],
                                     path="radio.elf")
    with RspClient("127.0.0.1", d.gdb_port) as gdb:
        gdb.request("qSupported:error-message+")
        gdb.request("?")
        define_tracepoint(gdb)
        assert gdb.request("QTStart") == "OK" and TP in core.breakpoints
        gdb.request("QTStop")


def test_deny_halt_given_by_the_user_refuses_tracepoints(probe, core, daemon):
    _, d = daemon
    with RspClient("127.0.0.1", d.gdb_port) as gdb:
        gdb.request("qSupported:error-message+")
        gdb.request("?")
        define_tracepoint(gdb)
        text, reply = gdb.monitor("trace deny-halt")
        assert reply == "OK" and '"deny_halt": true' in text
        reply = gdb.request("QTStart")
        assert reply.startswith("E.") and "deny-halt" in b"".join(gdb.console).decode()
        assert TP not in core.breakpoints and gdb.request("qTStatus").startswith("T0;")
        text, reply = gdb.monitor("trace allow-halt")
        assert reply == "OK" and '"deny_halt": false' in text
        assert gdb.request("QTStart") == "OK" and TP in core.breakpoints
        text, _ = gdb.monitor("trace")
        assert '"running": true' in text
        gdb.request("QTStop")


def test_disconnected_tracing_keeps_running_only_when_asked(daemon, core):
    _, d = daemon
    with RspClient("127.0.0.1", d.gdb_port) as gdb:
        gdb.request("?")
        define_tracepoint(gdb)
        gdb.request("QTStart")
    wait_for(lambda: TP not in core.breakpoints)                      # 切断でトレースは止まる
    with RspClient("127.0.0.1", d.gdb_port) as gdb:
        assert gdb.request("qTStatus").startswith("T0;tdisconnected:0;")
        gdb.request("?")
        define_tracepoint(gdb, "QTDisconnected:1")
        gdb.request("QTStart")
        gdb.send("c")
        wait_for(lambda: core.state == Target.State.RUNNING)
    time.sleep(0.1)
    assert TP in core.breakpoints                                     # GDB が切れても続ける
    hit_and_continue(core)
    with RspClient("127.0.0.1", d.gdb_port) as gdb:                   # つなぎ直した GDB が見つける
        status = gdb.request("qTStatus")
        assert status.startswith("T1;tunknown:0;tframes:1;") and "disconn:1" in status
        assert gdb.request("qTfP") == f"T1:{TP:x}:E:0:0"
        gdb.request("?")
        assert gdb.request("QTStop") == "OK"


# --- RTOS のスレッド ----------------------------------------------------------------------------------

@pytest.fixture
def rtos_daemon(tmp_path):
    k = FreeRTOSBuilder()
    h = {name: k.task(name, prio) for name, prio in [("ctrl", 3), ("sensor", 2), ("IDLE", 0)]}
    k.put_state(h["ctrl"], k.var("xSuspendedTaskList"))
    k.put_state(h["sensor"], k.ready(2))
    k.put_state(h["IDLE"], k.ready(0))
    k.set_current(h["sensor"])
    k.initial_context(h["ctrl"], "ctrl_task", 0x1234)
    core = FakeCore(memory=k.memory)
    core.registers.update(r0=99)
    url, d, stop = serve(make_probe(core, shared()[1]), tmp_path)
    yield k, h, d
    stop()


def test_rtos_tasks_are_gdb_threads(rtos_daemon):
    k, h, d = rtos_daemon
    with RspClient("127.0.0.1", d.gdb_port) as gdb:
        gdb.request("qSupported:swbreak+")
        assert f"thread:{h['sensor']:x};" in gdb.request("?")
        ids = gdb.request("qfThreadInfo")[1:].split(",")
        assert sorted(int(i, 16) for i in ids) == sorted(h.values()) and gdb.request("qsThreadInfo") == "l"
        assert gdb.request("qC") == f"QC{h['sensor']:x}"
        threads = ET.fromstring(gdb.xfer("threads"))
        names = {t.get("name"): t.text for t in threads.findall("thread")}
        assert names["ctrl"] == "suspended, priority 3"
        assert bytes.fromhex(gdb.request(f"qThreadExtraInfo,{h['IDLE']:x}")).decode() == "ready, priority 0"
        assert gdb.request(f"T{h['ctrl']:x}") == "OK" and gdb.request("T1").startswith("E")
        assert gdb.request(f"Hg{h['ctrl']:x}") == "OK"
        regs = gdb.request("g")
        assert rsp.from_le_hex(regs[:8]) == 0x1234
        assert rsp.from_le_hex(regs[15 * 8:16 * 8]) == k.symbols.symbol("ctrl_task").address
        assert regs[17 * 8:18 * 8] == "x" * 8                       # msp は保存された文脈に無い
        assert gdb.request(f"P0={rsp.le_hex(1, 32)}").startswith("E")   # 止まっているタスクは書けない
        assert gdb.request(f"Hg{h['sensor']:x}") == "OK"
        assert rsp.from_le_hex(gdb.request("g")[:8]) == 99          # 実行中のタスクは実レジスタ
        text, _ = gdb.monitor("threads off")
        assert "threads off: 1" in text and gdb.request("qfThreadInfo") == "m1"


def test_without_a_current_task_the_core_is_its_own_thread(tmp_path):
    """実行中のタスクが無い（スケジューラの前等）とき、先頭のタスクを「いま」としない: コアそのもののスレッド
    （ID 1、実レジスタ）を足していまのスレッドにする。"""
    k = FreeRTOSBuilder()
    h = {name: k.task(name, prio) for name, prio in [("ctrl", 3), ("IDLE", 0)]}
    k.put_state(h["ctrl"], k.ready(3))
    k.put_state(h["IDLE"], k.ready(0))
    k.set_current(0)
    k.initial_context(h["ctrl"], "ctrl_task", 0x1234)
    core = FakeCore(memory=k.memory)
    core.registers.update(r0=99)
    url, d, stop = serve(make_probe(core, shared()[1]), tmp_path)
    try:
        with RspClient("127.0.0.1", d.gdb_port) as gdb:
            gdb.request("qSupported:swbreak+")
            assert gdb.request("qC") == "QC1"
            ids = sorted(int(i, 16) for i in gdb.request("qfThreadInfo")[1:].split(","))
            assert ids == sorted([1, *h.values()])
            assert bytes.fromhex(gdb.request("qThreadExtraInfo,1")).decode() == "no current task"
            assert rsp.from_le_hex(gdb.request("g")[:8]) == 99              # コアの実レジスタ
    finally:
        stop()


# --- 本物の GDB（あれば） -------------------------------------------------------------------------------

ARM_GDB = shutil.which("arm-none-eabi-gdb") or shutil.which("gdb-multiarch")


@pytest.mark.skipif(ARM_GDB is None, reason="arm-none-eabi-gdb / gdb-multiarch が無い")
def test_real_gdb_reads_registers_and_memory(daemon, core):
    _, d = daemon
    core.memory.write_memory(0x2000_0000, b"\x78\x56\x34\x12")
    out = subprocess.run([ARM_GDB, "--batch", "-nx", "-ex", f"target remote 127.0.0.1:{d.gdb_port}",
                          "-ex", "info registers pc", "-ex", "x/1xw 0x20000000", "-ex", "monitor target.status",
                          "-ex", "detach"], capture_output=True, text=True, timeout=60)
    assert "0x8000100" in out.stdout and "0x12345678" in out.stdout and "HALTED" in out.stdout, out.stdout + out.stderr
