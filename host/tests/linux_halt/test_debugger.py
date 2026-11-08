"""linux.halt.debug.*（AI が JTAG の全コア停止で止めて調べる道具）の中身 HaltSession.debug_* を、模擬のコア（SimCores）と
合成のカーネルの上の 2 スレッドの競合のアプリの模型（race_world）で試す。実機ではない。仕様 2026-11-03-linux-halt-ai-tools-design.md。"""
import struct
import time

import pytest

from fprobe.linux_halt.bp import BKPT
from fprobe.linux_halt.engine import EngineError
from fprobe.linux_halt.service import Busy
from .race_world import BIAS, ELF, PID, SP, T_LEADER, T_OTHER, RaceWorld

ATTACH = dict(pid=PID, elf=str(ELF), skew_limit_ns=68, thread_id="stack", kernel_isa="arm", max_step_retries=8,
              bp_unmapped="error", max_halt_s=60.0)


@pytest.fixture
def world(tmp_path):
    w = RaceWorld(tmp_path)
    yield w
    w.s.close()


def value(entry):
    return entry["value"]["value"]


def by_name(entries):
    return {e["name"]: value(e) for e in entries}


# --- 端から端: 行にブレークポイント → 当たる → 全コア停止 → 両スレッドの行と変数を 1 回で読む → 再開 → 閉じる ------------

def test_end_to_end_line_breakpoint_hit_all_stop_read_both_threads_resume_close(world):
    s, w = world.s, world
    original = w.read_user(w.addr("race.c:11"), 4)

    a = s.debug_attach(**ATTACH)
    assert a["pid"] == PID and a["comm"] == "race" and a["running"] is False and all(w.cores.halted())
    assert a["image"]["load_bias"] == BIAS and a["image"]["phdr_match"] is True and a["image"]["entry_match"] is True
    assert a["elf"]["has_dwarf"] is True and [t["tid"] for t in a["threads"]] == [T_LEADER, T_OTHER]
    assert a["attach_stop"]["cores_halted"] == [0, 1, 2, 3] and a["attach_stop"]["skew_verdict"] == "within"

    bp = s.debug_break_insert(location="race.c:11", kind="sw")
    assert bp["address"] == w.addr("race.c:11") and bp["link_address"] == w.addr("race.c:11") - BIAS
    assert (bp["kind"], bp["width"], bp["function"], bp["file"], bp["line"]) == ("sw", 4, "add_one", "race.c", 11)
    assert w.read_user(bp["address"], 4) == BKPT[4]                       # BKPT を対象のメモリに書いた

    w.hit(core=1)
    stop = s.debug_run(wait_s=5.0)
    assert (stop["kind"], stop["tid"], stop["breakpoint"]) == ("swbreak", T_OTHER, bp["id"])
    assert (stop["function"], stop["file"], stop["line"]) == ("add_one", "race.c", 11)
    assert stop["running"] is False and all(w.cores.halted())
    assert stop["stop"]["method"] == "simultaneous" and stop["stop"]["cores_halted"] == [0, 1, 2, 3]
    assert stop["stop"]["skew_upper_bound_ns"] == 50 and stop["stop"]["basis"] == "simulated"
    assert stop["stop"]["skew_verdict"] == "within" and stop["stop"]["skew_limit_ns"] == 68

    t = s.debug_threads(variables=True, expressions=["counter", "before + 1"], max_elements=16)
    rows = {r["tid"]: r for r in t["threads"]}
    assert set(rows) == {T_LEADER, T_OTHER}
    assert (rows[T_OTHER]["on_core"], rows[T_OTHER]["mode"], rows[T_OTHER]["registers_from"]) == (1, "user", "core")
    assert (rows[T_LEADER]["on_core"], rows[T_LEADER]["mode"]) == (3, "user")
    assert (rows[T_OTHER]["function"], rows[T_OTHER]["line"]) == ("add_one", 11)
    assert (rows[T_LEADER]["function"], rows[T_LEADER]["line"]) == ("add_one", 10)
    assert rows[T_OTHER]["registers"]["sp"] == SP[T_OTHER] and rows[T_OTHER]["pc"] == w.addr("race.c:11")
    assert by_name(rows[T_OTHER]["locals"]) == {"before": 41, "after": 42}
    assert by_name(rows[T_LEADER]["locals"])["before"] == 41                 # 2 つのスレッドが同じ値を読んだ（足し損ね）
    assert by_name(rows[T_OTHER]["arguments"]) == {"who": 2} and by_name(rows[T_LEADER]["arguments"]) == {"who": 1}
    for tid in (T_LEADER, T_OTHER):
        exprs = {e["expression"]: e for e in rows[tid]["expressions"]}
        assert value(exprs["counter"]) == 41 and exprs["counter"]["address"] == w.addr("counter")
        assert value(exprs["before + 1"]) == 42
    assert t["stop"] == stop["stop"]                                        # どの停止の時点の値か

    resumed = s.debug_run(wait_s=0)
    assert resumed["kind"] == "timeout" and resumed["running"] is True and not any(w.cores.halted())

    closed = s.debug_close()
    assert closed["cleanup_errors"] == [] and closed["breakpoints_removed"] == 1
    assert w.read_user(w.addr("race.c:11"), 4) == original                  # BKPT を戻した
    assert not any(w.cores.halted()) and not w.cores.hw_bps and w.cores.hdbgen is False
    assert s.debug_status()["closed"] is True


# --- 個別 ------------------------------------------------------------------------------------------------------------

def test_attach_refuses_bad_choices_and_a_missing_elf_before_touching_the_cores(world, tmp_path):
    for key, bad in (("thread_id", "tls"), ("kernel_isa", "riscv"), ("bp_unmapped", "guess"), ("max_halt_s", 0)):
        with pytest.raises(EngineError, match=key):
            world.s.debug_attach(**{**ATTACH, key: bad})
    with pytest.raises((EngineError, OSError)):
        world.s.debug_attach(**{**ATTACH, "elf": str(tmp_path / "nothing.elf")})
    assert not any(world.cores.halted()) and world.cores.resumed == []


def test_the_owner_is_one_debug_and_gdb_and_the_plain_operations_are_refused(world):
    world.s.debug_attach(**ATTACH)
    for call in (world.s.halt_all, world.s.resume_all, world.s.processes, world.s.recover, world.s.rescue,
                 lambda: world.s.threads(PID),
                 lambda: world.s.gdb_serve(pid=PID, port=0, skew_limit_ns=68, thread_id="stack", kernel_isa="arm",
                                           max_step_retries=8, bp_unmapped="error"),
                 lambda: world.s.debug_attach(**ATTACH)):
        with pytest.raises(Busy, match="linux.halt.debug.close"):
            call()
    assert world.s.status()["debug"]["pid"] == PID                          # status は読むだけなので通す
    world.s.debug_close()
    world.s.processes()                                                      # 閉じたら使える


def test_debug_methods_need_an_attach(world):
    with pytest.raises(EngineError, match="debug.attach"):
        world.s.debug_threads(variables=False, expressions=[], max_elements=16)


def test_reading_is_refused_while_the_cores_run(world):
    world.s.debug_attach(**ATTACH)
    world.s.debug_run(wait_s=0)
    for call in (lambda: world.s.debug_threads(variables=False, expressions=[], max_elements=16),
                 lambda: world.s.debug_read(expressions=["counter"], tid=T_LEADER, frame=0, max_elements=16)):
        with pytest.raises(EngineError, match="running"):
            call()
    h = world.s.debug_halt()
    assert h["kind"] == "int" and h["stop"]["cores_halted"] == [0, 1, 2, 3] and all(world.cores.halted())


def test_a_breakpoint_is_placed_and_removed_while_the_cores_run_and_they_run_again_after(world):
    """一般のデバッガと同じ: 走行中にブレークポイントを置ける。デバッガが短く全コアを止めて置き、また走らせる。"""
    w = world
    original = w.read_user(w.addr("race.c:11"), 4)
    w.s.debug_attach(**ATTACH)
    w.s.debug_run(wait_s=0)
    assert not any(w.cores.halted()) and w.s.debug_status()["brief_halts"]["count"] == 0

    bp = w.s.debug_break_insert(location="race.c:11", kind="sw")
    assert (bp["line"], bp["kind"]) == (11, "sw")
    assert not any(w.cores.halted()), "置いたあとは、また走っている"
    assert w.s.debug_status()["running"] is True
    assert w.read_user(bp["address"], 4) == BKPT[4]                       # 走ったまま、BKPT が書いてある
    assert w.s.debug_status()["brief_halts"]["count"] == 1

    w.s.debug_break_remove(id=bp["id"])
    assert not any(w.cores.halted()) and w.read_user(bp["address"], 4) == original
    facts = w.s.debug_status()["brief_halts"]
    assert facts["count"] == 2 and facts["total_s"] >= 0 and facts["max_s"] <= facts["total_s"]


def test_memory_is_read_while_the_cores_run_by_a_brief_halt_and_they_run_again_after(world):
    """画面のメモリパネルは自動更新で走行中も読む。デバッガが全コアを短く止めて読み、また走らせる。"""
    w = world
    addr = w.addr("counter")
    w.s.debug_attach(**ATTACH)
    w.s.debug_run(wait_s=0)
    assert not any(w.cores.halted())
    got = w.s.debug_memory(address=addr, length=4)
    assert got["data"] == w.read_user(addr, 4).hex() and got["reason"] is None
    assert not any(w.cores.halted()) and w.s.debug_status()["running"] is True
    assert w.s.debug_status()["brief_halts"]["count"] == 1


def test_a_breakpoint_placed_while_running_is_hit_afterwards(world):
    w = world
    w.s.debug_attach(**ATTACH)
    w.s.debug_run(wait_s=0)
    bp = w.s.debug_break_insert(location="race.c:11", kind="sw")
    w.hit(core=1)
    stop = w.s.debug_run(wait_s=5.0)
    assert (stop["kind"], stop["tid"], stop["breakpoint"]) == ("swbreak", T_OTHER, bp["id"])
    assert all(w.cores.halted())


def test_placing_a_breakpoint_while_halted_does_not_count_a_brief_halt(world):
    world.s.debug_attach(**ATTACH)
    world.s.debug_break_insert(location="race.c:11", kind="sw")
    assert all(world.cores.halted()) and world.s.debug_status()["brief_halts"]["count"] == 0


def test_a_timeout_leaves_every_core_running_and_the_next_run_keeps_waiting(world):
    world.s.debug_attach(**ATTACH)
    bp = world.s.debug_break_insert(location="add_one", kind="hw")
    assert bp["kind"] == "hw" and bp["address"] == world.symbols.after_prologue(world.symbols.resolve("add_one")) + BIAS
    first = world.s.debug_run(wait_s=0.05)
    assert first["kind"] == "timeout" and not any(world.cores.halted()) and first["stop"] is None
    resumes = len(world.cores.resumed)
    world.place(1, T_OTHER, "add_one")
    world.cores.states[1] = world.cores.states[1].__class__(**{**world.cores.states[1].__dict__,
                                                              "pc": bp["address"]})
    world.hit(core=1)
    second = world.s.debug_run(wait_s=5.0)
    assert (second["kind"], second["tid"], second["breakpoint"]) == ("hwbreak", T_OTHER, bp["id"])
    assert len(world.cores.resumed) == resumes


def test_a_function_breakpoint_says_whether_the_prologue_end_was_used(world):
    world.s.debug_attach(**ATTACH)
    bp = world.s.debug_break_insert(location="worker", kind="sw")
    start = world.symbols.resolve("worker")
    assert bp["prologue_end"] is world.symbols.has_prologue_end(start)
    assert bp["link_address"] == world.symbols.after_prologue(start)


def test_breakpoint_errors_say_which_location_and_why(world):
    world.s.debug_attach(**ATTACH)
    with pytest.raises(EngineError, match="race.c:7"):
        world.s.debug_break_insert(location="race.c:7", kind="sw")            # 命令の無い行
    with pytest.raises(EngineError, match="no_such_function"):
        world.s.debug_break_insert(location="no_such_function", kind="sw")
    with pytest.raises(EngineError, match="instruction set"):
        world.s.debug_break_insert(location="0x7000000", kind="sw")          # ELF のどの関数の中でもない
    with pytest.raises(EngineError, match="kind"):
        world.s.debug_break_insert(location="add_one", kind="soft")
    first = world.s.debug_break_insert(location="race.c:11", kind="sw")
    world.unmap(world.addr("worker"))
    world.s._debug.engine.mem.invalidate()
    with pytest.raises(EngineError, match="not mapped.*bp_unmapped=error"):
        world.s.debug_break_insert(location="worker", kind="sw")              # 未マップ（bp_unmapped = error）
    with pytest.raises(EngineError, match="already"):
        world.s.debug_break_insert(location="race.c:11", kind="sw")
    removed = world.s.debug_break_remove(id=first["id"])
    assert removed["removed"]["id"] == first["id"] and world.s.debug_status()["breakpoints"] == []
    with pytest.raises(EngineError, match="no breakpoint"):
        world.s.debug_break_remove(id=first["id"])


def test_read_memory_and_a_failing_expression_report_facts_per_item(world):
    world.s.debug_attach(**ATTACH)
    r = world.s.debug_read(expressions=["counter", "nonexistent + 1", "&counter"], tid=T_OTHER, frame=0,
                           max_elements=16)
    assert r["frame"]["function"] == "add_one" and r["frame"]["line"] == 11
    values = {v["expression"]: v for v in r["values"]}
    assert value(values["counter"]) == 41 and "nonexistent" in values["nonexistent + 1"]["error"]
    assert values["&counter"]["value"]["address"] == world.addr("counter")
    caller = world.s.debug_read(expressions=["i"], tid=T_OTHER, frame=1, max_elements=16)
    assert caller["frame"]["function"] == "worker" and value(caller["values"][0]) == 7
    m = world.s.debug_memory(address=world.addr("counter"), length=4)
    assert bytes.fromhex(m["data"]) == struct.pack("<I", 41) and m["reason"] is None
    gone = world.s.debug_memory(address=0x7000000, length=4)
    assert gone["data"] is None and "not mapped" in gone["reason"]
    with pytest.raises(EngineError, match="tid"):
        world.s.debug_read(expressions=["counter"], tid=999, frame=0, max_elements=16)


def test_a_thread_off_the_cores_is_read_from_its_saved_user_registers(world):
    world.cores.states[3] = world.cores.states[0]                         # スレッド 100 はどのコアにもいない
    from fprobe.linux_halt.kernel import Kernel
    from .test_engine import EXTRA
    from .test_kernel import layout
    k = Kernel(world.m.read, layout(**EXTRA), max_tasks=100, max_threads=16)
    regs = [0] * 18
    regs[13], regs[15], regs[16] = SP[T_LEADER], world.addr("race.c:10"), 0x10
    task = next(t for t in k.threads(k.find_leader(PID).addr) if t.pid == T_LEADER)
    pt = k.pt_regs_addr(task)
    world.m.b[pt - 0xC0000000:pt - 0xC0000000 + 72] = struct.pack("<18I", *regs)
    world.s.debug_attach(**ATTACH)
    rows = {r["tid"]: r for r in world.s.debug_threads(variables=True, expressions=[], max_elements=16)["threads"]}
    assert rows[T_LEADER]["on_core"] is None and rows[T_LEADER]["registers_from"] == "pt_regs"
    assert rows[T_LEADER]["line"] == 10 and by_name(rows[T_LEADER]["locals"])["before"] == 41


def test_the_cores_are_not_left_halted_longer_than_max_halt_s(world):
    world.s.debug_attach(**{**ATTACH, "max_halt_s": 0.2})
    assert all(world.cores.halted())
    deadline = time.monotonic() + 5
    while all(world.cores.halted()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(world.cores.halted())
    st = world.s.debug_status()
    assert st["running"] is True and len(st["auto_resumed"]) == 1 and st["auto_resumed"][0]["halted_s"] >= 0.2


def test_each_call_starts_the_max_halt_count_again(world):
    world.s.debug_attach(**{**ATTACH, "max_halt_s": 0.3})
    for _ in range(4):
        time.sleep(0.15)
        world.s.debug_threads(variables=False, expressions=[], max_elements=16)
    assert all(world.cores.halted()) and world.s.debug_status()["auto_resumed"] == []


def test_closing_the_session_closes_the_debugger_and_removes_the_breakpoints(world):
    world.s.debug_attach(**ATTACH)
    bp = world.s.debug_break_insert(location="race.c:11", kind="sw")
    original = world.symbols.segments                                       # （読むだけ）
    world.s.close()
    assert world.read_user(bp["address"], 4) != BKPT[4] and not any(world.cores.halted()) and world.closed == 1
    assert original


def test_line_step_over_runs_a_call_to_its_return_and_stops_on_the_next_line(world):
    """worker の race.c:19（add_one(who) の呼び出し）から over で 1 行: bl で add_one に入ったら戻り先まで走らせ、
    次の文の始まり（race.c:18 の i++）で止まる。"""
    s, w = world.s, world
    s.debug_attach(**ATTACH)
    sym = w.symbols
    call = sym.resolve("race.c:19")                                        # ldr r0, [sp, #8] ; bl add_one
    w.cores.states[1] = w.cores.states[1].__class__(**{**w.cores.states[1].__dict__, "pc": call + BIAS})
    s._debug.engine.refresh()
    path = {call: call + 4, call + 4: sym.resolve("add_one")}               # 1 命令ずつの行き先（bl で add_one へ）

    def on_step(core):
        st = w.cores.states[core]
        nxt = path[st.pc - BIAS] + BIAS
        w.cores.states[core] = st.__class__(**{**st.__dict__, "pc": nxt})
        w.cores.reg_lists[core][15] = nxt
        if nxt - BIAS == sym.resolve("add_one"):
            w.cores.reg_lists[core][14] = call + 8 + BIAS                  # bl が lr に戻り先を入れた
    w.cores.on_step = on_step

    def returned():                                                         # 戻り先の一時ブレークポイントに当たる
        st = w.cores.states[1]
        w.cores.states[1] = st.__class__(**{**st.__dict__, "pc": call + 8 + BIAS})
        w.cores.reg_lists[1][15] = call + 8 + BIAS
        w.hit(core=1)
    original_resume = w.cores.resume

    def resume(cores):
        original_resume(cores)
        if w.cores.states[1].pc == sym.resolve("add_one") + BIAS:
            returned()
    w.cores.resume = resume
    r = s.debug_step(tid=T_OTHER, mode="over", max_instructions=16, timeout_s=5.0)
    assert r["kind"] == "step" and r["reached_new_line"] is True
    assert (r["function"], r["line"]) == ("worker", 18) and r["address"] == call + 8 + BIAS
    assert r["instructions"] == 2 and r["stepped_over_calls"] == 1 and all(w.cores.halted())


def test_line_step_stops_after_max_instructions_and_says_so(world):
    s, w = world.s, world
    s.debug_attach(**ATTACH)
    w.cores.on_step = lambda core: None                                     # pc が進まない
    r = s.debug_step(tid=T_OTHER, mode="into", max_instructions=3, timeout_s=5.0)
    assert r["kind"] == "step" and r["reached_new_line"] is False and r["instructions"] == 3
    assert r["line"] == 11


def test_a_run_reports_the_hits_it_did_not_count_as_the_targets_with_what_each_was_identified_as(world):
    """実機（2026-11-03、Haiku）: thread_id="contextidr" を PID_IN_CONTEXTIDR の無いカーネルで使うと、止まったコアの
    スレッドが存在しない番号に見え、自分の当たりが全部「対象外」として黙って捨てられた（8 秒で 134 回）。推測で方式を
    切り替えず、その run の間に対象外と判定した当たりの数と中身（コア・見分けた tid・番地）を事実として返す。"""
    s, w = world.s, world
    s.debug_attach(**{**ATTACH, "thread_id": "contextidr"})
    bp = s.debug_break_insert(location="race.c:11", kind="hw")
    w.hit(core=1)
    out = s.debug_run(wait_s=0.05)
    assert out["kind"] == "timeout"
    seen = out["while_running"]
    assert seen["thread_id_method"] == "contextidr"
    assert seen["target_threads"] == [T_LEADER, T_OTHER]
    assert seen["counters"]["foreign_bp_hits"] == 1
    hit = seen["foreign_hits"][0]
    assert hit["core"] == 1 and hit["address"] == bp["address"] and hit["tid"] not in (T_LEADER, T_OTHER)


def test_the_status_keeps_what_the_last_run_returned_and_the_threads_last_read_for_the_screen(world):
    """画面は考えない層: AI（MCP）が呼んだ run の結果（当たらなかった回も）と最後に読んだ全スレッドの値を、状態がそのまま
    持つ。画面は linux.halt.debug.status を読んで描くだけで、人にも AI と同じものが見える（2026-11-03、画面に何も出なかった）。"""
    s, w = world.s, world
    s.debug_attach(**ATTACH)
    assert s.debug_status()["last_run"] is None and s.debug_status()["last_threads"] is None
    bp = s.debug_break_insert(location="race.c:11", kind="sw")
    w.hit(core=1)
    stop = s.debug_run(wait_s=5.0)
    t = s.debug_threads(variables=True, expressions=["counter"], max_elements=16)
    st = s.debug_status()
    assert st["last_run"] == stop and st["last_run"]["breakpoint"] == bp["id"]
    assert st["last_threads"] == t


def test_line_step_over_a_call_that_does_not_return_stops_at_the_time_limit_and_says_so(world):
    """step over の呼び出しの踏み越え（run_to）にも、使う側が明示した時間の上限が効く（眠る呼び出しで作業スレッドを
    塞がない）。過ぎたら全コアを止め、一時的なブレークポイントを外して、timed_out = true とその時の位置を返す。"""
    s, w = world.s, world
    s.debug_attach(**ATTACH)
    sym = w.symbols
    call = sym.resolve("race.c:19")
    w.cores.states[1] = w.cores.states[1].__class__(**{**w.cores.states[1].__dict__, "pc": call + BIAS})
    s._debug.engine.refresh()
    path = {call: call + 4, call + 4: sym.resolve("add_one")}

    def on_step(core):
        st = w.cores.states[core]
        nxt = path[st.pc - BIAS] + BIAS
        w.cores.states[core] = st.__class__(**{**st.__dict__, "pc": nxt})
        w.cores.reg_lists[core][15] = nxt
        if nxt - BIAS == sym.resolve("add_one"):
            w.cores.reg_lists[core][14] = call + 8 + BIAS
    w.cores.on_step = on_step                                               # 戻り先には着かない（当たりが来ない）
    original = w.read_user(call + 8 + BIAS, 4)
    t0 = time.monotonic()
    r = s.debug_step(tid=T_OTHER, mode="over", max_instructions=16, timeout_s=0.2)
    assert time.monotonic() - t0 < 3.0
    assert r["timed_out"] is True and r["reached_new_line"] is False and r["kind"] == "step"
    assert r["function"] == "add_one" and all(w.cores.halted())
    assert w.read_user(call + 8 + BIAS, 4) == original                      # 一時的なブレークポイントを外した
    assert s.debug_status()["running"] is False


def test_instruction_steps_count_and_stop_at_the_time_limit(world):
    s, w = world.s, world
    s.debug_attach(**ATTACH)
    pc = w.addr("race.c:11")

    def on_step(core):
        st = w.cores.states[core]
        w.cores.states[core] = st.__class__(**{**st.__dict__, "pc": st.pc + 4})
        w.cores.reg_lists[core][15] = st.pc + 4
    w.cores.on_step = on_step
    r = s._debug.step_instructions(T_OTHER, 2, timeout_s=5.0)
    assert (r["kind"], r["steps_done"], r["timed_out"], r["address"]) == ("step", 2, False, pc + 8)


def test_once_the_process_is_seen_to_exit_every_operation_says_so_the_same_way(world):
    """終了を一度見たら固定する: step・読み・ブレークポイント・メモリは、以後同じ『プロセスは終了している』で断る。状態の読みと close は通る。
    （死んだタスクの task_struct が書き換わるまで古い値が読めて、step の成否が交互にぶれた: 実機 2026-11-05）"""
    s = world.s
    s.debug_attach(**ATTACH)
    d = s._debug
    d.last_stop = {"kind": "exit", "code": 11, "address": None}                  # 実行中に終了を見た後の状態
    for operation in (lambda: d.step(d.current_tid(), "over", 100, 5.0), lambda: d.step_instructions(d.current_tid(), 1, 5.0),
                      lambda: d.memory(0x10000, 4), lambda: d.break_insert("race.c:11", "sw")):
        with pytest.raises(EngineError, match=r"プロセス \d+ は終了している（exit, code 11 = exit status 0, killed by signal 11）"):
            operation()
    assert d.status_facts()["last_stop"]["kind"] == "exit"                       # 状態の読みは通る
    s.debug_close()                                                              # close は通る


def test_status_after_debug_close_shows_no_breakpoint_left(world):
    """close は BKPT を全部外す。その後の status に『閉じたのに breakpoints: 1』と出ない（haiku の試験: 手元の一覧は消えず、
    数がそのまま出て、まだ残っていると読めた）。"""
    world.s.debug_attach(**ATTACH)
    world.s.debug_break_insert(location="race.c:11", kind="sw")
    assert world.s.status()["debug"]["breakpoints"] == 1                     # 閉じる前は 1
    closed = world.s.debug_close()
    assert closed["breakpoints_removed"] == 1
    assert world.s.status()["debug"]["closed"] is True and world.s.status()["debug"]["breakpoints"] == 0
