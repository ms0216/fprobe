"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: RTOS（タスクの時間軸・CPU 負荷・オブジェクト）・Code Profile・Interrupts のデモの値（G05・G06）。

DemoProbe（fake_demo.py）に混ぜる mixin。DemoProbe の偽の RAM（self._mem）・本物の ELF（self._elf）・デモの記録
（self._elf._view。tests/demo_recording.py）・実行の模擬（self.counter・self.state）を使う。値の形は本物の Probe の
同じ名前のメソッドと同じにする（画面はデモと本物を区別しない）。

本物の解析の部品に偽の材料を入れて本物の結果を作る:

- タスクの時間軸: デモの記録の rtos の区間（run）・割り込み（isr）・抜け（gap）と止まっていた間から、本物の
  Timeline（方法 sampled は本物の timeline_from_samples に周期のサンプルを入れる）と Activation を作り、本物の
  Probe（self._elf）の _rtos_last に置いて rtos_timeline_get / load をそのまま呼ぶ。始めてからの実時間 1 秒が記録の
  1 秒（取っている間は区間が伸びる）。
- プロファイル: 記録のその時刻に動いていたもの（割り込み → default_handler、タスク → デモの ELF の関数）の命令の
  番地を PC サンプルにして、本物の Profiler・coverage・ExceptionProfile・aggregate を通す（方法 pcsr の時刻は
  タイムベース = 記録と同じ時間軸、itm はコアのクロック数）。
- オブジェクト: 本物のモデル（KernelObject・Task・HeapInfo）と本物の contention（優先度の逆転・デッドロック）。
"""

import bisect
import random
import time
from contextlib import contextmanager
from types import SimpleNamespace

from demo_recording import HALTS, HZ, ISRS, SPAN
from fprobe.api import NoData
from fprobe.probe_profile import _ProfileResult, _ProfileRun
from fprobe.probe_rtos import _TimelineResult, _TimelineRun
from fprobe.probe_window import EventWindow
from fprobe.profile import (Activation, ExceptionProfile, PcSample, PcSamples, Profiler, plan_itm_sampling,
                            plan_pcsr_sampling)
from fprobe.rtos.contention import contention
from fprobe.rtos.model import HeapInfo, KernelObject, StackInfo, Task, TaskState, WaitInfo, Waiter
from fprobe.rtos.timeline import Gap, Run, Switch, Timeline, timeline_from_samples

__all__ = ["RtosDemo"]

CORE_HZ = 84e6                          # デモのコアのクロック（STM32F401。方法 itm の時刻の単位）
ROWS = 100_000                          # 記録から読む行の上限
# 動いていたもの → デモの ELF の関数（PC サンプルの番地をその関数の命令から選ぶ）
CONTEXT = {"main": "tick", "sensor": "reached_milestone", "comm": "tick", "logger": "reached_milestone",
           "IDLE": "reset_handler"}
ISR_FUNCTION = "default_handler"
# サンプルの当たらない行（近似のカバレッジのデモ: tick の reached_milestone の呼び出しは 256 回に 1 回で、まず当たらない）
NEVER_SAMPLED = {("tick", 19)}
_HANDLE_TEXT = "0x"


class _DemoSession:
    """本物の Probe の session の代わり（方法 pcsr の結果がタイムベースの速さを読む。機器は開かない）。"""
    device = SimpleNamespace(capabilities=lambda: SimpleNamespace(timebase_hz=HZ))

    def close(self) -> None:
        pass


def _clip(runs: list[tuple[int, int, int, str]], gaps: list[tuple[int, int]]) -> list[tuple[int, int, int, str]]:
    """区間から抜け（記録の抜け・止まっていた間）を除く。"""
    out = []
    for t0, t1, handle, name in runs:
        pieces = [(t0, t1)]
        for g0, g1 in gaps:
            pieces = [p for a, b in pieces for p in ((a, min(b, g0)), (max(a, g1), b)) if p[1] > p[0]]
        out += [(a, b, handle, name) for a, b in pieces]
    return out


def _activations(isrs: list[tuple[int, int, int]]) -> list[Activation]:
    """割り込みの区間（入れ子あり）→ 回（自分の時間 = 含む時間 − 中に入れ子になった回の時間）。"""
    ordered = sorted(isrs, key=lambda x: (x[0], -x[1]))
    nested = [0] * len(ordered)
    stack: list[int] = []
    for i, (t0, t1, _) in enumerate(ordered):
        while stack and ordered[stack[-1]][1] <= t0:
            stack.pop()
        if stack and t1 <= ordered[stack[-1]][1]:
            nested[stack[-1]] += t1 - t0
        stack.append(i)
    return [Activation(n, t0, t1, t1 - t0, t1 - t0 - nested[i]) for i, (t0, t1, n) in enumerate(ordered)]


class _Context:
    """記録のその時刻に動いていたもの（いちばん内側の割り込み、無ければタスク）。"""

    def __init__(self, runs, isrs):
        self.runs, self.run_starts = runs, [r[0] for r in runs]
        self.isrs, self.isr_starts = sorted(isrs), [x[0] for x in sorted(isrs)]

    def at(self, t: int) -> tuple[str, int] | None:
        """("isr", 例外番号) / ("task", 名前) / None（止まっていた・抜け）。"""
        k = bisect.bisect_right(self.isr_starts, t) - 1
        for j in range(k, max(k - 4, -1), -1):          # 入れ子は浅い: 直前の数個に含まれる
            t0, t1, n = self.isrs[j]
            if t0 <= t < t1:
                return ("isr", n)
        k = bisect.bisect_right(self.run_starts, t) - 1
        if k >= 0 and self.runs[k][0] <= t < self.runs[k][1]:
            return ("task", self.runs[k][3])
        return None


class RtosDemo:
    def _init_rtos(self) -> None:
        """DemoProbe.__init__ から（ELF を読み、接続・停止した後）。"""
        self._rtos_clock = time.monotonic           # 試験が差し替える
        self._rtos_material: dict | None = None     # デモの記録から読んだ区間（初めて使うときに読む）
        self._demo_rtos: dict | None = None         # 時間軸: method・rate_hz・exceptions・started・stopped（記録の時刻）
        self._demo_profile: dict | None = None      # プロファイル: method・plan・started・stopped・samples
        self._profile_cache: tuple | None = None

    # --- 材料（デモの記録） -----------------------------------------------------------------------

    def _material(self) -> dict:
        if self._rtos_material is None:
            q = self._elf.recording_query
            runs = [tuple(r) for r in q("SELECT t, t_end, a, json_extract(body, '$.task') FROM events WHERE "
                                        "stream = 'rtos' AND kind = 'run' ORDER BY t", limit=ROWS)["rows"]]
            isrs = [tuple(r) for r in q("SELECT t, t_end, a FROM events WHERE stream = 'rtos' AND kind = 'isr' "
                                        "ORDER BY t", limit=ROWS)["rows"]]
            gaps = [(t0, t1, reason) for t0, t1, reason in
                    q("SELECT t, t_end, json_extract(body, '$.reason') FROM events WHERE stream = 'rtos' AND "
                      "kind = 'gap' ORDER BY t", limit=ROWS)["rows"]]
            gaps += [(h0, h1 if h1 is not None else SPAN, "target halted") for h0, h1, _ in HALTS]
            gaps.sort()
            switches = q("SELECT t, a, b FROM events WHERE stream = 'rtos' AND kind = 'switch' ORDER BY t",
                         limit=ROWS)["rows"]
            runs = _clip(runs, [(a, b) for a, b, _ in gaps])
            names = {handle: name for _, _, handle, name in runs}
            self._rtos_material = {"runs": runs, "isrs": isrs, "gaps": gaps, "switches": switches, "names": names,
                                   "context": _Context(runs, isrs)}
        return self._rtos_material

    def _upto(self, state: dict) -> int:
        """取っている間に進んだ記録の時刻（実時間 1 秒 = 記録の 1 秒、記録の終わりまで）。"""
        if state["stopped"] is not None:
            return state["stopped"]
        return min(SPAN, max(1, int((self._rtos_clock() - state["started"]) * HZ)))

    # --- タスクの時間軸 ---------------------------------------------------------------------------

    def rtos_timeline_start(self, method="sampled", rate_hz=20_000.0, ap=0, swo_baud=None, core_clock_hz=None,
                            memory=False, tasks=None, mutexes=None, exceptions=False, memory_rate_hz=1000.0,
                            kernel=True):
        if method not in ("sampled", "itm"):
            raise ValueError("method は sampled か itm")
        if exceptions and method != "itm":
            raise ValueError("exceptions（割り込みの区間）は method itm だけ（例外トレースは SWO に出る）")
        if self._demo_rtos is not None and self._demo_rtos["stopped"] is None:
            raise RuntimeError("時間軸を取っている（rtos_timeline_stop を先に）")
        self._demo_rtos = {"method": method, "rate_hz": rate_hz, "exceptions": exceptions,
                           "started": self._rtos_clock(), "stopped": None}
        self._elf._rtos_last = None
        out = {"running": True, "method": method, "t0": 0, "timebase_hz": HZ, "source": "recording",
               "tasks_known": len(self._material()["names"]), "warnings": []}
        if method == "sampled":
            out.update(address=0x20000004, rate_hz=rate_hz)
        else:
            out.update(port=31, resolution=1, exceptions=exceptions)
        return out

    def rtos_timeline_stop(self):
        state = self._demo_rtos
        if state is None or state["stopped"] is not None:
            raise RuntimeError("時間軸を取っていない（rtos_timeline_start）")
        state["stopped"] = self._upto(state)
        result = self._timeline_result()
        return self._elf._timeline_dict(result, 0, 0, recorded=False)

    def rtos_timeline_get(self, offset=0, limit=200):
        self._timeline_result()
        return self._elf.rtos_timeline_get(offset, limit)

    def rtos_timeline_load(self, window_us=10_000.0, limit=1000):
        self._timeline_result()
        return self._elf.rtos_timeline_load(window_us, limit)

    def _timeline_result(self) -> _TimelineResult:
        """いままで（止めた後は止めたとき）の時間軸を本物の Probe の最後の結果に置く。"""
        state = self._demo_rtos
        if state is None:
            raise NoData("時間軸が無い（rtos_timeline_start）")
        m, upto = self._material(), self._upto(state)
        names = m["names"]
        if state["method"] == "sampled":
            period = HZ / state["rate_hz"]
            ctx = m["context"]
            by_name = {v: k for k, v in names.items()}
            samples = []
            for k in range(int(upto / period)):
                t = int(k * period)
                k2 = bisect.bisect_right(ctx.run_starts, t) - 1      # サンプラはタスクの変数を読む（割り込みは見えない）
                if k2 >= 0 and ctx.runs[k2][0] <= t < ctx.runs[k2][1]:
                    samples.append((t, by_name[ctx.runs[k2][3]]))
            tl = timeline_from_samples(samples, names, period=period)
            activations = None
        else:
            runs = [Run(h, name, t0, min(t1, upto)) for t0, t1, h, name in m["runs"] if t0 < upto]
            tl = Timeline("itm", 0, upto, 1, False, runs,
                          [Switch(t, a or None, b) for t, a, b in m["switches"] if t < upto],
                          [Gap(a, min(b, upto), reason) for a, b, reason in m["gaps"] if a < upto])
            activations = (_activations([(t0, min(t1, upto), n) for t0, t1, n in m["isrs"] if t0 < upto])
                           if state["exceptions"] else None)
        window = EventWindow(0, True, t1=state["stopped"])
        run = _TimelineRun(state["method"], window, dict(names), HZ,
                           address=0x20000004 if state["method"] == "sampled" else None,
                           rate_hz=state["rate_hz"] if state["method"] == "sampled" else None,
                           resolution=tl.resolution, exceptions=state["exceptions"],
                           priorities={t["handle"]: t["priority"] for t in self.rtos_tasks()["tasks"]},
                           idle={h for h, name in names.items() if name == "IDLE"})
        result = _TimelineResult(tl, None, run, [], activations,
                                 {"inconsistencies": 0, "overflows": 0} if activations is not None else None)
        self._elf._rtos_last = result
        return result

    # --- オブジェクト・タスクの呼び出し履歴 --------------------------------------------------------

    def _task_models(self) -> list[Task]:
        out = []
        for t in self.rtos_tasks(live=True)["tasks"]:
            s = t["stack"]
            wait = WaitInfo(**t["wait"]) if "wait" in t else None
            out.append(Task(t["handle"], t["name"], t["priority"], TaskState(t["state"]),
                            StackInfo(s["base"], s["end"], s["pointer"], s["untouched"], s["fill"], s["overflow"]),
                            wait, number=t.get("number")))
        return out

    def rtos_objects(self, kinds=None, live=False):
        halted = self.state == "HALTED"
        if not halted and not live:
            raise RuntimeError("ターゲットが動いている: 止めてから（target_halt）。一覧は live=true で走行中にも読める"
                               "（一貫しないことがある）")
        tasks = self._task_models()
        by = {t.name: t for t in tasks}

        def waiter(name: str, reason: str = "receive") -> Waiter:
            return Waiter(name, by[name].handle, by[name].priority, reason)
        tick = 1000 + self.counter
        logger = by["logger"]
        objects = [
            KernelObject(0x20003000, "queue", "log_queue", "registry",
                         {"length": 16, "item_size": 4, "messages": 5, "free": 11}),
            KernelObject(0x20003040, "queue", "rx_queue", "registry",
                         {"length": 8, "item_size": 1, "messages": 0, "free": 8}, [waiter("comm")]),
            KernelObject(0x20003080, "mutex", "log_mutex", "registry",
                         {"available": False, "holder": {"task": "logger", "handle": logger.handle,
                                                         "priority": logger.priority, "base_priority": logger.priority,
                                                         "inherited": False}},
                         [waiter("sensor")]),
            KernelObject(0x200030C0, "binary_semaphore", "sensor_ready", "variable", {"count": 0, "max": 1},
                         aliases=["app.sensor_ready"]),
            KernelObject(0x20003100, "counting_semaphore", "tx_slots", "variable", {"count": 3, "max": 4}),
            KernelObject(0x20003140, "timer", "blink", "kernel",
                         {"period_ticks": 500, "auto_reload": True, "active": True, "expiry_tick": tick + 137,
                          "remaining_ticks": 137, "callback": "tick", "id": 0}),
            KernelObject(0x20003180, "timer", "watchdog", "kernel",
                         {"period_ticks": 100, "auto_reload": False, "active": False,
                          "callback": "reached_milestone", "id": 1}),
            KernelObject(0x200031C0, "event_group", "net_events", "variable", {"bits": 0x05}),
            KernelObject(0x20003200, "stream_buffer", "uart_rx_stream", "variable",
                         {"capacity": 127, "used": 23, "free": 104, "trigger_level": 1}),
            KernelObject(0x20003240, "queue", "0x20003240", "waiter",
                         {"length": 4, "item_size": 8, "messages": 4, "free": 0},
                         notes=["登録簿に無い: 待っているタスクのリストから見つけた"]),
        ]
        selected = objects
        if kinds:
            selected = [o for o in objects if any(o.kind == k or o.kind.endswith("_" + k) for k in kinds)]
        selected = sorted(selected, key=lambda o: (o.kind, o.name))
        heap = HeapInfo("heap_4", total=16384, free=5120, min_free=3712, allocations=42, frees=17, free_blocks=3,
                        largest_free_block=4096, address=0x20004000)
        out = {"rtos": "FreeRTOS", "objects": [o.to_dict() for o in selected], "heap": heap.to_dict(),
               "contention": contention(tasks, objects, 1), "priority_order": "higher_is_more_urgent",
               "warnings": [], "halted": halted, "consistent": halted}
        if not halted:
            out["notes"] = ["走行中に読んだ: リスト・数・持ち主は読む間に変わり得る（一貫しない）。実行中のタスクの SP は"
                            "分からない。正確な様子は target_halt してから"]
        return out

    def rtos_backtrace(self, task, variables=False, max_elements=64):
        tasks = self.rtos_tasks(live=True)["tasks"]
        key = int(task, 16) if isinstance(task, str) and task.strip().lower().startswith(_HANDLE_TEXT) else task
        found = next((t for t in tasks if t["name"] == key or t["handle"] == key), None)
        if found is None:
            raise KeyError(f"タスク {task!r} が無い")
        if found["state"] == "running":
            frames = self.backtrace(variables=variables, max_elements=max_elements)
        else:
            lines = {"sensor": [14, 19, 28], "comm": [18, 28], "logger": [13, 21, 28], "IDLE": [28]}
            top = found["stack"]["end"] - found["stack"]["used"]
            frames = []
            for i, line in enumerate(lines.get(found["name"], [28])):
                pc = self._line_address(line)
                where = self._elf._symbols.describe(pc)
                frame = {"index": i, "pc": pc, **self._elf._place_of(pc), "offset": where.offset,
                         "cfa": top + 0x20 * (i + 1), "note": ""}
                if variables:
                    frame.update(arguments=[], locals=[])
                frames.append(frame)
        return {"task": found["name"], "handle": found["handle"], "state": found["state"], "frames": frames}

    # --- プロファイル ----------------------------------------------------------------------------

    @contextmanager
    def _demo_session(self):
        saved = self._elf._session
        self._elf._session = _DemoSession()
        try:
            yield
        finally:
            self._elf._session = saved

    def profile_start(self, method="itm", rate_hz=10_000.0, core_clock_hz=None, swo_baud=2_000_000,
                      exception_trace=True, exception_rate_hz=1000.0, timestamps=None, ap=0):
        if method not in ("itm", "pcsr"):
            raise ValueError("method は itm か pcsr")
        if method == "itm" and core_clock_hz is None:
            raise ValueError("method itm には core_clock_hz（コアのクロック。DWT の間隔と SWO の分周に使う）が要る")
        if self._demo_profile is not None and self._demo_profile["stopped"] is None:
            raise RuntimeError("プロファイルを取っている（profile_stop を先に）")
        stamps = exception_trace if timestamps is None else timestamps
        if method == "itm":
            plan = plan_itm_sampling(core_clock_hz, rate_hz, swo_baud, exception_trace=exception_trace,
                                     exception_rate_hz=exception_rate_hz, timestamps=stamps)
        else:
            plan = plan_pcsr_sampling(rate_hz)
        run = _ProfileRun(method, plan, core_clock_hz, method == "itm" and exception_trace, method == "itm" and stamps)
        self._demo_profile = {"run": run, "started": self._rtos_clock(), "stopped": None,
                              "samples": self._profile_samples(run)}
        self._profile_cache = None
        self._elf._profile_last = None
        return {"running": True, "method": method, "plan": plan.as_dict()}

    def _profile_samples(self, run: _ProfileRun) -> list[tuple[int, PcSample | None]]:
        """記録の全体（2 秒）の PC サンプル（記録の時刻, サンプル）。止まっていた間は None（PCSR = 0xFFFFFFFF）。"""
        m = self._material()
        rng = random.Random(20261018)
        listing, _ = self._elf._disassembler(from_elf=True)
        describe = self._elf._symbols.describe
        code = {name: [i.address for i in listing.function(name).instructions
                       if i.kind == "code" and (name, describe(i.address).line) not in NEVER_SAMPLED]
                for name in {*CONTEXT.values(), ISR_FUNCTION}}
        period = HZ / run.plan.rate_hz
        scale = (run.core_clock_hz or HZ) / HZ
        out = []
        for k in range(int(SPAN / period)):
            t = int(k * period + rng.random() * period / 2)            # 周期のゆらぎ（本物のサンプラと同じく）
            what = m["context"].at(t)
            if what is None:
                out.append((t, None))
                continue
            kind, value = what
            pc = rng.choice(code[ISR_FUNCTION if kind == "isr" else CONTEXT[value]])
            if run.method == "itm":
                exception = (value if kind == "isr" else 0) if run.exception_trace else None
                out.append((t, PcSample(pc, round(t * scale) if run.timestamps else None, exception)))
            else:
                out.append((t, PcSample(pc, t)))
        return out

    def _profile_result(self) -> _ProfileResult:
        state = self._demo_profile
        if state is None:
            raise NoData("プロファイルが無い（profile_start）")
        upto = self._upto(state)
        if self._profile_cache is not None and self._profile_cache[0] == upto:
            result = self._profile_cache[1]
        else:
            run = state["run"]
            run.window = EventWindow(0, True, t1=state["stopped"])
            taken = [s for t, s in state["samples"] if t < upto]
            if run.method == "itm":            # 止まっていた間はサンプルが来ない
                samples = PcSamples(tuple(s for s in taken if s is not None), "itm", 0, 0, run.exception_trace)
            else:
                samples = PcSamples(tuple(s for s in taken if s is not None), "pcsr",
                                    sum(1 for s in taken if s is None))
            listing, _ = self._elf._disassembler(from_elf=True)
            profile = Profiler(self._elf._symbols, listing).profile(samples, names=dict(ISRS))
            exceptions = None
            if run.exception_trace:
                k = run.core_clock_hz / HZ
                acts = _activations([(t0, min(t1, upto), n) for t0, t1, n in self._material()["isrs"] if t0 < upto])
                scaled = [Activation(a.number, round(a.start * k), round(a.end * k), round(a.inclusive * k),
                                     round(a.self_time * k)) for a in acts]
                exceptions = ExceptionProfile(scaled, round(upto * k), run.core_clock_hz, names=dict(ISRS))
            result = _ProfileResult(run, samples, profile, exceptions)
            self._profile_cache = (upto, result)
        self._elf._profile_last = result
        return result

    def _running(self, out: dict) -> dict:
        out["running"] = self._demo_profile is not None and self._demo_profile["stopped"] is None
        return out

    def profile_stop(self, top=10):
        state = self._demo_profile
        if state is None or state["stopped"] is not None:
            raise RuntimeError("プロファイルを取っていない（profile_start）")
        state["stopped"] = self._upto(state)
        result = self._profile_result()
        with self._demo_session():
            return self._running(self._elf._profile_summary(result, "function", top, None))

    def profile_result(self, by="function", top=20, function=None):
        self._profile_result()
        with self._demo_session():
            return self._running(self._elf.profile_result(by, top, function))

    def profile_coverage(self, functions=None, all_functions=False):
        self._profile_result()
        return self._elf.profile_coverage(functions, all_functions)

    def profile_exceptions(self):
        self._profile_result()
        return self._elf.profile_exceptions()

    def profile_aggregate(self, by="function", groups=None, t0_s=None, t1_s=None, t0=None, t1=None, top=20,
                          function=None):
        self._profile_result()
        with self._demo_session():
            return self._elf.profile_aggregate(by, groups, t0_s, t1_s, t0, t1, top, function)

    # --- 記録・ELF（本物の Probe にそのまま） -------------------------------------------------------

    # 記録の読み（recording_find・recording_query）は fake_demo_search.py（SearchDemo）

    def stack_static(self, su_paths=None, indirect=None, assume_address_taken=False, roots=None, entry=None,
                     exception_frame=None, function=None, top=50):
        return self._elf.stack_static(su_paths, indirect, assume_address_taken, roots, entry, exception_frame,
                                      function, top)
