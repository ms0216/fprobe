"""Probe の道具: RTOS の認識（F6）。止まったターゲットのタスクの一覧・タスクごとの呼び出し履歴と C の式、
止めずに見るタスクの時間軸。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・session・_symbols・_debug・_recorder・
_swo_baud・swo_start と、WindowTools（DAP サンプラの持ち主・出来事の窓・write_registers）を使う。
機器に触るのは必ず _lock の中。

- RTOS の読み手（fprobe.rtos.open_rtos）は呼ぶたびに作る（リストは止まっている間しか一貫しない。DWARF の索引は
  Debugger が ELF ごとに共有するので、作り直しは安い）。
- 時間軸の方法 sampled（組み込み無し）: DAP サンプラで実行中のタスクの変数（FreeRTOS は pxCurrentTCB）を周期的に読む。
  方法 itm（組み込みあり）: ファームのトレースフック（fprobe_trace.h）が ITM のポート 31 に書く 1 語を、SWO の
  バイトのデバイスの時刻で読む。どちらも窓（記録中なら記録、そうでなければイベント記録）から読み、SWO の受信の
  エンドポイント（swo_read / swo_decode の読む口）は読まない（二重に読まない）。
- 止めたとき記録中なら、時間軸を共通の時間軸の記録の stream "rtos" に書く（run / switch / gap、方法 itm は生の出来事も、
  例外トレースがあれば割り込みの区間 isr も）。
- 時間軸と一緒に読むもの（U09）: memory = タスクの保存された SP とヒープの残量の語、mutexes = ミューテックスの持ち主と
  待ちの先頭の語（DAP サンプラ。sampled はいまのタスクの語と同じサンプラ、itm はサンプラだけ memory_rate_hz で）、
  exceptions = 例外トレース（方法 itm の SWO に一緒に流す）。解析の道具は probe_rtos_analysis（RtosAnalysisTools）。
- 方法 itm はカーネルの出来事（U10。fprobe_trace.h の記録: ITM のポート 30・29）も読む: キュー・セマフォ・ミューテックスの
  操作・準備完了・待ち・優先度・割り込みの出入り。始めにオブジェクトの一覧を走行中に読み（名前・種類・長さ）、止めたとき
  記録に出来事・待ちの区間（blocked）・逆転の区間（inversion）を書く。

仕様: docs/superpowers/specs/2026-10-17-f6-rtos-design.md（9 節が組み込みの提案）、
docs/superpowers/specs/2026-10-18-u08-u09-rtos-objects-timeline-design.md（オブジェクト・時間軸の解析）、
docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md（カーネルの出来事）
"""

import re
from dataclasses import dataclass, field

from pyocd.core.target import Target

from .api import NoData
from .debug import Debugger, PyocdAccess
from .dwarf.access import AccessError
from .probe_window import EventWindow, write_registers
from .profile.dwt import DWT_CTRL, ITM_TER, RegisterWrite, exception_trace_writes, itm_port_writes, plan_swo
from .profile.exceptions import Activation, exception_name
from .rtos import detect, open_rtos
from .rtos.analysis import MemorySlot, MutexWatch, exception_activations
from .rtos.base import KernelAwareness
from .rtos.contention import contention
from .rtos.flow import blocking, inversions_from_ops
from .rtos.kernel_trace import KERNEL_PORT, PAYLOAD_PORT, KernelTrace, kernel_trace, object_table
from .rtos.model import Task, TaskState
from .rtos.timeline import (STREAM, TRACE_PORT, TaskEvent, Timeline, itm_packets, task_events, timeline_from_events,
                            timeline_from_samples, write_task_events, write_timeline)
from .sampler import MAX_SLOTS, Sampler, plan_slots

__all__ = ["RtosTools", "TRACE_FLAG"]

TRACE_FLAG = "fprobe_trace_enabled"      # fprobe_trace.h: デバッガが 1 を書くとトレースフックが ITM に書く
ITM_WORD_BYTES = 5                       # 4 バイトのスティミュラスのパケット（ヘッダ + 4）
RUNS_LIMIT = 200                         # rtos_timeline_get が 1 回に返す区間の数の既定
_HANDLE = re.compile(r"0x[0-9a-fA-F]+")
_DURATIONS = ("total", "max_run", "min_run", "mean_run")


class _MemoryOnly:
    """動いているターゲットの読み手: メモリは読み、コアのレジスタは読まない（AccessError）。"""

    def __init__(self, access: PyocdAccess):
        self.access = access

    def read_memory(self, address: int, length: int) -> bytes:
        return self.access.read_memory(address, length)

    def read_register(self, name: str) -> int:
        raise AccessError(f"レジスタ {name}: ターゲットが動いている")


@dataclass
class _TimelineRun:
    """動いている時間軸の取り方。"""
    method: str                          # sampled / itm
    window: EventWindow
    names: dict[int, str]                # TCB の番地 → 名前（始めたときの一覧 + 後で見つけた番地）
    timebase_hz: float
    address: int | None = None           # sampled: 実行中のタスクの変数の番地
    rate_hz: float | None = None         # sampled: サンプルの周期
    flag: int | None = None              # itm: fprobe_trace_enabled の番地
    resolution: float = 0                # itm: 時刻の分解能（タイムベース）
    warnings: list[str] = field(default_factory=list)
    order: int = 1                       # 優先度の向き（1 = 大きい方が高い）
    priorities: dict[int, int] = field(default_factory=dict)     # TCB → 元の優先度（ジョブ・逆転の判定）
    idle: set[int] = field(default_factory=set)                  # アイドルのタスクの TCB
    slots: dict[int, MemorySlot] = field(default_factory=dict)   # memory: 語の番地 → 意味
    mutexes: list[MutexWatch] = field(default_factory=list)      # mutexes: 持ち主・待ちの先頭の語
    sampler: bool = False                # DAP サンプラを使っている（sampled は常に）
    memory_rate_hz: float | None = None  # itm でサンプラを使うときの周期
    exceptions: bool = False             # itm: 例外トレースも流している
    baud: float | None = None            # itm: SWO の速度（帯域の見積もり）
    core_clock_hz: float | None = None   # itm: コアのクロック（フックの負荷の見積もり）
    objects: dict[int, dict] = field(default_factory=dict)       # itm: オブジェクトの番地 → {name, kind, length}
    restore: dict[int, int] = field(default_factory=dict)        # 止めるときに戻す（番地 → 元の値）

    @property
    def words(self) -> set[int]:
        """いまのタスクの語のほかに、サンプラで読んで使う語。"""
        return set(self.slots) | {a for w in self.mutexes for a in (w.holder_word, w.head_word)}


@dataclass
class _TimelineResult:
    timeline: Timeline
    events: list[TaskEvent] | None       # itm の生の出来事
    run: _TimelineRun
    samples: list[tuple[int, int, int]] = field(default_factory=list)   # memory・mutexes の語の (時刻, 番地, 値)
    activations: list[Activation] | None = None                         # itm + exceptions: 例外の回
    exception_issues: dict | None = None                                # 並びの合わない数・あふれの数
    kernel: KernelTrace | None = None                                   # itm: カーネルの出来事（U10）

    @property
    def has_kernel(self) -> bool:
        """カーネルの出来事（ポート 31 以外の記録）がある。"""
        return self.kernel is not None and any(op.kind != "lost" for op in self.kernel.ops)

    def objects(self) -> dict[int, dict]:
        """オブジェクトの表（始めの一覧 + object_create の出来事）。"""
        return object_table(self.kernel, self.run.objects) if self.kernel is not None else dict(self.run.objects)


class RtosTools:
    """RTOS の認識（Probe の mixin）。"""

    def _init_rtos(self) -> None:
        self._rtos_run: _TimelineRun | None = None
        self._rtos_last: _TimelineResult | None = None

    # --- RTOS の読み手 -----------------------------------------------------------------------

    def _rtos_debug_info(self):
        if self._symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load）")
        info = Debugger(None, self._symbols).debug_info()
        if info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: RTOS の構造体が分からない")
        return info

    def _kernel(self, halted: bool = True) -> KernelAwareness:
        """RTOS の読み手（_lock の中で）。halted なら、止まっていなければ誤り。動いているターゲットの読み手は
        メモリだけを読む（コアのレジスタは止めないと意味が無い: 実行中のタスクの SP は分からないとする）。"""
        info = self._rtos_debug_info()
        target = self.target
        running = target.get_state() != Target.State.HALTED
        if halted and running:
            raise RuntimeError("ターゲットが動いている: 止めてから（target_halt）。一覧は live=true で走行中にも読める"
                               "（一貫しないことがある）")
        access = _MemoryOnly(PyocdAccess(target)) if running else PyocdAccess(target)
        kernel = open_rtos(info, access, self._symbols)
        if kernel is None:
            raise RuntimeError("ELF に RTOS（FreeRTOS / Zephyr / RTX5 / ThreadX / µC/OS-III / NuttX）が見つからない")
        return kernel

    @staticmethod
    def _task_key(task: str | int) -> str | int:
        """タスクの指定: 名前か TCB の番地（整数か "0x..."）。"""
        if isinstance(task, str) and _HANDLE.fullmatch(task.strip()):
            return int(task, 16)
        return task

    def _trace_flag(self) -> int | None:
        try:
            return self._symbols.variable(TRACE_FLAG).address if self._symbols is not None else None
        except KeyError:
            return None

    # --- 止めて調べる ------------------------------------------------------------------------

    def rtos_info(self) -> dict:
        """ELF の RTOS の種類と、接続していればスケジューラの状態・ティック・実行中のタスク（止まっていればタスクの数）。
        トレースフック（方法 itm）の有無と、時間軸を取っているか。"""
        with self._lock:
            info = self._rtos_debug_info()
            kind = detect(info)
            out = {"rtos": kind, "elf": str(self._symbols.path), "trace_hook": self._trace_flag() is not None,
                   "timeline": self._timeline_state(), "connected": self._debug is not None}
            if kind is None or self._debug is None:
                return out
            kernel = self._kernel(halted=False)
            halted = self.target.get_state() == Target.State.HALTED
            current = kernel.current()
            out.update(name=kernel.name, halted=halted, scheduler=kernel.scheduler_state(), tick=kernel.tick_count(),
                       current={"handle": current, "name": kernel.handle_name(current) if current else None})
            if getattr(kernel, "port", None) is not None:
                out["port"] = kernel.port
            if halted:
                out["tasks"] = len(kernel.tasks())
            return out

    def rtos_tasks(self, live: bool = False) -> dict:
        """タスクの一覧（名前・優先度・状態・待っているもの・スタックの使用量とあふれの印）。止まっているときだけ。
        live なら走行中にも読む（止めずに読むのでリストが途中で変わり得る: consistent = false）。"""
        with self._lock:
            kernel = self._kernel(halted=not live)
            return self._consistency(kernel.snapshot())

    def rtos_objects(self, kinds: list[str] | None = None, live: bool = False) -> dict:
        """カーネルのオブジェクト（キュー・セマフォ・ミューテックスと持ち主・タイマ・イベントグループ・ストリーム /
        メッセージバッファ。Zephyr は k_* の各種）と、ヒープの要約、止めた瞬間の優先度の逆転・デッドロック。
        登録簿に無い・名前の無いものも、変数の型・待っているタスクから番地で出す。kinds で種類を絞る。
        止まっているときだけ（live なら走行中にも: consistent = false）。"""
        with self._lock:
            kernel = self._kernel(halted=not live)
            tasks = kernel.tasks()
            objects = kernel.objects(tasks=tasks)
            heap = kernel.heap()
            out = {"rtos": kernel.name, "objects": [o.to_dict() for o in kernel.select(objects, kinds)],
                   "heap": None if heap is None else heap.to_dict(),
                   "contention": contention(tasks, objects, kernel.priority_order),
                   "priority_order": "higher_is_more_urgent" if kernel.priority_order > 0 else "lower_is_more_urgent",
                   "warnings": list(kernel.warnings)}
            return self._consistency(out)

    def _consistency(self, out: dict) -> dict:
        """止まっているか・一貫しているか（_lock の中で）。"""
        halted = self.target.get_state() == Target.State.HALTED
        out.update(halted=halted, consistent=halted)
        if not halted:
            out["notes"] = ["走行中に読んだ: リスト・数・持ち主は読む間に変わり得る（一貫しない）。実行中のタスクの SP は"
                            "分からない。正確な様子は target_halt してから"]
        return out

    def rtos_backtrace(self, task: str | int, variables: bool = False, max_elements: int = 64) -> dict:
        """タスク（名前か TCB の番地）の呼び出し履歴。止まっているタスクは保存された文脈から、実行中のタスクは
        実レジスタから巻き戻す。variables なら各段の引数とローカル変数も。止まっているときだけ。"""
        with self._lock:
            kernel = self._kernel()
            t = kernel.task(self._task_key(task))
            return {"task": t.name, "handle": t.handle, "state": t.state.value,
                    "frames": kernel.backtrace(t, variables=variables, max_elements=max_elements)}

    def rtos_read(self, task: str | int, expression: str, frame: int = 0, max_elements: int = 64) -> dict:
        """タスクの文脈で C の式を読む（frame は rtos_backtrace の index。そのフレームの引数・ローカル変数も使える）。"""
        with self._lock:
            kernel = self._kernel()
            t = kernel.task(self._task_key(task))
            return {"task": t.name, **kernel.inspector(t).read(expression, frame, max_elements)}

    # --- 止めずに見る: タスクの時間軸 ---------------------------------------------------------------

    def rtos_timeline_start(self, method: str = "sampled", rate_hz: float = 20_000.0, ap: int = 0,
                            swo_baud: int | None = None, core_clock_hz: float | None = None, memory: bool = False,
                            tasks: list[str] | None = None, mutexes: bool | list[str] | None = None,
                            exceptions: bool = False, memory_rate_hz: float = 1000.0, kernel: bool = True) -> dict:
        """タスクの時間軸を取り始める。method: sampled（組み込み無し。DAP サンプラが rate_hz で実行中のタスクの変数を
        読む。周期より短い実行は見えないことがあり、回数は下限）/ itm（ファームのトレースフック fprobe_trace.h が
        ITM のポート 31 に書く。正確）。itm は ITM のポート 31 を有効にして fprobe_trace_enabled に 1 を書く。
        swo_baud を渡すと SWO の受信も始め、core_clock_hz も渡すと TPIU（SWO の速度）も設定する（ピンの設定は
        チップ固有なので含まない）。記録中なら止めたときに記録の stream "rtos" に書く。

        一緒に読むもの: memory（タスクの保存された SP とヒープの残量 → スタック・ヒープの推移。tasks で絞る）、mutexes
        （true か名前の列: ミューテックスの持ち主と待ちの先頭 → 優先度の逆転）、exceptions（itm だけ: 例外トレースで
        割り込みの区間 → 割り込みとタスクの合成）。memory・mutexes は DAP サンプラの 8 か所に収める。
        kernel（itm だけ）: カーネルの出来事（ポート 30・29: キュー・ミューテックス等）も流す。SWO の帯域が足りない
        （overflows が出る）ときは false にしてタスクの切り替え（ポート 31）だけにする。"""
        if method not in ("sampled", "itm"):
            raise ValueError("method は sampled か itm")
        if exceptions and method != "itm":
            raise ValueError("exceptions（割り込みの区間）は method itm だけ（例外トレースは SWO に出る）")
        if memory_rate_hz <= 0:
            raise ValueError("memory_rate_hz は正")
        with self._lock:
            if self._rtos_run is not None:
                raise RuntimeError("時間軸を取っている（rtos_timeline_stop を先に）")
            kernel = self._kernel(halted=False)
            hz = self.session.device.capabilities().timebase_hz
            halted = self.target.get_state() == Target.State.HALTED
            warnings: list[str] = []
            task_list: list[Task] | None = None
            if halted or memory or mutexes:
                task_list = kernel.tasks()
                if not halted:
                    warnings.append("ターゲットが動いている間にタスクの一覧を読んだ（一貫しないことがある）")
            else:
                warnings.append("ターゲットが動いているので始めにタスクの一覧を取らなかった: 名前は見つけた TCB から読む")
            names = {t.handle: t.name for t in task_list or []}
            slots, watches, ranges = self._watch_plan(kernel, task_list, memory, tasks, mutexes)
            if method == "sampled":
                run = self._start_sampled(kernel, names, hz, rate_hz, ap, ranges)
            else:
                run = self._start_itm(names, hz, swo_baud, core_clock_hz, exceptions, ranges, memory_rate_hz, ap, kernel)
            run.warnings = warnings + run.warnings
            run.order = kernel.priority_order
            run.priorities = {t.handle: t.priority if t.base_priority is None else t.base_priority
                              for t in task_list or []}
            try:
                run.idle = kernel.idle_handles()
            except AccessError:
                run.idle = set()
            run.slots, run.mutexes = slots, watches
            if method == "itm":
                run.objects = self._object_table(kernel, task_list, run.warnings)
            self._rtos_run, self._rtos_last = run, None
            return self._timeline_state()

    def _watch_plan(self, kernel: KernelAwareness, task_list: list[Task] | None, memory: bool,
                    tasks: list[str] | None, mutexes: bool | list[str] | None
                    ) -> tuple[dict[int, MemorySlot], list[MutexWatch], list[tuple[int, int]]]:
        """memory・mutexes でサンプラが読む語（_lock の中で）。"""
        slots: dict[int, MemorySlot] = {}
        watches: list[MutexWatch] = []
        ranges: list[tuple[int, int]] = []
        if memory:
            chosen = [t for t in task_list or [] if t.state is not TaskState.DELETED
                      and (tasks is None or t.name in tasks)]
            missing = set(tasks or ()) - {t.name for t in chosen}
            if missing:
                raise KeyError(f"タスク {', '.join(sorted(missing))} は無い")
            for t in chosen:
                address = kernel.saved_sp_address(t.handle)
                slots[address] = MemorySlot("stack", t.name, t.handle, t.stack.base or None, t.stack.end)
                ranges.append((address, 4))
            heap = kernel.heap_free_word()
            if heap is not None:
                slots[heap[0]] = MemorySlot("heap", "heap", total=heap[1])
                ranges.append((heap[0], 4))
        if mutexes:
            wanted = None if mutexes is True else set(mutexes)
            found = set()
            for o in kernel.objects(tasks=task_list):
                if wanted is not None and o.name not in wanted:
                    continue
                words = kernel.mutex_words(o)
                if words is None:
                    if wanted is not None:
                        raise ValueError(f"{o.name} はミューテックスではない（{o.kind}）")
                    continue
                holder, head, delta = words
                watches.append(MutexWatch(o.name, holder, head, delta, kernel.mutex_empty_head(o)))
                found.add(o.name)
                lo, hi = min(holder, head), max(holder, head)
                ranges += [(lo, hi - lo + 4)] if hi - lo < 64 else [(holder, 4), (head, 4)]   # 近ければ 1 か所に
            missing = (wanted or set()) - found
            if missing:
                raise KeyError(f"ミューテックス {', '.join(sorted(missing))} は無い")
            if not watches:
                raise RuntimeError("ミューテックスが見つからない（rtos_objects で確かめる）")
        return slots, watches, ranges

    @staticmethod
    def _object_table(kernel: KernelAwareness, task_list: list[Task] | None, warnings: list[str]) -> dict[int, dict]:
        """カーネルの出来事に名前・種類・長さを付けるためのオブジェクトの表（_lock の中で。走行中の読みは一貫しない
        ことがあるが、名前と長さは変わらない）。読めなければ番地のまま（object_create の出来事があればそれで）。"""
        try:
            objects = kernel.objects(tasks=task_list)
        except Exception as e:                                        # 壊れたリスト等: 名前が付かないだけ
            warnings.append(f"オブジェクトの一覧が読めない（{type(e).__name__}）: カーネルの出来事のオブジェクトは番地で出る")
            return {}
        return {o.handle: {"name": o.name, "kind": o.kind,
                           "length": o.fields.get("length", o.fields.get("max"))} for o in objects}

    @staticmethod
    def _check_slots(ranges: list[tuple[int, int]]) -> None:
        try:
            plan_slots(ranges)
        except ValueError:
            raise ValueError(f"DAP サンプラの {MAX_SLOTS} か所に収まらない（{len(ranges)} か所を読む）: memory は tasks で"
                             "タスクを絞り、mutexes は名前で絞る") from None

    def _start_sampled(self, kernel: KernelAwareness, names: dict, hz: float, rate_hz: float, ap: int,
                       extra: list[tuple[int, int]]) -> _TimelineRun:
        if rate_hz <= 0:
            raise ValueError("rate_hz は正")
        address = kernel.current_address()
        ranges = [(address, 4)] + extra
        self._check_slots(ranges)
        self._claim_sampler("rtos")
        try:
            device = self.session.device
            sampler = Sampler(device)
            sampler.configure(ranges, 1.0 / rate_hz, ap)
            window = self._open_window()
            sampler.start()
        except Exception:
            self._release_sampler("rtos")
            raise
        warnings = ["memory・mutexes の語も同じサンプラで読むので 1 巡回が長くなる（分解能が粗くなる）"] if extra else []
        return _TimelineRun("sampled", window, names, hz, address=address, rate_hz=rate_hz, warnings=warnings,
                            sampler=True)

    def _start_itm(self, names: dict, hz: float, swo_baud: int | None, core_clock_hz: float | None,
                   exceptions: bool = False, extra: list[tuple[int, int]] | None = None,
                   memory_rate_hz: float = 1000.0, ap: int = 0, kernel: bool = True) -> _TimelineRun:
        flag = self._trace_flag()
        if flag is None:
            raise RuntimeError(f"ファームにトレースフックが無い（fprobe_trace.h の {TRACE_FLAG}）: method sampled を使う")
        if extra:
            self._check_slots(extra)
            self._claim_sampler("rtos")
        try:
            warnings: list[str] = []
            kernel_ports = 1 << KERNEL_PORT | 1 << PAYLOAD_PORT
            writes = list(itm_port_writes(TRACE_PORT)) + [
                RegisterWrite(ITM_TER, kernel_ports, kernel_ports if kernel else 0,
                              "ITM: カーネルの出来事のポート 30・29 を" + ("有効に" if kernel else "無効に（帯域を空ける）"))]
            baud = swo_baud
            if swo_baud is not None and core_clock_hz is not None:
                swo = plan_swo(core_clock_hz, swo_baud)
                writes[2:2] = swo.writes                              # 鍵を外した後、ITM を有効にする前に
                warnings += swo.warnings
                baud = round(swo.baud)
            target = self.target
            write_registers(target, writes)
            if baud is not None:
                self.swo_start(baud)
            baud = baud or self._swo_baud
            if baud is None:
                warnings.append("SWO の速度が分からない（swo_start か swo_baud）: 時刻の分解能を 0 にした")
            window = self._open_window()
            target.write_memory(flag, 1)
            restore: dict[int, int] = {}
            if exceptions:                                            # 受信と窓を先に、例外トレースは最後に
                before = write_registers(target, exception_trace_writes())
                restore[DWT_CTRL] = before[DWT_CTRL]
            if extra:
                sampler = Sampler(self.session.device)
                sampler.configure(extra, 1.0 / memory_rate_hz, ap)
                sampler.start()
        except Exception:
            if extra:
                self._release_sampler("rtos")
            raise
        resolution = ITM_WORD_BYTES * 10 / baud * hz if baud else 0
        return _TimelineRun("itm", window, names, hz, flag=flag, resolution=resolution, warnings=warnings,
                            sampler=bool(extra), memory_rate_hz=memory_rate_hz if extra else None,
                            exceptions=exceptions, restore=restore, baud=baud, core_clock_hz=core_clock_hz)

    def rtos_timeline_stop(self) -> dict:
        """時間軸を取るのを止め、結果の要約（タスクごとの合計・CPU の使用率・実行の回数・最長／最短、切り替えの回数）を
        返す。記録中なら時間軸を記録の stream "rtos" に書く（run・switch・gap。方法 itm は task_in 等の生の出来事も）。"""
        with self._lock:
            run = self._rtos_run
            if run is None:
                raise RuntimeError("時間軸を取っていない（rtos_timeline_start）")
            try:
                if run.sampler:
                    Sampler(self.session.device).stop()
                if run.method == "itm" and self._debug is not None:
                    self.target.write_memory(run.flag, 0)            # トレースフックを止める（ITM の設定は残す）
                    for address, value in run.restore.items():       # 例外トレースを元に戻す
                        self.target.write_memory(address, value)
            finally:
                if run.sampler:
                    self._release_sampler("rtos")
                self._close_window(run.window)
                self._rtos_run = None
        result = self._build_timeline(run)
        self._rtos_last = result
        rec = self._recorder
        if rec is not None:
            write_timeline(rec, result.timeline)
            if result.events is not None:
                write_task_events(rec, result.events, run.names)
            if result.activations:
                names = self._exception_names()
                rec.add_many((STREAM, "isr", a.start, a.number, 0, {"name": exception_name(a.number, names)}, a.end)
                             for a in result.activations)
            if result.has_kernel:
                self._record_kernel(rec, result)
        return self._timeline_dict(result, 0, 0, recorded=rec is not None)

    def _record_kernel(self, rec, result: _TimelineResult) -> None:
        """カーネルの出来事（kind = 出来事、a = オブジェクトか対象のタスク、b = 行為者のタスク）、待ちの区間 blocked、
        逆転の区間 inversion を記録の stream rtos に。"""
        run, trace = result.run, result.kernel
        objects, isr_names = result.objects(), self._exception_names()
        rows = []
        for op in trace.ops:
            if op.kind == "lost":
                continue
            body = op.to_dict(run.names, objects, isr_names)
            del body["t"], body["kind"]
            rows.append((STREAM, op.kind, op.t, op.obj or op.task, op.actor or 0, body))
        for w in blocking(trace, run.names, objects, result.timeline.t1)["waits"]:
            rows.append((STREAM, "blocked", w["t0"], w["handle"], w["object_handle"] or 0,
                         {"task": w["task"], "on": w["on"], "outcome": w["outcome"]}, w["t1"]))
        found = inversions_from_ops(result.timeline, trace, run.priorities, run.order, run.names, objects)
        for ep in found["episodes"]:
            rows.append((STREAM, "inversion", ep["t0"], 0, 0, ep, ep["t1"]))
        rec.add_many(rows)

    def rtos_timeline_get(self, offset: int = 0, limit: int = RUNS_LIMIT) -> dict:
        """時間軸（取っている最中ならいままでの分、止めた後なら最後の結果）: 要約と、区間（run: タスク・始め・終わり・
        不確かさ）の offset から limit 件、抜け（gap）。時刻はタイムベース（timebase_hz）。"""
        return self._timeline_dict(self._current_timeline(), offset, limit)

    def _current_timeline(self) -> _TimelineResult:
        """取っている最中ならいままでの分、止めた後なら最後の結果。"""
        run = self._rtos_run
        if run is not None:
            return self._build_timeline(run)
        if self._rtos_last is not None:
            return self._rtos_last
        raise NoData("時間軸が無い（rtos_timeline_start）")

    def _exception_names(self) -> dict[int, str]:
        """割り込みの名前（ProfileTools のベクタ表から。無ければ番号の名前）。"""
        names = getattr(self, "_vector_names", None)
        return names() if names is not None else {}

    def _timeline_state(self) -> dict | None:
        run = self._rtos_run
        if run is None:
            return None
        out = {"running": True, "method": run.method, "t0": run.window.t0, "timebase_hz": run.timebase_hz,
               "source": "recording" if run.window.recorded else "event_log", "tasks_known": len(run.names),
               "warnings": list(run.warnings)}
        if run.method == "sampled":
            out.update(address=run.address, rate_hz=run.rate_hz)
        else:
            out.update(port=TRACE_PORT, resolution=run.resolution, exceptions=run.exceptions)
        if run.slots:
            out["memory"] = {"stacks": [s.name for s in run.slots.values() if s.kind == "stack"],
                             "heap": any(s.kind == "heap" for s in run.slots.values()),
                             "rate_hz": run.rate_hz if run.method == "sampled" else run.memory_rate_hz}
        if run.mutexes:
            out["mutexes"] = [w.name for w in run.mutexes]
        return out

    def _build_timeline(self, run: _TimelineRun) -> _TimelineResult:
        words = run.words
        sampled = self._window_events(run.window, "sample") if run.sampler else []
        extra = [(t, a, v) for t, a, v in sampled if a in words]
        mutex_tasks = {v - w.delta for w in run.mutexes for _, a, v in extra
                       if a == w.head_word and v not in (0, w.empty)}
        mutex_tasks |= {v for w in run.mutexes for _, a, v in extra if a == w.holder_word and v}
        if run.method == "sampled":
            samples = [(t, v) for t, a, v in sampled if a == run.address]
            self._learn_names(run, {v for _, v in samples} | mutex_tasks)
            period = run.timebase_hz / run.rate_hz
            tl = timeline_from_samples(samples, run.names, period=period)
            if tl.resolution > 1.5 * period:
                achieved_us = tl.resolution * 1e6 / run.timebase_hz
                note = (f"実際のサンプルの間隔は {achieved_us:.0f} µs（設定 {period * 1e6 / run.timebase_hz:.0f} µs）: "
                        "SWD の速さで 1 巡回が間に合わない。rate_hz を下げるか SWD を速くする")
                if note not in run.warnings:
                    run.warnings.append(note)
            return _TimelineResult(tl, None, run, extra)
        data = [(t, b) for t, _, b in self._window_events(run.window, "swo")]
        packets = itm_packets(data)
        events = task_events(packets)
        kernel = kernel_trace(packets)
        actors = {op.actor for op in kernel.ops if op.actor} | {op.task for op in kernel.ops if op.task}
        self._learn_names(run, {e.handle for e in events} | mutex_tasks | actors)
        tl = timeline_from_events(events, run.names, resolution=run.resolution, t_end=run.window.t1)
        activations, issues = None, None
        if run.exceptions:
            activations, inconsistencies, overflows = exception_activations(packets)
            issues = {"inconsistencies": inconsistencies, "overflows": overflows}
        return _TimelineResult(tl, events, run, extra, activations, issues, kernel)

    def _learn_names(self, run: _TimelineRun, handles: set[int]) -> None:
        """一覧に無い TCB の名前と元の優先度を、動作中の読みで引く（読めなければ番地のまま・優先度は分からない）。"""
        unknown = sorted(h for h in handles if h and (h not in run.names or h not in run.priorities))
        if not unknown or self._debug is None:
            return
        with self._lock:
            try:
                kernel = self._kernel(halted=False)
            except Exception:
                return
            for h in unknown:
                try:
                    name = kernel.handle_name(h)
                    priority = kernel.handle_priority(h)
                except Exception:                                     # 壊れた値・読めない番地: 番地のまま
                    continue
                if name:
                    run.names[h] = name
                run.priorities.setdefault(h, priority)

    def _timeline_dict(self, result: _TimelineResult, offset: int, limit: int, recorded: bool | None = None) -> dict:
        tl, run = result.timeline, result.run
        hz = run.timebase_hz
        stats = tl.stats()
        for row in stats["tasks"].values():
            row.update({f"{k}_us": row[k] * 1e6 / hz for k in _DURATIONS})
        out = {"running": run.window.t1 is None, "method": tl.method, "timebase_hz": hz,
               "t0": tl.t0, "t1": tl.t1, "resolution": tl.resolution, "resolution_us": tl.resolution * 1e6 / hz,
               "lower_bound": tl.lower_bound, "stats": stats, "window": self._window_info(run.window, hz),
               "warnings": list(run.warnings), "runs_total": len(tl.runs)}
        if limit:
            out["offset"] = offset
            out["runs"] = [{"task": r.task, "handle": r.handle, "t0": r.t0, "t1": r.t1,
                            "duration_us": r.duration * 1e6 / hz, "uncertainty": r.uncertainty,
                            **({"samples": r.samples} if r.samples is not None else {})}
                           for r in tl.runs[offset:offset + limit]]
            out["gaps"] = [{"t0": g.t0, "t1": g.t1, "reason": g.reason} for g in tl.gaps]
        if recorded is not None:
            out["recorded"] = recorded
        if tl.lower_bound:
            out["notes"] = ["方法 sampled: 周期より短い実行は見えないことがある。切り替え・実行の回数は下限、区間の端は"
                            "±周期の半分（samples = 1 の区間は周期より短いかもしれない）"]
        if result.activations is not None:
            out["exceptions"] = {"activations": len(result.activations), **(result.exception_issues or {})}
            if (result.exception_issues or {}).get("overflows"):
                out["warnings"].append(
                    f"ITM のあふれが {result.exception_issues['overflows']} 回: 例外の出入りが続くと SWO が追いつかず、"
                    "パケットが欠ける（割り込みの回数・時間は下限）。swo_baud を上げるか kernel=false で帯域を空ける")
        if result.kernel is not None:
            out["kernel"] = {"events": sum(1 for op in result.kernel.ops if op.kind != "lost"),
                             "lost_records": result.kernel.lost,
                             "gaps": sum(1 for op in result.kernel.ops if op.kind == "lost")}
        if run.slots or run.mutexes:
            out["words_samples"] = len(result.samples)
        return out

