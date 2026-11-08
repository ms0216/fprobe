"""Linux のプロセスの全コア停止デバッグを、今あるデバッガの画面の API（target.status・debug.source.*・debug.breakpoint.*・
debug.backtrace・debug.frame_variables・target.registers.read）で見せる（fprobe.linux_halt.view）。本物のデーモンと Probe、
2 スレッドの競合の模型（RaceWorld・偽のコア）で試す。応答の形は api_result_check が仕様と照らす。実機ではない。
仕様 docs/superpowers/specs/2026-11-03-linux-halt-ui-design.md。"""
import shutil
from pathlib import Path
import threading
import time

import pytest

from fprobe.api.client import ApiError
from linux_halt.race_world import BIAS, ELF, PID, SP, T_LEADER, T_OTHER
from test_api_linux_halt import race  # noqa: F401 - 試験の器（本物のデーモンと Probe と模型）

ATTACH = dict(pid=PID, elf=str(ELF), skew_limit_ns=68, thread_id="stack", kernel_isa="arm", max_step_retries=8,
              bp_unmapped="error", max_halt_s=60.0)


def wait_for(cond, timeout_s=5.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        value = cond()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("待った条件にならなかった")


class Seen:
    """画面の代わり: 通知 target.state と activity を受ける。"""

    def __init__(self, client):
        self.states, self.methods, self._lock = [], [], threading.Lock()
        client.subscribe(["target.state", "activity"], self._on)

    def _on(self, topic, params):
        with self._lock:
            (self.states if topic == "target.state" else self.methods).append(
                params if topic == "target.state" else params.get("method"))

    def last(self):
        with self._lock:
            return self.states[-1] if self.states else None


def by_name(entries):
    return {e["name"]: e["value"]["value"] for e in entries}


def test_the_ai_stops_the_process_and_the_human_screen_sees_the_same_place_source_breakpoints_and_variables(race):  # noqa: F811
    """AI が MCP（linux.halt.debug.*）で止めると、人の画面が読む API（target.state・ソース・印・呼び出し履歴・変数・
    レジスタ）がそのプロセスの事実を、マイコンと同じ形で返す。人の行のクリック（debug.breakpoint.set / clear）も届く。"""
    world, _, ai, gui = race
    seen = Seen(gui)
    ai.call("linux.halt.debug.attach", **ATTACH, breakpoint_kind="sw")
    st = wait_for(lambda: (s := seen.last()) and s.get("linux") and s)
    assert st["state"] == "HALTED" and st["target"] == "linux" and st["linux"]["pid"] == PID
    assert st["linux"]["stop_kind"] == "attach" and st["halt_reason"] == "request"
    assert [t["tid"] for t in st["linux"]["threads"]] == [T_LEADER, T_OTHER]
    assert st["linux"]["load_bias"] == BIAS

    bp = ai.call("linux.halt.debug.break.insert", location="race.c:11", kind="sw")
    listed = gui.call("debug.breakpoint.list")
    assert listed["watchpoints"] == [] and len(listed["breakpoints"]) == 1
    row = listed["breakpoints"][0]
    assert (row["address"], row["location"], row["file"], row["line"], row["kind"], row["enabled"]) == \
        (bp["address"], "race.c:11", "race.c", 11, "sw", True)
    assert (row["at"]["file"], row["at"]["line"]) == ("race.c", 11)          # 置いた場所（画面は文字を解釈しない）

    world.hit(core=1)
    stop = ai.call("linux.halt.debug.run", wait_s=5.0)
    assert stop["tid"] == T_OTHER
    st = wait_for(lambda: (s := seen.last()) and s.get("linux", {}).get("stop_kind") == "swbreak" and s)
    assert (st["state"], st["pc"], st["function"], st["file"], st["line"]) == \
        ("HALTED", world.addr("race.c:11"), "add_one", "race.c", 11)
    assert st["halt_reason"] == "breakpoint" and st["linux"]["tid"] == T_OTHER and "race.c:11" in st["where"]
    assert gui.call("session.status")["target"] == st                         # 画面が最初に読む状態も同じ
    assert "linux.halt.debug.run" in seen.methods                             # 画面が読み直す合図（activity）

    files = gui.call("debug.source.list")["files"]
    assert [f["name"] for f in files] == ["race.c"] and files[0]["exists"] is True
    src = gui.call("debug.source.read", name="race.c")
    assert "counter = after;" in src["text"].splitlines()[10] and 11 in src["lines"] and "fetched" not in src

    regs = gui.call("target.registers.read")
    assert regs["pc"] == world.addr("race.c:11") and regs["sp"] == SP[T_OTHER] and "cpsr" in regs
    assert gui.call("target.registers.read", names=["pc", "r13"]) == {"pc": regs["pc"], "r13": SP[T_OTHER]}
    bt = gui.call("debug.backtrace")
    assert [(f["function"], f["line"]) for f in bt[:2]] == [("add_one", 11), ("worker", 19)]
    assert bt[0]["index"] == 0 and bt[0]["pc"] == regs["pc"] and "add_one+" in bt[0]["where"]
    fv = gui.call("debug.frame_variables", frame=0)
    assert fv["function"] == "add_one" and by_name(fv["arguments"]) == {"who": 2}
    assert by_name(fv["locals"]) == {"before": 41, "after": 42}
    assert by_name(gui.call("debug.frame_variables", frame=1)["locals"])["i"] == 7

    # 人の行のクリック: 置く（attach の breakpoint_kind）・外す。AI の道（linux.halt.debug.status）にも同じものが見える
    put = gui.call("debug.breakpoint.set", location="race.c:10")
    assert (put["file"], put["line"], put["address"]) == ("race.c", 10, world.addr("race.c:10"))
    assert {b["location"] for b in ai.call("linux.halt.debug.status")["breakpoints"]} == {"race.c:11", "race.c:10"}
    left = gui.call("debug.breakpoint.clear", location="race.c:11")
    assert [b["location"] for b in left["breakpoints"]] == ["race.c:10"]
    assert gui.call("debug.breakpoint.clear")["breakpoints"] == []
    assert ai.call("linux.halt.debug.status")["breakpoints"] == []

    ai.call("linux.halt.debug.close")
    wait_for(lambda: (s := seen.last()) and "linux" not in s)                 # 閉じたらマイコンの道（未接続）に戻る
    assert seen.last()["state"] == "DISCONNECTED"


def test_the_screen_way_of_setting_a_breakpoint_needs_the_kind_named_at_attach(race):  # noqa: F811
    _, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH)
    with pytest.raises(ApiError, match="breakpoint_kind.*Attach の詳細"):
        gui.call("debug.breakpoint.set", location="race.c:11")
    with pytest.raises(ApiError, match="condition"):
        gui.call("debug.breakpoint.set", location="race.c:11", condition="who == 1")


def _fetcher(world, asked, missing=()):
    """linux.core.fetch の代わり（ssh の cat）。実機と同じく、全コアを止めている間の対象は ssh に答えない。"""
    def fake_fetch(target, path, local_path):
        asked.append((target, path))
        if world.cores.halted() and all(world.cores.halted()):
            raise RuntimeError(f"{path} を読めない（255）: ssh: connect to host <Pi の LAN のアドレス> port 22: Connection timed out")
        if path in missing:
            raise RuntimeError(f"{path} を読めない（1）: cat: {path}: No such file or directory")
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        data = (ELF if path.startswith("/proc/") else ELF.parent / "race.c").read_bytes()
        local.write_bytes(data)
        return {"path": path, "local_path": local_path, "bytes": len(data), "sha256": "ab" * 32}
    return fake_fetch


def test_a_source_not_on_this_pc_is_fetched_at_attach_before_the_cores_stop_and_the_copy_is_used(race, tmp_path,  # noqa: F811
                                                                                                 monkeypatch):
    """実機（2026-11-03）: 全コアを止めている間に ssh で取りに行き、止まった Pi が答えなかった。attach のとき（コアを止める
    前、ELF を取り寄せるのと同じ時）に、PC に無いソースを ELF と同じ所の下に写し、以後はその写しを使う。"""
    world, probe, ai, gui = race
    elf = tmp_path / "pi" / "race_arm"                       # 隣に race.c の無い ELF（行表は race.c）
    elf.parent.mkdir()
    shutil.copy(ELF, elf)
    asked = []
    monkeypatch.setattr(probe, "linux_core_fetch", _fetcher(world, asked))
    a = ai.call("linux.halt.debug.attach", **{**ATTACH, "elf": str(elf)}, fetch_from_target="pi", breakpoint_kind="hw")
    assert asked == [("pi", f"/proc/{PID}/exe"), ("pi", "race.c"),
                     ("pi", f"/proc/{PID}/maps")]               # 実行ファイル・ソース・共有ライブラリの一覧を、止める前に
    assert a["library_symbols"] == []                           # 模型の maps に共有ライブラリは無い
    local = elf.parent / "race_arm.sources" / "race.c"
    assert a["sources_fetched"] == [{"name": "race.c", "target": "pi", "path": "race.c", "local_path": str(local),
                                     "bytes": local.stat().st_size, "sha256": "ab" * 32}]
    assert all(world.cores.halted())
    files = gui.call("debug.source.list")["files"]
    assert files[0]["exists"] is False and files[0]["fetched"]["local_path"] == str(local)
    src = gui.call("debug.source.read", name="race.c")
    assert len(asked) == 3                                   # 止まっている間は取りに行かない（写しを読む）
    assert "counter = after;" in src["text"] and src["path"] == str(local) and src["fetched"]["path"] == "race.c"


def test_a_source_that_could_not_be_fetched_at_attach_says_why(race, tmp_path, monkeypatch):  # noqa: F811
    world, probe, ai, gui = race
    elf = tmp_path / "race_arm"
    shutil.copy(ELF, elf)
    monkeypatch.setattr(probe, "linux_core_fetch", _fetcher(world, [], missing=("race.c",)))
    a = ai.call("linux.halt.debug.attach", **{**ATTACH, "elf": str(elf)}, fetch_from_target="pi")
    assert a["sources_fetched"][0]["error"].endswith("No such file or directory")
    assert gui.call("debug.source.list")["files"][0]["fetch_error"]["path"] == "race.c"
    with pytest.raises(ApiError, match="取り寄せられなかった"):
        gui.call("debug.source.read", name="race.c")


def test_a_source_that_is_nowhere_is_refused_with_the_reason(race, tmp_path):  # noqa: F811
    _, _, ai, gui = race
    elf = tmp_path / "race_arm"
    shutil.copy(ELF, elf)
    a = ai.call("linux.halt.debug.attach", **{**ATTACH, "elf": str(elf)})
    assert a["sources_fetched"] == []
    f = gui.call("debug.source.list")["files"][0]
    assert "fetched" not in f and "fetch_error" not in f
    with pytest.raises(ApiError, match="止まっている間は"):
        gui.call("debug.source.read", name="race.c")


def test_the_elf_status_and_the_system_status_show_the_attached_process(race, monkeypatch):  # noqa: F811
    """実機（2026-11-03）: ソースの画面が「プログラムが読み込まれていません」、システムの欄が「未接続」だった。attach の間は
    debug.elf.status が attach の ELF を、system.status の mcu が target.status（linux 欄）とプロセスのメモリの語を返す。"""
    from types import SimpleNamespace
    world, probe, ai, gui = race
    assert gui.call("debug.elf.status")["path"] is None
    ai.call("linux.halt.debug.attach", **ATTACH)
    st = gui.call("debug.elf.status")
    assert st["path"] == str(ELF) and st["file_changed"] is False and st["target_image"] == "unknown"
    probe._session.device = SimpleNamespace(time=lambda: 0)            # 機器の時計（偽の機器）
    monkeypatch.setattr(probe, "_caps", lambda: SimpleNamespace(timebase_hz=1, fpga_obs=False))
    sysst = gui.call("system.status", addresses=[world.addr("counter")])
    assert sysst["mcu"]["connected"] is True and sysst["mcu"]["state"] == "HALTED"
    assert sysst["mcu"]["linux"]["pid"] == PID and sysst["mcu"]["memory"] == {f"0x{world.addr('counter'):08X}": 41}


def test_the_watched_state_stays_on_the_process_while_the_watcher_reads_it_again_and_again(race):  # noqa: F811
    """見張りが何度読み直しても、attach の間の target.state は Linux のプロセス（未接続に戻らない）。読めない欄は理由。"""
    world, probe, ai, gui = race
    seen = Seen(gui)
    ai.call("linux.halt.debug.attach", **ATTACH)
    wait_for(lambda: (s := seen.last()) and s.get("linux"))
    for _ in range(5):
        assert gui.call("target.status")["linux"]["pid"] == PID
    assert all("linux" in s for s in seen.states[seen.states.index(next(s for s in seen.states if "linux" in s)):])


# --- 第 2 段: 画面の続行・停止・ステップ・Run to Cursor と、読み・書きの残り（実機の制約: 作業スレッドを長く塞がない）----

SCREEN = dict(breakpoint_kind="sw", step_max_instructions=64, step_timeout_s=2.0)


def arrive_on_resume(world, core, address):
    """次に全コアを再開したら、コア core が address で自分で止まる（一時的なブレークポイントに当たる）。"""
    original = world.cores.resume

    def resume(cores):
        original(cores)
        world.cores.resume = original
        st = world.cores.states[core]
        world.cores.states[core] = st.__class__(**{**st.__dict__, "pc": address})
        world.cores.reg_lists[core][15] = address
        world.hit(core=core)
    world.cores.resume = resume


def test_the_human_resumes_without_blocking_and_the_watcher_picks_up_the_hit(race):  # noqa: F811
    """画面の続行（target.resume）は待たずに返る。当たりは見張りの target.status（poll）が拾い、target.state が止まった
    場所を配る（人も AI と同じに走らせて止まりを見られる）。"""
    world, _, ai, gui = race
    seen = Seen(gui)
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    ai.call("linux.halt.debug.break.insert", location="race.c:11", kind="sw")
    t0 = time.monotonic()
    st = gui.call("target.resume")
    assert time.monotonic() - t0 < 1.0 and st["state"] == "RUNNING" and st["linux"]["stop_kind"] is None
    assert not any(world.cores.halted())
    world.hit(core=1)
    st = wait_for(lambda: (s := seen.last()) and s["state"] == "HALTED" and s["linux"]["stop_kind"] == "swbreak" and s)
    assert (st["line"], st["linux"]["tid"]) == (11, T_OTHER) and all(world.cores.halted())
    assert ai.call("linux.halt.debug.status")["last_run"]["kind"] == "swbreak"      # AI にも同じ止まり


def test_a_long_ai_run_does_not_block_the_human_halt_or_the_watched_state(race):  # noqa: F811
    """AI の linux.halt.debug.run（長い wait_s）の間も、人の停止（target.halt）が効き、target.state（RUNNING）が配られる。"""
    world, _, ai, gui = race
    seen = Seen(gui)
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    result = {}
    worker = threading.Thread(target=lambda: result.update(ai.call("linux.halt.debug.run", wait_s=30.0)))
    t0 = time.monotonic()
    worker.start()
    wait_for(lambda: (s := seen.last()) and s["state"] == "RUNNING")             # 待っている間も配られる
    st = gui.call("target.halt")
    worker.join(10)
    assert time.monotonic() - t0 < 10.0 and not worker.is_alive()
    assert result["kind"] == "int" and st["state"] == "HALTED" and all(world.cores.halted())
    wait_for(lambda: (s := seen.last()) and s["state"] == "HALTED")


def test_screen_steps_use_the_limits_named_at_attach_and_answer_in_the_debug_step_form(race):  # noqa: F811
    world, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH)
    with pytest.raises(ApiError, match="step_max_instructions"):
        gui.call("debug.step", mode="over")
    ai.call("linux.halt.debug.close")
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    world.cores.on_step = lambda core: None                                    # pc が進まない: 命令の数の上限
    r = gui.call("debug.step", mode="over")
    assert (r["reason"], r["steps"], r["line"], r["time_source"]) == ("max_instructions", 64, 11, "host")
    # Run to Cursor（until + location）: 今のスレッド（規則: 対象のスレッドを走らせている最小番号のコア 1 の 101）が着くまで
    assert gui.call("target.status")["linux"]["tid"] == T_OTHER
    target = world.addr("race.c:10")
    arrive_on_resume(world, 1, target)
    r = gui.call("debug.step", mode="until", location="race.c:10")
    assert (r["reason"], r["pc"], r["line"]) == ("step", target, 10)
    # out: 呼び出し元（worker）へ
    ret = gui.call("debug.backtrace")[1]["pc"]
    arrive_on_resume(world, 1, ret)
    r = gui.call("debug.step", mode="out")
    assert (r["reason"], r["pc"], r["function"]) == ("step", ret, "worker")
    # 命令ステップ
    st = gui.call("target.step", count=2)
    assert st["steps_done"] == 2 and st["state"] == "HALTED"


def test_reads_and_writes_on_the_screen_answer_from_the_process_and_never_fall_to_the_mcu(race):  # noqa: F811
    world, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    tid = gui.call("target.status")["linux"]["tid"]
    assert tid in (T_LEADER, T_OTHER)
    e = gui.call("debug.evaluate", expression="counter")
    assert e["value"]["value"] == 41 and e["address"] == world.addr("counter")
    with pytest.raises(ApiError, match="live"):
        gui.call("debug.evaluate", expression="counter", live=True)
    c = gui.call("debug.expression.check", expression="counter")
    assert c["address"] == world.addr("counter") and c["type"] == "volatile int"
    assert gui.call("debug.type_info", name="int")["size"] == 4
    si = gui.call("debug.symbol.info", location="add_one")
    assert si["address"] == world.addr("add_one") and si["function"] == "add_one" and si["kind"] == "function"
    assert gui.call("debug.symbol.info", location=f"0x{world.addr('race.c:11'):x}")["line"] == 11
    pc = gui.call("target.registers.read")["pc"]
    d = gui.call("debug.disasm.around", before=2, after=2)
    assert d["pc"] == pc and d["instructions"][d["pc_index"]]["address"] == pc and d["memory"] == "target"
    # ブレークポイントの有効・無効（人の Breakpoints のチェック）
    gui.call("debug.breakpoint.set", location="race.c:11")
    off = gui.call("debug.breakpoint.enable", location="race.c:11", enabled=False)
    assert [(b["location"], b["enabled"]) for b in off["breakpoints"]] == [("race.c:11", False)]
    assert ai.call("linux.halt.debug.status")["breakpoints"] == []           # 機器からは外した
    on = gui.call("debug.breakpoint.enable", location="race.c:11", enabled=True)
    assert on["breakpoints"][0]["enabled"] is True and on["breakpoints"][0]["kind"] == "sw"
    # 書く: レジスタ・式
    gui.call("target.registers.write", name="r0", value=0x1234)
    assert gui.call("target.registers.read", names=["r0"]) == {"r0": 0x1234}
    w = gui.call("debug.write_expression", expression="counter", value=7)
    assert w["value"]["value"] == 7 and world.read_user(world.addr("counter"), 4) == (7).to_bytes(4, "little")
    for method, params in (("debug.run_until_halt", {}),
                           ("debug.watchpoint.set", {"location": "counter"}), ("target.reset", {}), ("target.disconnect", {}),
                           ("watch.write", {"expression": "counter", "value": 1})):
        with pytest.raises(ApiError, match="Linux"):
            gui.call(method, **params)


def test_the_screen_reads_the_process_memory_in_the_same_shape_as_the_microcontroller_and_compares(race):  # noqa: F811
    """113: 人の画面の「メモリ」パネルは target.memory.view を呼ぶ。Linux のプロセスに attach している間も、マイコンと同じ形
    （rows・dump・names・compare の印）で、AI の linux.halt.debug.memory と同じバイトが読める。"""
    world, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    addr = world.addr("counter")
    first = gui.call("target.memory.view", address=addr, length=16, unit=32, width=16, compare=True)
    assert (first["memory"], first["address"], first["length"], first["unit"]) == ("target", addr, 16, 32)
    assert first["rows"][0]["values"][0] == int.from_bytes(world.read_user(addr, 4), "little")
    assert first["compared"] is False and first["changes"] == []
    assert {"address": addr, "name": "counter", "kind": "symbol"} in first["names"]
    assert first["dump"].startswith(f"{addr:08x}: ")
    gui.call("debug.write_expression", expression="counter", value=7)
    second = gui.call("target.memory.view", address=addr, length=16, unit=32, width=16, compare=True)
    assert second["compared"] is True and second["rows"][0]["changed"][0] is True
    assert [c["address"] for c in second["changes"]] == [addr] and second["rows"][0]["values"][0] == 7
    ai_read = ai.call("linux.halt.debug.memory", address=addr, length=16)               # AI の読みと同じバイト
    assert bytes.fromhex(ai_read["data"]) == b"".join(v.to_bytes(4, "little") for v in second["rows"][0]["values"])


def test_the_screen_memory_view_says_why_an_address_cannot_be_read_and_refuses_elf_and_oversize(race):  # noqa: F811
    world, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    with pytest.raises(ApiError, match="読めない"):
        gui.call("target.memory.view", address=0x10, length=4)
    with pytest.raises(ApiError, match="from_elf"):
        gui.call("target.memory.view", address=world.addr("counter"), length=4, from_elf=True)
    with pytest.raises(ApiError, match="4096 以下"):                  # API の仕様の検査が先に断る
        gui.call("target.memory.view", address=world.addr("counter"), length=5000)


def test_the_status_reads_are_not_recorded_as_operations_so_the_screen_polling_does_not_flood_the_activity(race):  # noqa: F811
    """画面（LinuxHaltPanel）は状態を読み続ける。読みの linux.halt.status・linux.halt.debug.status が操作として記録されると、
    アクティビティが読みで埋まる（2026-11-05 実機: attach したままページを読み直すと毎秒数十回）。読みの種別にして記録しない。"""
    _, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    before = len(ai.call("session.activity.list")["entries"])
    for _ in range(5):                                            # 画面の自動の読み（quiet）
        gui.call_quiet("linux.halt.status")
        gui.call_quiet("linux.halt.debug.status")
    gui.call("linux.halt.debug.status")                           # quiet でない読みは、そのまま記録される（隠さない）
    entries = ai.call("session.activity.list")["entries"]
    assert [e["method"] for e in entries[before:]] == ["linux.halt.debug.status"]     # quiet の 10 回は記録に足さない


# --- 止まっている間の linux.*（C2）: 止まった対象の ssh は答えない。叩かずに事実を誤りにする ----------------------------

def test_linux_ssh_tools_refuse_while_the_attached_target_is_halted_and_work_again_when_it_runs(race, tmp_path,  # noqa: F811
                                                                                                 monkeypatch):
    world, probe, ai, gui = race
    here = Path(__file__).parent
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FPROBE_SSH", str(here / "linux_fake_ssh.py"))
    monkeypatch.setenv("FPROBE_SCP", str(here / "linux_fake_scp.py"))
    monkeypatch.setenv("PYTHONPATH", str(here))
    monkeypatch.setenv("FPROBE_FAKE_SSH_LOG", str(log))
    ai.call("linux.connect", target="pi", host="h", user="u")
    elf = tmp_path / "race_arm"
    shutil.copy(ELF, elf)
    monkeypatch.setattr(probe, "linux_core_fetch", _fetcher(world, []))
    ai.call("linux.halt.debug.attach", **{**ATTACH, "elf": str(elf)}, fetch_from_target="pi")
    before = log.read_text(encoding="utf-8")
    t0 = time.monotonic()
    with pytest.raises(ApiError, match="全コアを止めている"):
        gui.call("linux.log.read", target="pi", lines=5)
    with pytest.raises(ApiError, match="全コアを止めている"):
        gui.call("linux.connect", target="pi", host="h", user="u")
    assert time.monotonic() - t0 < 2.0 and log.read_text(encoding="utf-8") == before      # ssh を叩かない
    ai.call("linux.halt.debug.run", wait_s=0)                                 # 走らせる: 使える
    gui.call("linux.log.read", target="pi", lines=5)
    assert "journalctl" in log.read_text(encoding="utf-8")
    ai.call("linux.halt.debug.halt")
    out = gui.call("linux.disconnect", target="pi")
    assert out["disconnected"] is True and "全コアを止めている" in out["remote_cleanup_skipped"]


def test_operations_that_hold_the_device_lock_longer_than_the_halt_budget_are_refused_while_halted(race):  # noqa: F811
    """C3: 錠を長く持つ操作（ロジアナの取り込み・流し）が、止めたまま放置しない見張り（max_halt_s）の残りより長ければ断る
    （見張りが錠を取れず、全コアを止めたままになる）。走っていれば断らない（見張りの出番が無い）。"""
    _, probe, ai, gui = race
    ai.call("linux.halt.debug.attach", **{**ATTACH, "max_halt_s": 5.0})
    with pytest.raises(RuntimeError, match="max_halt_s"):
        probe.la_stream(vcd_path="x.vcd", duration_s=60.0)
    with pytest.raises(RuntimeError, match="max_halt_s"):
        probe.la_collect(timeout_s=60.0)
    probe._require_halt_budget("la.collect", 1.0)                            # 残りより短ければ通す
    ai.call("linux.halt.debug.run", wait_s=0)
    probe._require_halt_budget("la.stream", 60.0)                            # 走っていれば見張りは要らない


def test_choosing_a_thread_switches_what_the_debugger_screen_reads_until_the_next_stop(race):  # noqa: F811
    """A2: 人が Threads でスレッドを選ぶ（linux.halt.debug.select）と、レジスタ・呼び出し履歴・変数がそのスレッドになる。
    AI の linux.halt.debug.read と同じものが人に見える。次に止まると止まったスレッドに戻る。"""
    world, _, ai, gui = race
    seen = Seen(gui)
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    st = gui.call("target.status")
    assert (st["linux"]["tid"], st["linux"]["tid_from"]) == (T_OTHER, "rule")
    assert gui.call("linux.halt.debug.select", tid=T_LEADER) == {"tid": T_LEADER, "tid_from": "selected"}
    st = wait_for(lambda: (s := seen.last()) and s["linux"]["tid"] == T_LEADER and s)
    assert st["linux"]["tid_from"] == "selected" and st["line"] == 10
    assert gui.call("target.registers.read")["sp"] == SP[T_LEADER]
    assert gui.call("debug.backtrace")[0]["line"] == 10
    assert by_name(gui.call("debug.frame_variables")["arguments"]) == {"who": 1}
    with pytest.raises(ApiError, match="not a thread"):
        gui.call("linux.halt.debug.select", tid=999)
    ai.call("linux.halt.debug.break.insert", location="race.c:11", kind="sw")
    world.hit(core=1)
    ai.call("linux.halt.debug.run", wait_s=5.0)
    st = gui.call("target.status")
    assert (st["linux"]["tid"], st["linux"]["tid_from"]) == (T_OTHER, "last_stop")


# --- 鍵の順番（2026-11-03 実機の行き詰まり）: 作業スレッドの target.status・system.status、作業スレッドを通さない読み、
#     debug.run の待ち、止める操作、放置しない見張りを同時に何度も回しても、必ず時間の上限内に終わる -------------------

def _slow(cores, names, delay_s):
    """実機のように、コアの口の 1 回ずつに遅れを入れる（重なりを起こす）。"""
    for name in names:
        original = getattr(cores, name)

        def slow(*args, _f=original, **kwargs):
            time.sleep(delay_s)
            return _f(*args, **kwargs)
        setattr(cores, name, slow)


def test_concurrent_status_reads_runs_halts_and_the_watchdog_never_deadlock(race, monkeypatch):  # noqa: F811
    from types import SimpleNamespace
    world, probe, ai, gui = race
    _slow(world.cores, ("regs", "read_phys", "read_kernel", "halt_all", "resume", "snapshot"), 0.002)
    probe._session.device = SimpleNamespace(time=lambda: 0)
    monkeypatch.setattr(probe, "_caps", lambda: SimpleNamespace(timebase_hz=1, fpga_obs=False))
    ai.call("linux.halt.debug.attach", **{**ATTACH, "max_halt_s": 0.3}, **SCREEN)
    done, errors = threading.Event(), []

    def loop(fn):
        while not done.is_set():
            try:
                fn()
            except ApiError:
                pass                                       # 走っている・止まっている等の断りは事実（行き詰まりではない）
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
    jobs = [lambda: gui.call("target.status"), lambda: gui.call("system.status"),
            lambda: probe.target_status_unqueued(), lambda: ai.call("linux.halt.debug.run", wait_s=0.1),
            lambda: gui.call("target.halt"), lambda: gui.call("target.resume"),
            lambda: gui.call("debug.backtrace")]
    threads = [threading.Thread(target=loop, args=(j,), daemon=True) for j in jobs]
    for t in threads:
        t.start()
    time.sleep(3.0)
    done.set()
    for t in threads:
        t.join(20)
    assert not [t for t in threads if t.is_alive()], "行き詰まった（鍵の順番）"
    assert errors == []
    gui.call("target.halt")                                 # 止めたまま放置すると、見張りが max_halt_s で走らせる
    wait_for(lambda: not any(world.cores.halted()), timeout_s=5.0)


def test_the_watchdog_resumes_the_cores_once_a_long_holder_of_the_device_lock_lets_go(race):  # noqa: F811
    """見張りは機器を使うので、機器の錠を持ち続ける操作の間は待つ（長いものは C3 で断る）。放されたらすぐ走らせる。"""
    world, probe, ai, gui = race
    ai.call("linux.halt.debug.attach", **{**ATTACH, "max_halt_s": 0.3})
    held = threading.Event()

    def hold():
        with probe._lock:
            held.set()
            time.sleep(1.0)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    assert probe.target_status_unqueued()["state"] == "HALTED"     # 作業スレッドを通さない読みは錠を待たない
    t.join(5)
    wait_for(lambda: not any(world.cores.halted()), timeout_s=3.0)
    assert ai.call("linux.halt.debug.status")["auto_resumed"]


def test_ui_state_set_panel_is_in_the_reply_and_the_notification_through_the_daemon(race):  # noqa: F811
    """実機の確かめ（10）: ui.state.set(panel=…) の応答に panel が見えなかった。デーモン越しの応答と通知に出る。"""
    _, _, ai, gui = race
    got = []
    gui.subscribe(["ui.state"], lambda topic, params: got.append(params))
    out = ai.call("ui.state.set", panel="threads")
    assert out["panel"] == {"id": "threads", "seq": 1} and "panel" in out["changed"]
    wait_for(lambda: got and got[-1]["state"]["panel"] == {"id": "threads", "seq": 1})
    assert ai.call("ui.state.get")["panel"] == {"id": "threads", "seq": 1}


def test_session_status_follows_attach_and_close_even_without_a_screen(race):  # noqa: F811
    """実機の確かめ（12）: linux.halt.debug.close の直後も session.status の target が HALTED のままだった（target.state を
    購読する画面が無いと見張りが読み直さなかった）。コアを動かし得る操作の後は、購読が無くても読み直す。"""
    _, _, ai, _ = race
    ai.call("linux.halt.debug.attach", **ATTACH)
    wait_for(lambda: (ai.call("session.status")["target"] or {}).get("linux"))
    ai.call("linux.halt.debug.close")
    wait_for(lambda: (ai.call("session.status")["target"] or {}).get("state") == "DISCONNECTED")


def test_the_state_read_right_after_select_or_a_step_returns_its_result(race):  # noqa: F811
    """実機（2026-11-03、59e540bc）: select(tid) の応答は 4459 なのに、直後の session.status の target.linux.tid は 4458
    だった。今のスレッドを変える操作の直後の状態の読みは、必ずその結果を返す（見張りが後で読み直すのを待たない）。"""
    world, probe, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    unqueued = probe.target_status_unqueued

    def slow():                                               # 実機の読みの遅れ（見張りの読み直しが応答より遅れる）
        time.sleep(0.3)
        return unqueued()
    probe.target_status_unqueued = slow
    for tid in (T_LEADER, T_OTHER, T_LEADER):
        assert gui.call("linux.halt.debug.select", tid=tid)["tid"] == tid
        assert gui.call("session.status")["target"]["linux"]["tid"] == tid
        assert gui.call("target.status")["linux"]["tid"] == tid
    world.cores.on_step = lambda core: None
    gui.call("target.step", count=1)
    assert gui.call("session.status")["target"]["linux"]["stop_kind"] == "step"


def test_state_reads_do_not_wait_for_a_long_attach_and_say_it_is_attaching(race, tmp_path, monkeypatch):  # noqa: F811
    """実機: attach（ソースの取り寄せ 約 4 秒）の間、画面の状態の読みが最大 4.1 秒待たされた。長い操作の間も状態の読みは
    待たない: target.state は attach 中であることを、target.status は待たずにその事実の誤りを返す。"""
    from types import SimpleNamespace
    world, probe, ai, gui = race
    probe._session.device = SimpleNamespace(time=lambda: 0)
    monkeypatch.setattr(probe, "_caps", lambda: SimpleNamespace(timebase_hz=1, fpga_obs=False))
    seen = Seen(gui)
    elf = tmp_path / "race_arm"
    shutil.copy(ELF, elf)
    fetch = _fetcher(world, [])

    def slow_fetch(target, path, local_path):
        time.sleep(1.5)                                       # 止まっていない Pi の ssh の取り寄せ
        return fetch(target, path, local_path)
    monkeypatch.setattr(probe, "linux_core_fetch", slow_fetch)
    worker = threading.Thread(target=lambda: ai.call("linux.halt.debug.attach", **{**ATTACH, "elf": str(elf)},
                                                     fetch_from_target="pi"))
    worker.start()
    st = wait_for(lambda: (s := seen.last()) and s.get("linux_attaching") and s, timeout_s=3.0)
    assert st["state"] == "DISCONNECTED" and st["linux_attaching"]["pid"] == PID
    t0 = time.monotonic()
    with pytest.raises(ApiError, match="attach"):
        gui.call("target.status")
    sysst = gui.call("system.status")
    assert time.monotonic() - t0 < 0.8                      # 取り寄せ（3 秒）を待たない
    assert sysst["mcu"]["connected"] is False and sysst["mcu"]["linux_attaching"]["pid"] == PID
    worker.join(10)
    wait_for(lambda: (s := seen.last()) and s.get("linux"))


def test_the_status_tells_the_screen_that_breakpoint_edits_work_while_running(race):  # noqa: F811
    """画面が「走行中も置き外しを許す」ための事実: target.status の linux に breakpoint_edit_requires_halt = False
    （エンジンが走行中の置き外しを、全コアを短く止めて行い、また走らせる）。画面は対象の種類を決めず、この値を見る。
    人（画面）の操作も AI と同じに、走行中に通る。"""
    world, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    st = gui.call("target.status")
    assert st["linux"]["breakpoint_edit_requires_halt"] is False
    ai.call("linux.halt.debug.run", wait_s=0.01)                          # 走らせたままにする
    assert not any(world.cores.halted())
    gui.call("debug.breakpoint.set", location="race.c:11")                  # 人の画面のクリックと同じ呼び出し
    assert not any(world.cores.halted()), "置いたあとは、また走っている"
    assert gui.call("target.status")["state"] == "RUNNING"
    gui.call("debug.breakpoint.clear")
    assert not any(world.cores.halted())
    assert ai.call("linux.halt.debug.status")["brief_halts"]["count"] == 2

def test_a_step_on_a_dead_process_says_the_exit_status_and_the_killing_signal():
    """クラッシュで死んだプロセスへのステップの誤り: カーネルの終了コード（(状態 << 8) | シグナル）の読み方を事実として添える
    （「exit, code 11」だけでは、終了状態 11 なのかシグナル 11 なのか分からなかった）。"""
    from fprobe.linux_halt.view import LinuxDebugView
    with pytest.raises(RuntimeError, match=r"プロセス 77 は終了している（exit, code 11 = exit status 0, killed by signal 11）"):
        LinuxDebugView._step_reply({"address": None, "kind": "exit", "code": 11}, 0.0, 77)     # 以後の操作の断りと同じ文
    with pytest.raises(RuntimeError, match=r"code 256 = exit status 1）。code はカーネルの終了コード"):
        LinuxDebugView._step_reply({"address": None, "kind": "exit", "code": 256}, 0.0)

def test_the_debug_status_returns_the_settings_the_attach_used_so_the_human_screen_shows_what_the_ai_ran(race):  # noqa: F811
    """AI が attach したとき、人の画面の欄に同じ設定値を映すための事実: linux.halt.debug.status の options は、attach に
    渡した設定そのまま（渡さなかった任意の設定は null）。"""
    world, _, ai, gui = race
    ai.call("linux.halt.debug.attach", **ATTACH, **SCREEN)
    opts = gui.call("linux.halt.debug.status")["options"]
    assert opts == {**ATTACH, **SCREEN, "fetch_from_target": None}
    ai.call("linux.halt.debug.close")
    ai.call("linux.halt.debug.attach", **ATTACH)
    assert gui.call("linux.halt.debug.status")["options"]["breakpoint_kind"] is None


def test_the_open_status_names_what_is_refused_and_how_the_lock_is_used_by_identifiers_not_prose(race):  # noqa: F811
    """画面の言葉（英語・日本語）は画面の i18n が付ける。API は識別子を返す（日本語の文を英語の画面に出さない）。"""
    world, _, ai, gui = race
    occ = gui.call("linux.halt.status")["occupies"]
    assert occ["probe_dap"] is True
    assert occ["refused_while_open"] == ["target.connect", "lowlevel", "linux.memap.connect_jtag", "probe.design.switch"]
    assert occ["device_lock"] == "per_call"


def test_the_watched_state_says_linux_halt_is_open_before_any_process_is_attached(race, monkeypatch):  # noqa: F811
    """backlog 80 番: linux.halt.open だけ（attach の前）の画面は、下の帯が「Not connected」、Linux の停止パネルは「開いている」
    で、接続の言葉が食い違った。マイコンは未接続（DISCONNECTED）のまま、linux.halt が開いている事実（linux_halt_open）を
    target.state と system.status に載せる。attach している間は linux の欄に変わり、閉じれば消える。"""
    from types import SimpleNamespace
    world, probe, ai, gui = race
    probe._session.device = SimpleNamespace(time=lambda: 0)
    monkeypatch.setattr(probe, "_caps", lambda: SimpleNamespace(timebase_hz=1, fpga_obs=False))
    seen = Seen(gui)
    st = wait_for(lambda: (s := seen.last()) and s.get("linux_halt_open") and s)
    assert st["state"] == "DISCONNECTED" and st["linux_halt_open"] == {"backend": "fprobe"}
    assert gui.call("system.status")["mcu"]["linux_halt_open"] == {"backend": "fprobe"}
    ai.call("linux.halt.debug.attach", **ATTACH)
    st = wait_for(lambda: (s := seen.last()) and s.get("linux") and s)
    assert "linux_halt_open" not in st
    ai.call("linux.halt.debug.close")
    wait_for(lambda: (s := seen.last()) and s.get("linux_halt_open"))
    ai.call("linux.halt.close")
    st = wait_for(lambda: (s := seen.last()) and s["state"] == "DISCONNECTED" and "linux_halt_open" not in s and s)
    assert "linux_halt_open" not in gui.call("system.status")["mcu"]


def test_a_step_that_stopped_on_a_signal_says_which_one():
    """reason が signal の step の応答に、シグナルの番号と名前（SIGILL 等）を載せる（Haiku の周回 3: 番号が無く、何で止まったか分からなかった）。"""
    from fprobe.linux_halt.view import LinuxDebugView
    out = LinuxDebugView._step_reply({"address": 0x10400, "kind": "signal", "signal": 4, "code": 4}, 0.1)
    assert out["reason"] == "signal" and out["signal"] == 4 and out["signal_name"] == "SIGILL"
    plain = LinuxDebugView._step_reply({"address": 0x10400, "kind": "step", "reached_new_line": True}, 0.1)
    assert "signal" not in plain and "signal_name" not in plain
