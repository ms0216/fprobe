"""API の linux.halt.*（JTAG で全コアを止めて Linux のアプリをデバッグする）を、本物のデーモンと Probe、偽のコア（SimCores）で試す。
機器は開かない: バックエンドを開く所（service.open_backend）とオフセットの読み込みを差し替える。"""
import socket
import time

import pytest

from fprobe.api import E_DEVICE, E_INVALID_PARAMS
from fprobe.api.client import ApiError, DaemonClient
from fprobe.api.spec import load_spec
from fprobe.gdb import rsp as framing
from fprobe.linux_halt import service
from fprobe.linux_halt.core_access import CoreState, MOE_HALT
from fprobe.mcp_server import Probe
from linux_halt.test_engine import EXTRA, STACK
from linux_halt.test_kernel import layout
from linux_halt.test_service import Rig
from test_api_daemon import _serve

OPEN = dict(backend="fprobe", jtag_khz=2000, offsets="offsets.json", page_offset=0xC0000000, phys_offset=0,
            linear_map_end=0xC0100000, max_tasks=100, max_threads=16, mmu_format="short", fpga_request_times=False,
            part="zynq7000")


class FakeSession:
    """Probe.session の代わり（機器を開かない）。dap_access は呼ばれたことだけを数える。"""
    def __init__(self):
        self.device = object()
        self.dap_opened = 0

    def dap_access(self):
        self.dap_opened += 1
        return object()

    def close(self):
        pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    world = Rig(tmp_path)
    opened = []

    def fake_open_backend(o, lay, *, dap_link, device):
        dap_link()                                                    # 実物と同じく、開くときにプローブの DAP の口を開く
        opened.append(o)
        # 開いた事実は本物（service.open_backend の fprobe の道）と同じ欄（part も。応答の形は仕様の照合が確かめる）
        return world.cores, world._close, (RuntimeError,), {"backend": o.backend, "part": o.part, "n_cores": 4, "hw_slots": 2,
                                                             "watch_slots": 2, "debug_ap": 0, "discovery": "ahb_system_map",
                                                             "cti": None, "fpga_halt_watch": None, "fpga_request_times": None}
    monkeypatch.setattr(service, "open_backend", fake_open_backend)
    monkeypatch.setattr(service, "load_layout_json", lambda path, **kw: layout(**EXTRA))
    probe = Probe("unused")
    probe._session = FakeSession()
    probe._ensure_design = lambda protocol: None
    url, app, stop = _serve(probe, tmp_path)
    ai, gui = DaemonClient(url, "claude", "ai"), DaemonClient(url, "gui", "human")
    yield world, probe, ai, gui, dict(OPEN, journal=str(tmp_path / "j.json")), opened
    ai.close(), gui.close()
    stop()
    probe._close_linux_halt()


def test_the_spec_has_every_linux_halt_method_with_the_cli_arguments():
    spec = load_spec()
    names = {n for n in spec.methods if n.startswith("linux.halt.")}
    assert names == {"linux.halt." + n for n in ("open", "close", "status", "halt_all", "resume_all", "processes", "threads",
                                                 "recover", "rescue", "gdb.serve", "gdb.status", "gdb.stop",
                                                 "debug.attach", "debug.status", "debug.break.insert",
                                                 "debug.break.remove", "debug.run", "debug.halt", "debug.threads",
                                                 "debug.read", "debug.memory", "debug.step", "debug.select", "debug.close",
                                                 "profile.save", "profile.load")}
    from fprobe.linux_halt.cli import build_parser
    import dataclasses
    fields = {f.name for f in dataclasses.fields(service.HaltOptions)}
    assert set(spec.methods["linux.halt.open"].params["properties"]) == fields          # CLI の共通の引数（HaltOptions）と一対一
    common = {a.dest for a in build_parser()._subparsers._group_actions[0].choices["ps"]._actions} - {"help"}
    assert fields == common - {"probe_serial", "probe_eth", "probe_eth_link_up_wait"}  # 機器の選び方は CLI だけ（デーモンは起動時に握る）
    serve = set(spec.methods["linux.halt.gdb.serve"].params["properties"])
    attach = {a.dest for a in build_parser()._subparsers._group_actions[0].choices["attach"]._actions} - common - {"help"}
    assert serve == attach - {"verbose"}                                               # -v は端末への表示だけ
    from fprobe.plugins.openocd_boards import BOARDS
    assert spec.methods["linux.halt.open"].params["properties"]["ocd_board"]["enum"] == sorted(BOARDS)


def test_the_profile_saves_and_loads_the_open_and_attach_settings_without_the_pid(rig, tmp_path):
    world, probe, ai, gui, args, opened = rig
    offsets = tmp_path / "offsets.json"
    offsets.write_text("{}", encoding="utf-8")
    args = dict(args, offsets=str(offsets))
    ai.call("linux.halt.open", **args)
    path = str(tmp_path / "sub" / "p.json")
    saved = ai.call("linux.halt.profile.save", path=path)
    assert saved["open"] == {k: v for k, v in args.items()} and saved["attach"] is None
    loaded = gui.call("linux.halt.profile.load", path=path)                  # 人の画面も同じ応答を読む
    assert loaded["open"] == saved["open"] and loaded["offsets_matches"] is True
    offsets.write_text('{"changed": 1}', encoding="utf-8")                     # offsets が変わった事実
    assert gui.call("linux.halt.profile.load", path=path)["offsets_matches"] is False
    offsets.unlink()
    assert gui.call("linux.halt.profile.load", path=path)["offsets_matches"] is None
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "x", "version": 1}', encoding="utf-8")
    with pytest.raises(ApiError, match="fprobe.linux_halt.profile"):
        ai.call("linux.halt.profile.load", path=str(bad))


def test_no_option_has_a_default_and_the_required_ones_are_refused_when_missing(rig):
    _, _, ai, _, opts, _ = rig
    for key in ("backend", "jtag_khz", "journal", "fpga_request_times"):
        with pytest.raises(ApiError) as e:
            ai.call("linux.halt.open", **{k: v for k, v in opts.items() if k != key})
        assert e.value.code == E_INVALID_PARAMS
    with pytest.raises(ApiError, match="CTI needs every one"):
        ai.call("linux.halt.open", **opts, cti_board="zynq7000")


def test_open_occupies_the_probe_dap_and_the_other_dap_users_are_refused(rig):
    world, probe, ai, gui, opts, opened = rig
    r = ai.call("linux.halt.open", **opts)
    assert r["occupies"]["probe_dap"] is True and r["opened_by"]["name"] == "claude"
    assert r["journal"] == {"path": opts["journal"], "exists": False, "entries": []}
    assert probe._session.dap_opened == 1
    for method, params in (("target.connect", {"target": "zynq7000", "frequency_hz": 4_000_000}), ("probe.pins.read", {})):
        with pytest.raises(ApiError, match="linux.halt が使っている") as e:
            gui.call(method, **params)
        assert e.value.code == E_DEVICE
    with pytest.raises(ApiError, match="もう開いている"):
        gui.call("linux.halt.open", **opts)
    assert gui.call("linux.halt.close")["closed"] is True
    assert gui.call("linux.halt.status") == {"open": False}


def test_the_human_and_the_ai_see_the_same_facts(rig):
    world, _, ai, gui, opts, _ = rig
    ai.call("linux.halt.open", **opts)
    stop = gui.call("linux.halt.halt_all")["stop"]
    assert stop == {"method": "simultaneous", "cores_halted": [0, 1, 2, 3], "skew_upper_bound_ns": 50, "basis": "simulated",
                    "assumptions": [], "bound_without_assumptions_ns": None}
    a, g = ai.call("linux.halt.status"), gui.call("linux.halt.status")
    assert a == g and [c["running"] for c in a["cores"]] == [False] * 4
    procs = ai.call("linux.halt.processes")
    assert procs["stop"] is None and procs["resumed"] is False
    assert [p["tgid"] for p in procs["processes"]] == [100, 200]
    assert gui.call("linux.halt.threads", pid=100)["threads"][0]["tid"] == 100
    gui.call("linux.halt.resume_all")
    assert not any(world.cores.halted())


def test_gdb_serve_runs_in_the_daemon_and_the_api_can_stop_it(rig):
    world, _, ai, gui, opts, _ = rig
    ai.call("linux.halt.open", **opts)
    for c in range(4):
        world.cores.states[c] = CoreState(False, pc=0x10000, cpsr=0x10, moe=MOE_HALT, sp_svc=STACK[100] + 8000)
    r = gui.call("linux.halt.gdb.serve", pid=100, port=0, skew_limit_ns=68, thread_id="stack", kernel_isa="arm",
                 max_step_retries=8, bp_unmapped="error")
    assert r["state"] == "listening" and r["connect"].startswith("target remote 127.0.0.1:")
    with socket.create_connection(("127.0.0.1", r["port"])) as g:
        g.sendall(framing.frame(b"?"))
        g.settimeout(5)
        reader, got = framing.PacketReader(), None
        while got is None:
            got = next((e.payload for e in reader.feed(g.recv(65536)) if isinstance(e, framing.Packet)), None)
        assert got.startswith(b"T05")
        deadline = time.monotonic() + 5
        while ai.call("linux.halt.gdb.status")["gdb"]["state"] != "serving" and time.monotonic() < deadline:
            time.sleep(0.02)
        with pytest.raises(ApiError, match="gdb server"):
            ai.call("linux.halt.halt_all")
        assert ai.call("linux.halt.status")["cores"] is None
        out = ai.call("linux.halt.gdb.stop")["gdb"]
    assert out["state"] == "ended" and out["ended_by"] == "stop" and out["cleanup_errors"] == []
    assert not world.cores.hw_bps and not any(world.cores.halted())


def test_the_daemon_closing_the_probe_cleans_up_a_running_gdb_server(rig):
    world, probe, ai, _, opts, _ = rig
    ai.call("linux.halt.open", **opts)
    ai.call("linux.halt.gdb.serve", pid=100, port=0, skew_limit_ns=68, thread_id="stack", kernel_isa="arm",
            max_step_retries=8, bp_unmapped="error")
    probe._release_device()                                                  # デーモンの終了・USB の開き直しと同じ道
    assert probe._svc_linux_halt._halt_session is None and world.closed == 1
    assert not any(world.cores.halted())


# --- linux.halt.debug.*: AI が JTAG の全コア停止で止めて調べる（gdb を使わない）。本物のデーモンと Probe、2 スレッドの競合の模型 --

@pytest.fixture
def race(tmp_path, monkeypatch):
    from linux_halt.race_world import RaceWorld
    world = RaceWorld(tmp_path)

    def fake_open_backend(o, lay, *, dap_link, device):
        dap_link()
        return world.cores, world._close, (RuntimeError,), {"backend": o.backend, "part": o.part, "n_cores": 4,
                                                             "hw_slots": 2, "watch_slots": 2, "debug_ap": 0,
                                                             "discovery": "ahb_system_map", "cti": None,
                                                             "fpga_halt_watch": None, "fpga_request_times": None}
    monkeypatch.setattr(service, "open_backend", fake_open_backend)
    monkeypatch.setattr(service, "load_layout_json", lambda path, **kw: layout(**EXTRA))
    probe = Probe("unused")
    probe._session = FakeSession()
    probe._ensure_design = lambda protocol: None
    url, app, stop = _serve(probe, tmp_path)
    ai, gui = DaemonClient(url, "claude", "ai"), DaemonClient(url, "gui", "human")
    ai.call("linux.halt.open", **dict(OPEN, journal=str(tmp_path / "j.json")))
    yield world, probe, ai, gui
    ai.close(), gui.close()
    stop()
    probe._close_linux_halt()


def test_the_ai_stops_every_core_at_a_line_and_reads_both_threads_at_the_same_moment_through_the_api(race):
    """端から端（API）: 行にブレークポイント → 当たる → 全コア停止 → 両スレッドの行と変数を 1 回で読む → 再開 → 閉じる。"""
    from linux_halt.race_world import BIAS, ELF, PID, T_LEADER, T_OTHER
    world, _, ai, gui = race
    original = world.read_user(world.addr("race.c:11"), 4)
    a = ai.call("linux.halt.debug.attach", pid=PID, elf=str(ELF), skew_limit_ns=68, thread_id="stack", kernel_isa="arm",
                max_step_retries=8, bp_unmapped="error", max_halt_s=60.0)
    assert a["image"]["load_bias"] == BIAS and a["running"] is False and a["fetched"] is None
    bp = ai.call("linux.halt.debug.break.insert", location="race.c:11", kind="sw")
    assert (bp["function"], bp["line"], bp["kind"]) == ("add_one", 11, "sw")
    world.hit(core=1)
    stop = ai.call("linux.halt.debug.run", wait_s=5.0)
    assert (stop["kind"], stop["tid"], stop["line"], stop["breakpoint"]) == ("swbreak", T_OTHER, 11, bp["id"])
    assert stop["stop"]["cores_halted"] == [0, 1, 2, 3] and stop["stop"]["skew_verdict"] == "within"
    t = ai.call("linux.halt.debug.threads", variables=True, expressions=["counter"], max_elements=16)
    rows = {r["tid"]: r for r in t["threads"]}
    assert (rows[T_OTHER]["line"], rows[T_LEADER]["line"]) == (11, 10)
    assert {v["name"]: v["value"]["value"] for v in rows[T_LEADER]["locals"]}["before"] == 41
    assert {v["name"]: v["value"]["value"] for v in rows[T_OTHER]["locals"]} == {"before": 41, "after": 42}
    assert all(r["expressions"][0]["value"]["value"] == 41 for r in rows.values())
    assert gui.call("linux.halt.debug.threads", variables=True, expressions=["counter"], max_elements=16) == t  # 人と AI が同じ事実
    r = ai.call("linux.halt.debug.read", expressions=["i", "bogus"], tid=T_OTHER, frame=1, max_elements=16)
    assert r["frame"]["function"] == "worker" and r["values"][0]["value"]["value"] == 7 and "error" in r["values"][1]
    m = ai.call("linux.halt.debug.memory", address=world.addr("race.c:11"), length=4)
    assert world.read_user(world.addr("race.c:11"), 4) != original                 # 対象のメモリには BKPT がある
    assert bytes.fromhex(m["data"]) == original                                   # 読みは BKPT ではなく元の命令を見せる
    st = ai.call("linux.halt.debug.status")
    assert st["breakpoints"][0]["id"] == bp["id"] and st["last_stop"]["kind"] == "swbreak"
    assert ai.call("linux.halt.status")["debug"] == {"pid": PID, "comm": "race", "closed": False, "running": False,
                                                     "breakpoints": 1}
    with pytest.raises(ApiError, match="linux.halt.debug.close"):
        gui.call("linux.halt.halt_all")
    world.cores.on_step = lambda core: None
    s = ai.call("linux.halt.debug.step", tid=T_OTHER, mode="over", max_instructions=2, timeout_s=5.0)
    assert s["kind"] == "step" and s["reached_new_line"] is False
    assert ai.call("linux.halt.debug.break.remove", id=bp["id"])["removed"]["id"] == bp["id"]
    assert ai.call("linux.halt.debug.run", wait_s=0)["kind"] == "timeout" and not any(world.cores.halted())
    h = ai.call("linux.halt.debug.halt")
    assert h["kind"] == "int" and all(world.cores.halted())
    closed = ai.call("linux.halt.debug.close")
    assert closed["closed"] is True and closed["cleanup_errors"] == [] and not any(world.cores.halted())
    out = ai.call("linux.halt.close")
    assert out["debug"] is None and out["closed"] is True


def test_the_api_errors_name_the_argument_and_the_cause(race):
    from linux_halt.race_world import ELF, PID
    _, _, ai, _ = race
    with pytest.raises(ApiError) as e:
        ai.call("linux.halt.debug.attach", pid=PID, elf=str(ELF), skew_limit_ns=68, thread_id="tls", kernel_isa="arm",
                max_step_retries=8, bp_unmapped="error", max_halt_s=60.0)
    assert e.value.code == E_INVALID_PARAMS and "thread_id" in str(e.value)
    with pytest.raises(ApiError) as e:
        ai.call("linux.halt.debug.attach", pid=PID, elf=str(ELF), skew_limit_ns=68, thread_id="stack", kernel_isa="arm",
                max_step_retries=8, bp_unmapped="error")
    assert e.value.code == E_INVALID_PARAMS and "max_halt_s" in str(e.value)       # 放置しない上限は既定なし
    with pytest.raises(ApiError, match="debug.attach first") as e:
        ai.call("linux.halt.debug.run", wait_s=1.0)
    assert e.value.code == E_DEVICE


def test_the_mcp_tools_include_linux_halt_debug_and_forward_the_arguments():
    """MCP の道具の一覧に linux.halt.debug.* があり、呼ぶと引数がそのままデーモンへ転送される。"""
    import asyncio
    from fprobe.mcp_client import build_client_server
    from linux_halt.race_world import ELF

    class Recorder:
        def __init__(self):
            self.calls = []

        def call(self, method, **params):
            self.calls.append((method, params))
            return {}
    spec = load_spec()
    rec = Recorder()
    server = build_client_server(rec, spec)
    tools = {t.name for t in asyncio.run(server.list_tools())}
    want = {"linux_halt_debug_" + n for n in ("attach", "status", "break_insert", "break_remove", "run", "halt",
                                               "threads", "read", "memory", "step", "close")}
    assert want <= tools
    args = dict(pid=100, elf=str(ELF), skew_limit_ns=68, thread_id="stack", kernel_isa="arm", max_step_retries=8,
                bp_unmapped="error", max_halt_s=30.0)
    asyncio.run(server.call_tool("linux_halt_debug_attach", args))
    assert rec.calls[0][0] == "linux.halt.debug.attach" and rec.calls[0][1].items() >= args.items()
    spec.validate_params("linux.halt.debug.attach", rec.calls[0][1])
