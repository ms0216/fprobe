"""Probe のヒープの道具（U27）: heap.scan（走査・差・呼び出し元）と heap.trace.*（ブレークポイントの malloc / free の
記録とリークの候補）。偽の CPU（freertos_demo の ELF の像と、台本どおりに止まる実行）で。"""

from types import SimpleNamespace

import pytest
from pyocd.core.target import Target
from rtos_kernel import FreeRTOSBuilder, shared

from fprobe.mcp_server import Probe
from heap_fakes import ALLOCATED

SP = 0x2000_7000


class HeapCpu:
    """台本（止まる点のレジスタ）どおりに動く CPU。ブレークポイントの無い点では止まらずに通り過ぎる。"""

    def __init__(self, memory, stops=()):
        self.memory = memory
        self.stops = list(stops)
        self.regs = {"pc": 0, "r0": 0, "r1": 0, "lr": 0, "sp": SP}
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.USER
        self.breakpoints: set[int] = set()
        self.passed: list[int] = []

    def read_memory_block8(self, address, length):
        return list(self.memory.read_memory(address, length))

    def write_memory_block8(self, address, data):
        self.memory.write_memory(address, bytes(data))

    def flush(self):
        pass

    def get_state(self):
        return self.state

    def get_halt_reason(self):
        return self.reason

    def read_core_register(self, name):
        return self.regs.get(name, 0)

    read_core_register_raw = read_core_register

    def read_core_registers_raw(self, names):
        return [self.read_core_register(n) for n in names]

    def write_core_register(self, name, value):
        self.regs[name] = value

    def set_breakpoint(self, address, kind=None):
        self.breakpoints.add(address)
        return True

    def remove_breakpoint(self, address):
        self.breakpoints.discard(address)

    def step(self):
        pass

    def resume(self):
        while self.stops:
            stop = self.stops.pop(0)
            if stop["pc"] in self.breakpoints:
                self.regs.update(stop)
                self.state, self.reason = Target.State.HALTED, Target.HaltReason.BREAKPOINT
                return
            self.passed.append(stop["pc"])
        self.state = Target.State.RUNNING

    def halt(self):
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.USER


def heap_4(k: FreeRTOSBuilder, layout):
    """ucHeap に heap_4 のブロックを並べる。使用中のブロックの本体の番地の列を返す。"""
    heap = k.var("ucHeap")
    start, end = (heap + 7) & ~7, (heap + 20 * 1024 - 8) & ~7
    a, free, pointers = start, [], []
    for size, used in layout:
        k.u32(a, 0)
        k.u32(a + 4, size | (ALLOCATED if used else 0))
        (pointers if used else free).append(a + 8 if used else a)
        a += size
    k.u32(a, 0)
    k.u32(a + 4, end - a)
    free.append(a)
    k.u32(end, 0)
    k.u32(end + 4, 0)
    previous = k.var("xStart")
    for f in free:
        k.u32(previous, f)
        previous = f
    k.u32(previous, end)
    k.u32(k.var("pxEnd"), end)
    total = sum(s for s, u in layout if not u) + end - a
    for name, value in (("xFreeBytesRemaining", total), ("xMinimumEverFreeBytesRemaining", total),
                        ("xNumberOfSuccessfulAllocations", len(pointers)), ("xNumberOfSuccessfulFrees", 0)):
        k.u32(k.var(name), value)
    return pointers


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    _, symbols = shared()
    k = FreeRTOSBuilder()
    p = Probe("unused")
    p._session = SimpleNamespace(device=None)
    p._symbols = symbols
    p.poll_s = 0.001

    def connect(stops=()):
        cpu = HeapCpu(k.memory, stops)
        p._debug = SimpleNamespace(target=cpu, probe=None, close=lambda: None)
        p.semihosting_configure(enabled=False)
        return cpu
    yield p, k, symbols, connect
    p._stop_observing()


def call_site(symbols, function: str, offset: int = 0x10) -> int:
    return symbols.symbol(function).address + offset


def test_scan_reports_usage_and_changes(setup):
    p, k, symbols, connect = setup
    pointers = heap_4(k, [(64, True), (96, False), (128, True)])
    connect()
    first = p.heap_scan(compare=True, blocks=10)
    h = first["heaps"][0]
    assert h["allocator"] == "freertos" and h["used_blocks"] == 2 and not h["issues"] and first["consistent"]
    assert h["changes"] == {"first": True} and len(h["blocks"]) == 4
    heap_4(k, [(64, True), (96, True), (128, True)])
    again = p.heap_scan(compare=True)["heaps"][0]["changes"]
    assert again["new_count"] == 1 and again["new"][0]["pointer"] == pointers[0] + 64 and again["gone_count"] == 0
    assert again["limit"] == 50 and again["new_truncated"] is False and again["gone_truncated"] is False


def test_scan_changes_say_when_the_lists_are_cut(setup):
    # 黙って切らない（総点検）: new・gone を上限 changes_limit で切ったこと・使った上限を返す（上限は引数で渡せる）
    p, k, _, connect = setup
    heap_4(k, [(64, False), (96, False), (128, False)])
    connect()
    p.heap_scan(compare=True)
    heap_4(k, [(64, True), (96, True), (128, True)])
    cut = p.heap_scan(compare=True, changes_limit=2)["heaps"][0]["changes"]
    assert cut["new_count"] == 3 and len(cut["new"]) == 2 and cut["new_truncated"] is True and cut["limit"] == 2
    heap_4(k, [(64, False), (96, False), (128, False)])
    gone = p.heap_scan(compare=True, changes_limit=2)["heaps"][0]["changes"]
    assert gone["gone_count"] == 3 and len(gone["gone"]) == 2 and gone["gone_truncated"] is True


def test_scan_needs_a_halted_target_unless_live(setup):
    p, k, _, connect = setup
    heap_4(k, [(64, True)])
    cpu = connect()
    cpu.state = Target.State.RUNNING
    with pytest.raises(RuntimeError, match="動いている"):
        p.heap_scan()
    assert not p.heap_scan(live=True)["consistent"]


def test_trace_records_allocations_returns_and_frees_and_finds_leaks(setup):
    p, k, symbols, connect = setup
    malloc, free = symbols.symbol("pvPortMalloc").address, symbols.symbol("vPortFree").address
    leaky, steady = call_site(symbols, "sensor_task"), call_site(symbols, "worker_task")
    base = k.var("ucHeap") + 8 + 4                                 # 本体の番地（heap_4 のブロックの頭 8 の後）
    stops = []
    for i in range(6):
        stops += [{"pc": malloc, "r0": 16, "lr": leaky | 1}, {"pc": leaky, "r0": base + 32 * i}]
        stops += [{"pc": malloc, "r0": 40, "lr": steady | 1}, {"pc": steady, "r0": base + 0x400},
                  {"pc": free, "r0": base + 0x400, "lr": steady + 8 | 1}]
    stops += [{"pc": free, "r0": base + 0x400, "lr": steady + 8 | 1}]  # 二重の解放
    cpu = connect(stops)
    started = p.heap_trace_start()
    assert {f["function"] for f in started["functions"]} == {"pvPortMalloc", "vPortFree"}
    assert any(b.get("tool") == "heap_trace" for b in p.breakpoint_list()["breakpoints"])
    stop = p.run_until_halt(timeout_s=0.5)
    assert stop["reason"] == "timeout" and not cpu.stops and not cpu.passed       # 戻り先でも全部止まった
    assert cpu.breakpoints == {malloc, free}                                       # 一時の戻り先は外した
    report = p.heap_trace_report()                                                # 既定は年齢で絞らない
    assert report["allocations"] == 12 and report["frees"] == 7 and report["live"]["count"] == 6
    assert report["peak_bytes"] == 6 * 16 + 40
    assert [i["kind"] for i in report["issues"]] == ["double_free"]
    top = report["live_by_caller"][0]
    assert top["caller"].startswith("sensor_task") and top["count"] == 6 and top["live_counts"][-1][1] == 6
    events = p.heap_trace_events(limit=3)
    assert [e["op"] for e in events["events"]] == ["alloc", "alloc", "free"] and events["next"] == 3
    assert events["events"][0]["size"] == 16 and events["events"][0]["pointer"] == base
    stopped = p.heap_trace_stop()
    assert stopped["live"]["count"] == 6 and cpu.breakpoints == set()


def test_the_caller_is_the_return_address_unless_the_user_names_functions_to_skip(setup, monkeypatch):
    # 名前の表（標準の確保関数の名前）で黙って呼び出し元を飛ばさない（総点検 Q3 の WRAPPERS）: 既定は戻り先の関数。
    # 飛ばす関数は使う側が skip_functions で明示し、飛ばしたフレームを出来事に出す
    from fprobe.debug import Debugger
    p, k, symbols, connect = setup
    malloc = symbols.symbol("pvPortMalloc").address
    wrapper, outer = call_site(symbols, "worker_task"), call_site(symbols, "sensor_task")
    frames = [SimpleNamespace(pc=malloc), SimpleNamespace(pc=wrapper), SimpleNamespace(pc=outer)]
    monkeypatch.setattr(Debugger, "backtrace", lambda self, max_frames=64, inspector=None: frames[:max_frames])
    script = [{"pc": malloc, "r0": 16, "lr": wrapper | 1}, {"pc": wrapper, "r0": 0x2000_1000}]
    connect(list(script))
    started = p.heap_trace_start()
    assert started["skip_functions"] == []
    p.run_until_halt(timeout_s=0.3)
    event = p.heap_trace_events()["events"][0]
    assert event["caller"].startswith("worker_task") and event["skipped"] == []
    p.heap_trace_stop()
    connect(list(script))
    started = p.heap_trace_start(skip_functions=["worker_task"])
    assert started["skip_functions"] == ["worker_task"]
    p.run_until_halt(timeout_s=0.3)
    event = p.heap_trace_events()["events"][0]
    assert event["caller"].startswith("sensor_task") and event["skipped"] == ["worker_task"]
    assert p.heap_trace_report()["skip_functions"] == ["worker_task"]


def test_the_report_tells_how_many_callers_there_were_when_cut_to_top(setup):
    # 黙って切らない（総点検 Q3）: live_by_caller を top 個に切ったら、呼び出し元の総数・切った印・使った top を返す
    p, k, symbols, connect = setup
    malloc = symbols.symbol("pvPortMalloc").address
    sites = [call_site(symbols, f) for f in ("sensor_task", "worker_task", "ctrl_task")]
    stops = []
    for i, site in enumerate(sites):
        stops += [{"pc": malloc, "r0": 16, "lr": site | 1}, {"pc": site, "r0": 0x2000_1000 + 0x40 * i}]
    connect(stops)
    p.heap_trace_start()
    p.run_until_halt(timeout_s=0.3)
    full = p.heap_trace_report()
    assert full["callers_total"] == 3 and full["truncated"] is False and full["top"] == 20
    cut = p.heap_trace_report(top=1)
    assert len(cut["live_by_caller"]) == 1 and cut["callers_total"] == 3 and cut["truncated"] is True
    assert cut["top"] == 1
    # 誤りと推移も切ったことを黙らない（点検 2 回目）: 上限は引数で渡せる
    assert full["issues_limit"] == 200 and full["issues_total"] == 0 and full["series_points"] == 512
    thin = p.heap_trace_report(series_points=2)
    assert len(thin["series"]) == 2 and thin["series_total"] == 3 and thin["series_downsampled"] is True


def test_scan_passes_the_names_the_user_gave_for_the_regions_and_the_break(setup, monkeypatch):
    # heap_5 の領域の表・newlib-nano の break の変数の名前は慣習で探さない（点検 2 回目 H4・H5）: 使う側が渡す
    import fprobe.probe_heap as probe_heap
    p, k, symbols, connect = setup
    heap_4(k, [(64, True)])
    connect()
    seen = []
    real = probe_heap.scan

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(probe_heap, "scan", spy)
    p.heap_scan(regions_symbol="appRegions", break_symbol="my_break")
    assert seen[-1]["regions_symbol"] == "appRegions" and seen[-1]["break_symbol"] == "my_break"


def test_scan_attributes_blocks_to_callers_while_tracing(setup):
    p, k, symbols, connect = setup
    pointers = heap_4(k, [(24, True), (24, True)])
    malloc = symbols.symbol("pvPortMalloc").address
    site = call_site(symbols, "ctrl_task")
    connect([{"pc": malloc, "r0": 16, "lr": site | 1}, {"pc": site, "r0": pointers[1]}])
    p.heap_trace_start(allocators=["freertos"])
    p.run_until_halt(timeout_s=0.3)
    callers = p.heap_scan()["heaps"][0]["callers"]
    assert {c["caller"].split("+")[0] for c in callers} == {"ctrl_task", "(記録に無い確保)"}


def test_trace_refuses_a_users_breakpoint_on_the_allocator_and_clear_detaches(setup):
    p, k, symbols, connect = setup
    connect()
    p.breakpoint_set(hex(symbols.symbol("pvPortMalloc").address))
    with pytest.raises(RuntimeError, match="外してから"):
        p.heap_trace_start()
    p.breakpoint_clear()
    p.heap_trace_start()
    p.breakpoint_clear()
    assert not p._break_hooks and not p.heap_trace_report()["active"]
    with pytest.raises(RuntimeError):
        p.heap_trace_stop()


def test_trace_events_go_to_the_recording(setup):
    p, k, symbols, connect = setup
    p._session = SimpleNamespace(device=_Clock())
    malloc = symbols.symbol("pvPortMalloc").address
    site = call_site(symbols, "fpu_task")
    connect([{"pc": malloc, "r0": 8, "lr": site | 1}, {"pc": site, "r0": 0x2000_1000}])
    p.recording_start(name="heap")
    p.heap_trace_start()
    p.run_until_halt(timeout_s=0.3)
    found = p.recording_find({"stream": "heap", "kind": "alloc"})
    assert found["count"] == 1 and found["events"][0]["a"] == 0x2000_1000 and found["events"][0]["b"] == 8
    p.recording_stop()


class _Clock:
    def __init__(self):
        self.ticks = 0

    def time(self):
        self.ticks += 1000
        return self.ticks

    def read32(self, address):
        return 0

    def capabilities(self):
        return SimpleNamespace(timebase_hz=100_000_000, board="fake", design="fprobe")
