"""Probe の RTOS の道具（fprobe.probe_rtos）: 止めて調べる（一覧・呼び出し履歴・C の式）と、タスクの時間軸
（方法 sampled: DAP サンプラ、方法 itm: トレースフック → SWO）。記録中は stream "rtos" に書く。
機器は使わない（freertos_demo.elf の像の上に組み立てた偽の FreeRTOS、偽のデバイス・リング・SWO）。"""

from types import SimpleNamespace

import pytest
from probe_fakes import FakeDevice, FakeTarget, use_fake_ring
from pyocd.core.target import Target
from rtos_kernel import FreeRTOSBuilder, shared

from fprobe import collector, probe_window
from fprobe import generated as g
from fprobe.mcp_server import Probe
from fprobe.profile import dwt
from fprobe.rtos.timeline import TRACE_PORT

TRACE_REGISTERS = (dwt.DEMCR, dwt.ITM_LAR, dwt.ITM_TCR, dwt.ITM_TER, dwt.TPIU_CSPSR, dwt.TPIU_ACPR, dwt.TPIU_SPPR,
                   dwt.TPIU_FFCR, dwt.DWT_CTRL)


@pytest.fixture
def kernel():
    """ctrl（キューの待ち）・sensor（実行中）・worker（遅延）・IDLE（実行可能）。"""
    k = FreeRTOSBuilder()
    k.set_tick(1234)
    h = {name: k.task(name, prio) for name, prio in [("ctrl", 3), ("sensor", 2), ("worker", 1), ("IDLE", 0)]}
    q = k.queue("cmd_queue")
    k.put_state(h["ctrl"], k.var("xSuspendedTaskList"))
    k.put_event(h["ctrl"], k.receivers(q))
    k.put_state(h["sensor"], k.ready(2))
    k.set_current(h["sensor"])
    k.put_state(h["worker"], k.var("xDelayedTaskList1"), 1300)
    k.put_state(h["IDLE"], k.ready(0))
    for address in TRACE_REGISTERS:
        k.u32(address, 0)
    k.handles = h
    return k


class FakeSwo:
    def __init__(self):
        self.started: list[int] = []

    def swo_stop(self):
        pass

    def swo_start(self, baud):
        self.started.append(baud)

    def swo_read(self):
        return b""


@pytest.fixture
def probe(tmp_path, monkeypatch, kernel):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    use_fake_ring(monkeypatch, probe_window, collector)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._symbols = shared()[1]
    target = FakeTarget(kernel.memory)
    p._debug = SimpleNamespace(target=target, probe=FakeSwo(), close=lambda: None)
    yield p
    if p._recorder is not None:
        p.recording_stop()


def itm_word(word: int) -> bytes:
    """ITM のスティミュラス・パケット（ポート 31、4 バイト）。"""
    return bytes([TRACE_PORT << 3 | 0b11]) + word.to_bytes(4, "little")


def exception_packet(number: int, action: int) -> bytes:
    """DWT の例外トレースのパケット（1 = 入る、2 = 出る、3 = 戻る）。"""
    return bytes([0x0E, number & 0xFF, action << 4 | number >> 8])


# --- 止めて調べる ------------------------------------------------------------------------------

def test_info_without_a_target_tells_the_kind_and_the_trace_hook(probe):
    probe._debug = None
    info = probe.rtos_info()
    assert info["rtos"] == "freertos" and info["trace_hook"] and not info["connected"] and info["timeline"] is None


def test_info_and_tasks_of_a_halted_target(probe, kernel):
    info = probe.rtos_info()
    assert info["halted"] and info["tasks"] == 4 and info["tick"] == 1234 and info["port"] == "armv7m_fp"
    assert info["current"] == {"handle": kernel.handles["sensor"], "name": "sensor"}
    tasks = {t["name"]: t for t in probe.rtos_tasks()["tasks"]}
    assert tasks["sensor"]["state"] == "running" and tasks["ctrl"]["wait"]["name"] == "cmd_queue"


def test_running_target_is_not_walked(probe):
    probe.target.state = Target.State.RUNNING
    with pytest.raises(RuntimeError, match="target_halt"):
        probe.rtos_tasks()
    info = probe.rtos_info()                                    # 1 語の読み（実行中のタスク・ティック）はできる
    assert not info["halted"] and "tasks" not in info and info["current"]["name"] == "sensor"


def test_backtrace_and_expressions_of_a_task_by_name_or_handle(probe, kernel):
    from test_rtos_freertos import arguments, exit_trap, functions
    h = kernel.handles["worker"]
    b = kernel.stack_builder(h)
    b.enter("worker_task", lr=exit_trap())
    b.call("worker_task", "recurse", 6, 0)
    b.call("recurse", "recurse", 5, 0x1234)
    kernel.switch_out(h, b, b.regs["pc"])
    bt = probe.rtos_backtrace("worker", variables=True)
    assert bt["task"] == "worker" and bt["state"] == "blocked"
    assert functions(bt["frames"]) == ["recurse", "recurse", "worker_task"]
    assert arguments(bt["frames"][0])["depth"] == 5
    assert probe.rtos_backtrace(f"0x{h:08X}")["task"] == "worker"
    read = probe.rtos_read("worker", "depth")
    assert read["task"] == "worker" and read["value"] == 5
    with pytest.raises(KeyError):
        probe.rtos_backtrace("nothing")


# --- カーネルのオブジェクト（U08） ----------------------------------------------------------------

def test_objects_of_a_halted_target(probe, kernel):
    out = probe.rtos_objects()
    assert out["rtos"] == "FreeRTOS" and out["halted"] and out["consistent"] and "notes" not in out
    objects = {o["name"]: o for o in out["objects"]}
    cmd = objects["cmd_queue"]
    assert cmd["kind"] == "queue" and cmd["source"] == "registry" and cmd["fields"]["length"] == 4
    assert [w["task"] for w in cmd["waiters"]] == ["ctrl"]
    assert out["heap"]["scheme"] == "heap_4" and out["contention"] == {"inversions": [], "deadlocks": []}
    only = probe.rtos_objects(kinds=["timer"])
    assert only["objects"] == []


def test_objects_and_tasks_while_running_need_live(probe, kernel):
    probe.target.state = Target.State.RUNNING
    with pytest.raises(RuntimeError, match="live"):
        probe.rtos_objects()
    with pytest.raises(RuntimeError, match="live"):
        probe.rtos_tasks()
    probe.target.registers["psp"] = 0x2000_0000                    # 走行中のレジスタは読まない（読めても意味が無い）
    out = probe.rtos_objects(live=True)
    assert not out["halted"] and not out["consistent"] and out["notes"]
    assert any(o["name"] == "cmd_queue" for o in out["objects"])
    tasks = probe.rtos_tasks(live=True)
    assert not tasks["consistent"] and tasks["notes"]
    sensor = next(t for t in tasks["tasks"] if t["name"] == "sensor")
    assert sensor["stack"]["pointer"] is None                       # 実行中のタスクの SP は分からない


# --- 時間軸: 方法 sampled ------------------------------------------------------------------------

def test_sampled_timeline_from_the_event_log(probe, kernel):
    dev, h = probe.session.device, kernel.handles
    started = probe.rtos_timeline_start(method="sampled", rate_hz=10_000)
    current = kernel.var("pxCurrentTCB")
    assert started["running"] and started["address"] == current and started["tasks_known"] == 4
    assert dev.sampler_running() and dev.csr[g.SAMPLER_PERIOD] == 10_000 and probe._sampler_user == "rtos"
    with pytest.raises(RuntimeError, match="rtos_timeline_stop"):
        probe.watch_set(["0x20000000"])                        # サンプラは時間軸が使っている
    late = kernel.task("late", 1)                               # 始めた後に作られたタスク: 名前は動作中に読む
    t0 = started["t0"]
    values = [h["IDLE"]] * 5 + [h["sensor"]] * 3 + [late] * 2 + [h["IDLE"]] * 2
    for i, v in enumerate(values):
        dev.sample(t0 + 100 + i * 10_000, current, v)
        dev.sample(t0 + 101 + i * 10_000, current + 4, 0xFFFF)  # 隣の語（別の番地）は使わない
    live = probe.rtos_timeline_get()
    assert live["running"] and [r["task"] for r in live["runs"]] == ["IDLE", "sensor", "late", "IDLE"]
    stopped = probe.rtos_timeline_stop()
    assert not dev.sampler_running() and probe._sampler_user is None
    stats = stopped["stats"]
    assert stopped["method"] == "sampled" and stopped["lower_bound"] and stats["switches"] == 3
    assert stats["tasks"]["sensor"]["runs"] == 1 and stats["tasks"]["sensor"]["total_us"] == pytest.approx(300)
    assert stopped["resolution_us"] == pytest.approx(100) and stopped["window"]["source"] == "event_log"
    assert not stopped["recorded"] and stopped["notes"]
    got = probe.rtos_timeline_get(offset=1, limit=2)            # 止めた後は最後の結果
    assert not got["running"] and [r["task"] for r in got["runs"]] == ["sensor", "late"] and got["runs_total"] == 4
    with pytest.raises(RuntimeError):
        probe.rtos_timeline_stop()


def test_sampled_timeline_is_written_to_the_recording(probe, kernel):
    dev, h = probe.session.device, kernel.handles
    probe.recording_start(name="rtos")
    started = probe.rtos_timeline_start(method="sampled", rate_hz=10_000)
    current = started["address"]
    for i, v in enumerate([h["IDLE"]] * 3 + [h["ctrl"]] * 3):
        dev.sample(started["t0"] + 10 + i * 10_000, current, v)
    stopped = probe.rtos_timeline_stop()
    assert stopped["recorded"] and stopped["window"]["source"] == "recording"
    rows = probe.recording_events(streams=["rtos"])["events"]
    assert [e["body"]["task"] for e in rows if e["kind"] == "run"] == ["IDLE", "ctrl"]
    switch = next(e for e in rows if e["kind"] == "switch")
    assert switch["body"] == {"from": "IDLE", "to": "ctrl"} and switch["uncertainty"] == pytest.approx(5000)


# --- 時間軸: 方法 itm --------------------------------------------------------------------------

def test_itm_timeline_enables_the_hook_and_reads_swo_bytes_with_device_times(probe, kernel):
    dev, h, target = probe.session.device, kernel.handles, probe.target
    started = probe.rtos_timeline_start(method="itm", swo_baud=2_000_000, core_clock_hz=16e6)
    flag = shared()[1].variable("fprobe_trace_enabled").address
    written = dict(target.write_log)
    assert written[flag] == 1 and written[dwt.TPIU_ACPR] == 7 and written[dwt.ITM_TER] == 0b111 << 29
    assert written[dwt.ITM_TCR] & 1 and probe._debug.probe.started == [2_000_000]
    assert started["resolution"] == pytest.approx(5 * 10 / 2e6 * 1e8)     # 1 語 = 5 バイト
    assert probe._sampler_user is None                                     # サンプラは使わない
    t0 = started["t0"]
    dev.swo(t0 + 1000, itm_word(h["sensor"] | 0))                         # sensor: 実行を始めた
    dev.swo(t0 + 5000, itm_word(h["sensor"] | 1))                         # やめた
    dev.swo(t0 + 5500, itm_word(h["IDLE"] | 0))
    dev.swo(t0 + 9000, itm_word(h["IDLE"] | 1))
    dev.swo(t0 + 9500, itm_word(h["sensor"] | 0))
    dev.advance(1000)
    stopped = probe.rtos_timeline_stop()
    assert (flag, 0) in target.write_log[-2:] and (dwt.DWT_CTRL, 0) in target.write_log[-2:]   # フックを止め、DWT_CTRL（同期パケット）を戻した
    assert stopped["method"] == "itm" and not stopped["lower_bound"]
    runs = probe.rtos_timeline_get()["runs"]
    assert [(r["task"], r["t0"] - t0) for r in runs] == [("sensor", 1000), ("IDLE", 5500), ("sensor", 9500)]
    assert runs[0]["t1"] - runs[0]["t0"] == 4000 and stopped["stats"]["switches"] == 2


def test_itm_timeline_writes_raw_task_events_to_the_recording(probe, kernel):
    dev, h = probe.session.device, kernel.handles
    probe.swo_start(4_000_000)
    probe.recording_start(name="itm")
    started = probe.rtos_timeline_start(method="itm")
    assert started["resolution"] == pytest.approx(5 * 10 / 4e6 * 1e8)    # swo_start の速度から
    dev.swo(started["t0"] + 100, itm_word(h["ctrl"] | 0) + itm_word(h["ctrl"] | 1))
    probe.rtos_timeline_stop()
    kinds = [(e["kind"], (e["body"] or {}).get("task")) for e in probe.recording_events(streams=["rtos"])["events"]]
    assert ("task_in", "ctrl") in kinds and ("task_out", "ctrl") in kinds and ("run", "ctrl") in kinds


def test_timeline_start_errors(probe):
    with pytest.raises(ValueError):
        probe.rtos_timeline_start(method="etm")
    probe.rtos_timeline_start(method="sampled")
    with pytest.raises(RuntimeError, match="rtos_timeline_stop"):
        probe.rtos_timeline_start(method="itm")
    probe.rtos_timeline_stop()
    probe._symbols = None
    with pytest.raises(RuntimeError, match="debug_elf_load"):
        probe.rtos_timeline_start()


# --- 時間軸の解析（U09） ---------------------------------------------------------------------------

def mutex_words(k, m):
    q, lst, item = k.queue_t, k.list, k.item
    holder = m + q.offset("u.xSemaphore.xMutexHolder")
    head = m + q.offset("xTasksWaitingToReceive") + lst.offset("xListEnd") + item.offset("pxNext")
    return holder, head, m + q.offset("xTasksWaitingToReceive") + lst.offset("xListEnd")


def test_sampled_timeline_reads_stacks_heap_and_mutex_words(probe, kernel):
    k, dev, h = kernel, probe.session.device, kernel.handles
    m = k.queue("log_mutex", "mutex", 1, 0, holder=h["worker"])
    k.heap_state(free=9000, minimum=8000, allocations=3, frees=0)
    k.u32(k.var("xIdleTaskHandles"), h["IDLE"])
    started = probe.rtos_timeline_start(method="sampled", rate_hz=10_000, memory=True, tasks=["worker", "sensor"],
                                        mutexes=["log_mutex"])
    assert started["memory"] == {"stacks": ["sensor", "worker"], "heap": True, "rate_hz": 10_000}
    assert started["mutexes"] == ["log_mutex"] and dev.sampler_running()
    current = started["address"]
    holder, head, empty = mutex_words(k, m)
    ctrl_item = h["ctrl"] + k.tcb.offset("xEventListItem")
    end = k.stack_end(h["worker"])
    heap_word = k.var("xFreeBytesRemaining")
    steps = [(h["worker"], h["worker"], empty, 100, 9000),
             (h["worker"], h["worker"], ctrl_item, 300, 8800),           # ctrl（3）が log_mutex を待ち始めた
             (h["sensor"], h["worker"], ctrl_item, 300, 8800),           # 間の sensor（2）が動く: 際限の無い逆転
             (h["worker"], h["worker"], ctrl_item, 200, 8800),
             (h["ctrl"], 0, empty, 200, 8600),                           # worker が返した
             (h["IDLE"], 0, empty, 200, 8600)]
    t0 = started["t0"] + 100
    for i, (cur, hold, waiting, used, free) in enumerate(steps):
        t = t0 + i * 10_000
        for j, (address, value) in enumerate([(current, cur), (holder, hold), (head, waiting),
                                              (h["worker"], end - used), (heap_word, free)]):
            dev.sample(t + j, address, value)
    mem = probe.rtos_timeline_memory(points=3)
    assert mem["stacks"]["worker"]["max_used"] == 300 and mem["stacks"]["worker"]["last_used"] == 200
    assert mem["heap"]["min_free"] == 8600 and mem["heap"]["total"] == 20 * 1024 - 8
    inv = probe.rtos_timeline_inversions()
    (ep,) = inv["episodes"]
    assert (ep["mutex"], ep["holder"], ep["waiter"]) == ("log_mutex", "worker", "ctrl")
    assert ep["unbounded"] and 9_990 <= ep["interference"]["sensor"] <= 10_000 and inv["duration_us"]["max"] > 0
    jobs = probe.rtos_timeline_jobs(task="worker")
    # 出来事が無い（sampled）ので、ジョブを優先度から推定しない。区間の事実（回数・時間・次に動いたタスク）だけ
    assert jobs["tasks"] == {} and jobs["jobs"] == [] and any("出来事" in n for n in jobs["notes"])
    worker = jobs["runs"]["worker"]
    assert worker["runs"] >= 1 and sum(worker["followed_by"].values()) == worker["runs"]
    assert jobs["task_runs"] and jobs["task_runs"][0]["exec"] == max(r["exec"] for r in jobs["task_runs"])
    assert not any("= 横取り" in n or "uncertain" in n for n in jobs["notes"])
    fine = probe.rtos_timeline_load(window_us=1)          # 窓が分解能より短くても経験則の注記は出さない（分解能は事実で返す）
    assert fine["resolution_us"] > 0 and not any("倍" in n for n in fine["notes"])
    load = probe.rtos_timeline_load(window_us=200)
    assert load["windows"][0]["tasks"]["worker"] > 0 and load["peak"]["load"] == pytest.approx(1.0)
    assert any("割り込み" in n for n in load["notes"])                  # 方法 sampled: 割り込みは見えない
    profile = probe.rtos_timeline_profile()
    assert profile["interrupts"] is None and profile["threads"]["IDLE"]["idle"]
    stopped = probe.rtos_timeline_stop()
    assert not dev.sampler_running() and probe._sampler_user is None and stopped["words_samples"] == 4 * len(steps)
    assert probe.rtos_timeline_memory()["stacks"]["worker"]["max_used"] == 300      # 止めた後も最後の結果


def test_itm_timeline_with_exception_trace_composes_interrupts(probe, kernel):
    dev, h, target, k = probe.session.device, kernel.handles, probe.target, kernel
    k.u32(k.var("xIdleTaskHandles"), h["IDLE"])
    probe.recording_start(name="isr")
    started = probe.rtos_timeline_start(method="itm", swo_baud=2_000_000, core_clock_hz=16e6, exceptions=True)
    written = dict(target.write_log)
    assert written[dwt.DWT_CTRL] & 1 << 16 and written[dwt.ITM_TCR] & 1 << 3 and started["exceptions"]
    assert probe._sampler_user is None                                   # memory・mutexes が無ければサンプラは使わない
    t0 = started["t0"]
    dev.swo(t0 + 1000, itm_word(h["sensor"] | 0))
    dev.swo(t0 + 2000, exception_packet(15, 1))                          # SysTick に入る
    dev.swo(t0 + 2500, exception_packet(15, 2))                          # 出る
    dev.swo(t0 + 2600, exception_packet(0, 3))                           # スレッドに戻る
    dev.swo(t0 + 5000, itm_word(h["sensor"] | 1))
    dev.swo(t0 + 5500, itm_word(h["IDLE"] | 0))
    dev.swo(t0 + 9000, itm_word(h["IDLE"] | 1))
    dev.swo(t0 + 9500, itm_word(h["sensor"] | 0))
    dev.advance(1000)
    profile = probe.rtos_timeline_profile()
    systick = profile["interrupts"]["SysTick"]
    assert (systick["count"], systick["time"]) == (1, 500) and profile["exception_trace"]["overflows"] == 0
    assert profile["threads"]["sensor"]["time"] >= 3500
    stopped = probe.rtos_timeline_stop()
    assert (dwt.DWT_CTRL, 0) in target.write_log[-2:]                    # 例外トレースを元に戻した
    assert stopped["exceptions"]["activations"] == 1
    rows = probe.recording_events(streams=["rtos"], kinds=["isr"])["events"]
    assert [(e["t"] - t0, e["t_end"] - t0, e["body"]["name"]) for e in rows] == [(2000, 2500, "SysTick")]
    measured = probe.rtos_timeline_interval({"stream": "rtos", "kind": "task_in", "a": h["sensor"]},
                                            {"stream": "rtos", "kind": "task_out", "a": h["sensor"]})
    first = measured["intervals"][0]
    assert (first["task"], first["wall"], first["own"]) == ("sensor", 4000, 3500)   # 割り込みを除く


def test_timeline_analysis_errors(probe, kernel):
    k, h = kernel, kernel.handles
    with pytest.raises(ValueError, match="itm"):
        probe.rtos_timeline_start(method="sampled", exceptions=True)
    with pytest.raises(KeyError, match="nothing"):
        probe.rtos_timeline_start(memory=True, tasks=["nothing"])
    with pytest.raises(KeyError, match="no_mutex"):
        probe.rtos_timeline_start(mutexes=["no_mutex"])
    for i in range(4):                                                    # 番地の離れたミューテックス 4 つ
        k.alloc(256)
        k.queue(f"m{i}", "mutex", 1, 0, holder=h["worker"])
    with pytest.raises(ValueError, match="8 か所"):
        probe.rtos_timeline_start(memory=True, mutexes=True)
    assert probe._sampler_user is None and probe._rtos_run is None
    probe.rtos_timeline_start()
    with pytest.raises(RuntimeError, match="memory=true"):
        probe.rtos_timeline_memory()
    assert not probe.rtos_timeline_inversions()["available"]
    probe.rtos_timeline_stop()


# --- カーネルの出来事（U10） -----------------------------------------------------------------------------

def stimulus(port: int, word: int) -> bytes:
    """ITM のスティミュラス・パケット（4 バイト）。"""
    return bytes([port << 3 | 0b11]) + word.to_bytes(4, "little")


def record(kind: str, *payload: int, arg: int = 0, ipsr: int = 0) -> bytes:
    """fprobe_trace.h のカーネルの出来事の記録（見出しはポート 30、中身はポート 29）。"""
    from fprobe.rtos.kernel_trace import EVENTS
    code = {name: c for c, name in EVENTS.items()}[kind]
    out = stimulus(30, code | len(payload) << 8 | ipsr << 10 | arg << 19)
    return out + b"".join(stimulus(29, w) for w in payload)


def test_itm_timeline_reads_kernel_events_flow_blocking_queues_and_exact_inversions(probe, kernel):
    dev, h, k = probe.session.device, kernel.handles, kernel
    q = dict(k.registry)["cmd_queue"]
    m = k.queue("log_mutex", "mutex", 1, 0)
    S = 1000
    probe.recording_start(name="kernel")
    started = probe.rtos_timeline_start(method="itm", swo_baud=2_000_000, core_clock_hz=16e6)
    assert dict(probe.target.write_log)[dwt.ITM_TER] == 0b111 << 29          # ポート 29・30・31
    t0 = started["t0"]
    for dt, data in [(100, itm_word(h["worker"] | 0)), (110, record("receive", m | 2, arg=1)),
                     (120, itm_word(h["worker"] | 1)),
                     (130, itm_word(h["ctrl"] | 0)), (140, record("block", m | 2)),
                     (141, record("prio_inherit", h["worker"], arg=3)), (150, itm_word(h["ctrl"] | 1)),
                     (160, itm_word(h["sensor"] | 0)), (170, record("send", q, arg=0)),
                     (180, itm_word(h["sensor"] | 1)),
                     (190, itm_word(h["worker"] | 0)), (200, record("send", m | 2, arg=0)),
                     (201, record("ready", h["ctrl"])), (210, itm_word(h["worker"] | 1)),
                     (220, itm_word(h["ctrl"] | 0)), (230, record("receive", m | 2, arg=1)),
                     (240, record("receive", q, arg=1))]:
        dev.swo(t0 + S * dt, data)                                         # 1 バイト 50 ティック: 間を空ける
    dev.advance(S * 1000)
    inv = probe.rtos_timeline_inversions()
    assert inv["source"] == "trace" and inv["count"] == 1
    ep = inv["episodes"][0]
    assert (ep["mutex"], ep["holder"], ep["waiter"], ep["t0"] - t0, ep["t1"] - t0) == ("log_mutex", "worker", "ctrl",
                                                                                         140 * S, 200 * S)
    assert ep["interference"] == {"sensor": 20 * S} and ep["inherited"]["priority"] == 3
    edges = {(e["from"], e["to"], e["op"]): e["count"] for e in probe.rtos_timeline_flow()["edges"]}
    assert edges[("sensor", "cmd_queue", "send")] == 1 and edges[("log_mutex", "ctrl", "take")] == 1
    waits = probe.rtos_timeline_blocking()["waits"]
    assert [(w["task"], w["on"], w["t0"] - t0, w["t1"] - t0, w["outcome"]) for w in waits] == [
        ("ctrl", "log_mutex", 140 * S, 201 * S, "ok")]
    queue = probe.rtos_timeline_queues()["objects"]["cmd_queue"]
    assert queue["length"] == 4 and queue["max"] == 1 and queue["max_usage"] == 0.25
    sends = probe.rtos_timeline_events(kinds=["send"])
    assert sends["total"] == 2 and [e["object"] for e in sends["events"]] == ["cmd_queue", "log_mutex"]
    assert probe.rtos_timeline_events(task="ctrl", kinds=["block"])["total"] == 1
    cost = probe.rtos_timeline_overhead()
    assert cost["words"] == 9 + 8 + 8 and cost["swo_share"] > 0 and "cpu_share" not in cost
    jobs = probe.rtos_timeline_jobs()
    assert any("ready" in n for n in jobs["notes"]) and jobs["runs"]          # ready → block のそろうタスクは無い
    probe.rtos_timeline_stop()
    kinds = {e["kind"] for e in probe.recording_events(streams=["rtos"])["events"]}
    assert {"send", "receive", "block", "ready", "prio_inherit", "blocked", "inversion"} <= kinds


def test_kernel_tools_need_kernel_events(probe):
    probe.rtos_timeline_start()
    with pytest.raises(RuntimeError, match="fprobe_trace.h"):
        probe.rtos_timeline_flow()
    assert probe.rtos_timeline_inversions()["source"] == "sampled"
    probe.rtos_timeline_stop()
