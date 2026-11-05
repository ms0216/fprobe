"""Probe の道具: RTOS の認識（F6）。止まったターゲットのタスクの一覧・タスクごとの呼び出し履歴と C の式、
止めずに見るタスクの時間軸。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・target・session・_symbols・_debug・_recorder・
_swo_baud・swo_start と、WindowService（DAP サンプラの持ち主・出来事の窓・write_registers）を使う。
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
  exceptions = 例外トレース（方法 itm の SWO に一緒に流す）。解析の道具は probe_rtos_analysis（RtosAnalysisService）。
- 方法 itm はカーネルの出来事（U10。fprobe_trace.h の記録: ITM のポート 30・29）も読む: キュー・セマフォ・ミューテックスの
  操作・準備完了・待ち・優先度・割り込みの出入り。始めにオブジェクトの一覧を走行中に読み（名前・種類・長さ）、止めたとき
  記録に出来事・待ちの区間（blocked）・逆転の区間（inversion）を書く。

領域ごとの分け方: ここには RTOS の読み手と止めて調べる道具（一覧・呼び出し履歴・式）を置く。時間軸を始める道具は
probe_rtos_timeline_start.py、止める・読む道具は probe_rtos_timeline_read.py（いずれも RtosService に混ぜ込む部品）、
実行状態と定数は probe_rtos_run.py。

仕様: docs/superpowers/specs/2026-10-17-f6-rtos-design.md（9 節が組み込みの提案）、
docs/superpowers/specs/2026-10-18-u08-u09-rtos-objects-timeline-design.md（オブジェクト・時間軸の解析）、
docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md（カーネルの出来事）
"""

import re
from pyocd.core.target import Target

from .debug import Debugger, PyocdAccess
from .rtos import detect, open_rtos
from .rtos.base import KernelAwareness
from .rtos.contention import contention
from .probe_rtos_run import _MemoryOnly, _TimelineResult, _TimelineRun
from .probe_service import HostView
from .probe_rtos_timeline_read import RtosTimelineReadMixin
from .probe_rtos_timeline_start import RtosTimelineStartMixin
from .probe_rtos_run import ITM_WORD_BYTES, RUNS_LIMIT, SWO_BITS_PER_BYTE, TRACE_FLAG  # noqa: F401  (旧名を保つ)

__all__ = ["RtosService", "TRACE_FLAG"]

_HANDLE = re.compile(r"0x[0-9a-fA-F]+")


class RtosService(RtosTimelineStartMixin, RtosTimelineReadMixin):
    """RTOS の認識（Probe のサービス）。"""

    API = ("rtos_backtrace", "rtos_info", "rtos_objects", "rtos_read", "rtos_tasks", "rtos_timeline_get",
           "rtos_timeline_start", "rtos_timeline_stop")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_current_timeline", "_exception_names", "_kernel", "_rtos_debug_info", "_rtos_last", "_rtos_run",
              "_timeline_dict")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_claim_sampler", "_close_window", "_debug", "_lock", "_open_window", "_recorder", "_release_sampler",
             "_swo_baud", "_swo_mode", "_symbols", "_vector_names", "_vector_table_arg", "_window_events",
             "_window_info", "session", "swo_start", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._rtos_run: _TimelineRun | None = None
        self._rtos_last: _TimelineResult | None = None

    # --- RTOS の読み手 -----------------------------------------------------------------------

    def _rtos_debug_info(self):
        if self._host._symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load）")
        info = Debugger(None, self._host._symbols).debug_info()
        if info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: RTOS の構造体が分からない")
        return info

    def _kernel(self, halted: bool = True, port: str | None = None) -> KernelAwareness:
        """RTOS の読み手（_lock の中で）。halted なら、止まっていなければ誤り。動いているターゲットの読み手は
        メモリだけを読む（コアのレジスタは止めないと意味が無い: 実行中のタスクの SP は分からないとする）。"""
        info = self._rtos_debug_info()
        target = self._host.target
        running = target.get_state() != Target.State.HALTED
        if halted and running:
            raise RuntimeError("ターゲットが動いている: 止めてから（target_halt）。一覧は live=true で走行中にも読める"
                               "（一貫しないことがある）")
        access = _MemoryOnly(PyocdAccess(target)) if running else PyocdAccess(target)
        options = {} if port is None else {"port": port}
        if options and detect(info) not in ("freertos", "threadx"):
            raise ValueError("port を渡せるのは FreeRTOS・ThreadX だけ")
        kernel = open_rtos(info, access, self._host._symbols, **options)
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
            return self._host._symbols.variable(TRACE_FLAG).address if self._host._symbols is not None else None
        except KeyError:
            return None

    # --- 止めて調べる ------------------------------------------------------------------------

    def rtos_info(self) -> dict:
        """ELF の RTOS の種類と、接続していればスケジューラの状態・ティック・実行中のタスク（止まっていればタスクの数）。
        トレースフック（方法 itm）の有無と、時間軸を取っているか。"""
        with self._host._lock:
            info = self._rtos_debug_info()
            kind = detect(info)
            out = {"rtos": kind, "elf": str(self._host._symbols.path), "trace_hook": self._trace_flag() is not None,
                   "timeline": self._timeline_state(), "connected": self._host._debug is not None}
            if kind is None or self._host._debug is None:
                return out
            kernel = self._kernel(halted=False)
            halted = self._host.target.get_state() == Target.State.HALTED
            current = kernel.current()
            out.update(name=kernel.name, halted=halted, scheduler=kernel.scheduler_state(), tick=kernel.tick_count(),
                       current={"handle": current, "name": kernel.handle_name(current) if current else None})
            if getattr(kernel, "port", None) is not None:
                out["port"] = kernel.port
            if hasattr(kernel, "port_basis"):
                out["port_basis"] = kernel.port_basis
            if halted:
                out["tasks"] = len(kernel.tasks())
            return out

    def rtos_tasks(self, live: bool = False) -> dict:
        """タスクの一覧（名前・優先度・状態・待っているもの・スタックの使用量とあふれの印）。止まっているときだけ。
        live なら走行中にも読む（止めずに読むのでリストが途中で変わり得る: consistent = false）。"""
        with self._host._lock:
            kernel = self._kernel(halted=not live)
            return self._consistency(kernel.snapshot())

    def rtos_objects(self, kinds: list[str] | None = None, live: bool = False) -> dict:
        """カーネルのオブジェクト（キュー・セマフォ・ミューテックスと持ち主・タイマ・イベントグループ・ストリーム /
        メッセージバッファ。Zephyr は k_* の各種）と、ヒープの要約、止めた瞬間の優先度の逆転・デッドロック。
        登録簿に無い・名前の無いものも、変数の型・待っているタスクから番地で出す。kinds で種類を絞る。
        止まっているときだけ（live なら走行中にも: consistent = false）。"""
        with self._host._lock:
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
        halted = self._host.target.get_state() == Target.State.HALTED
        out.update(halted=halted, consistent=halted)
        if not halted:
            out["notes"] = ["走行中に読んだ: リスト・数・持ち主は読む間に変わり得る（一貫しない）。実行中のタスクの SP は"
                            "分からない。正確な様子は target_halt してから"]
        return out

    def rtos_backtrace(self, task: str | int, variables: bool = False, max_elements: int = 64,
                       port: str | None = None) -> dict:
        """タスク（名前か TCB の番地）の呼び出し履歴。止まっているタスクは保存された文脈から、実行中のタスクは
        実レジスタから巻き戻す。variables なら各段の引数とローカル変数も。止まっているときだけ。
        port は保存された文脈の積み方（FreeRTOS・ThreadX。ELF から決まらないとき使う側が渡す）。"""
        with self._host._lock:
            kernel = self._kernel(port=port)
            t = kernel.task(self._task_key(task))
            return {"task": t.name, "handle": t.handle, "state": t.state.value,
                    "frames": kernel.backtrace(t, variables=variables, max_elements=max_elements)}

    def rtos_read(self, task: str | int, expression: str, frame: int = 0, max_elements: int = 64,
                  port: str | None = None) -> dict:
        """タスクの文脈で C の式を読む（frame は rtos_backtrace の index。そのフレームの引数・ローカル変数も使える）。
        port は rtos_backtrace と同じ。"""
        with self._host._lock:
            kernel = self._kernel(port=port)
            t = kernel.task(self._task_key(task))
            return {"task": t.name, **kernel.inspector(t).read(expression, frame, max_elements)}

    # --- 止めずに見る: タスクの時間軸 ---------------------------------------------------------------
