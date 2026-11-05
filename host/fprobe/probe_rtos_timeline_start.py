"""RTOS の時間軸を始める（RtosService に混ぜ込む部品）: 方法 sampled（DAP サンプラ）と itm（ファームのトレースフック）。

始めるときの計画（サンプラのか所・オブジェクトの一覧）と、方法ごとの受け口の準備。止める・結果を読むのは probe_rtos_timeline_read.py。
"""

from pyocd.core.target import Target

from .dwarf.access import AccessError
from .probe_window import write_registers
from .profile.dwt import DWT_CTRL, ITM_TER, RegisterWrite, exception_trace_writes, itm_port_writes, plan_swo
from .rtos.analysis import MemorySlot, MutexWatch
from .rtos.base import KernelAwareness
from .rtos.kernel_trace import KERNEL_PORT, PAYLOAD_PORT
from .rtos.model import Task, TaskState
from .rtos.timeline import TRACE_PORT
from .sampler import MAX_SLOTS, Sampler, plan_slots
from .probe_rtos_run import ITM_WORD_BYTES, SWO_BITS_PER_BYTE, TRACE_FLAG, _TimelineRun

class RtosTimelineStartMixin:
    """rtos_timeline_start と、その計画・準備。"""

    def rtos_timeline_start(self, method: str = "sampled", rate_hz: float = 20_000.0, ap: int = 0,
                            swo_baud: int | None = None, core_clock_hz: float | None = None, memory: bool = False,
                            tasks: list[str] | None = None, mutexes: bool | list[str] | None = None,
                            exceptions: bool = False, memory_rate_hz: float = 1000.0, kernel: bool = True,
                            vector_table: int | None = None, vector_table_size: int | None = None,
                            caller: dict | None = None) -> dict:
        """タスクの時間軸を取り始める。method: sampled（組み込み無し。DAP サンプラが rate_hz で実行中のタスクの変数を
        読む。周期より短い実行は見えないことがあり、回数は下限）/ itm（ファームのトレースフック fprobe_trace.h が
        ITM のポート 31 に書く。正確）。itm は ITM のポート 31 を有効にして fprobe_trace_enabled に 1 を書く。
        swo_baud を渡すと SWO の受信も始め、core_clock_hz も渡すと TPIU（SWO の速度）も設定する（ピンの設定は
        チップ固有なので含まない）。記録中なら止めたときに記録の stream "rtos" に書く。

        一緒に読むもの: memory（タスクの保存された SP とヒープの残量 → スタック・ヒープの推移。tasks で絞る）、mutexes
        （true か名前の列: ミューテックスの持ち主と待ちの先頭 → 優先度の逆転）、exceptions（itm だけ: 例外トレースで
        割り込みの区間 → 割り込みとタスクの合成）。memory・mutexes は DAP サンプラの 8 か所に収める。
        kernel（itm だけ）: カーネルの出来事（ポート 30・29: キュー・ミューテックス等）も流す。SWO の帯域が足りない
        （overflows が出る）ときは false にしてタスクの切り替え（ポート 31）だけにする。
        vector_table: ベクタ表の番地（使う側が明示する。大きさは vector_table_size か ELF のその番地の変数）。渡せば
        割り込みの区間に処理の関数の名前を付ける。渡さなければ番号の名前だけ（表の場所を推定しない）。
        caller: デーモンが渡す呼び出し元 {id, name, role}（引数の仕様には出ない。sampler_status の client に残す）。"""
        if method not in ("sampled", "itm"):
            raise ValueError("method は sampled か itm")
        if exceptions and method != "itm":
            raise ValueError("exceptions（割り込みの区間）は method itm だけ（例外トレースは SWO に出る）")
        if memory_rate_hz <= 0:
            raise ValueError("memory_rate_hz は正")
        with self._host._lock:
            if self._rtos_run is not None:
                raise RuntimeError("時間軸を取っている（rtos_timeline_stop を先に）")
            kernel = self._kernel(halted=False)
            table = self._host._vector_table_arg(vector_table, vector_table_size)
            hz = self._host.session.device.capabilities().timebase_hz
            halted = self._host.target.get_state() == Target.State.HALTED
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
                run = self._start_sampled(kernel, names, hz, rate_hz, ap, ranges, caller)
            else:
                run = self._start_itm(names, hz, swo_baud, core_clock_hz, exceptions, ranges, memory_rate_hz, ap, kernel,
                                      caller)
            run.warnings = warnings + run.warnings
            run.order = kernel.priority_order
            run.priorities = {t.handle: t.priority if t.base_priority is None else t.base_priority
                              for t in task_list or []}
            try:
                run.idle = kernel.idle_handles()
            except AccessError:
                run.idle = set()
            run.slots, run.mutexes = slots, watches
            run.vector_table = table
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
                       extra: list[tuple[int, int]], caller: dict | None = None) -> _TimelineRun:
        if rate_hz <= 0:
            raise ValueError("rate_hz は正")
        address = kernel.current_address()
        ranges = [(address, 4)] + extra
        self._check_slots(ranges)
        self._host._claim_sampler("rtos", caller)
        try:
            device = self._host.session.device
            sampler = Sampler(device)
            sampler.configure(ranges, 1.0 / rate_hz, ap)
            window = self._host._open_window()
            sampler.start()
        except Exception:
            self._host._release_sampler("rtos")
            raise
        warnings = ["memory・mutexes の語も同じサンプラで読むので 1 巡回が長くなる（分解能が粗くなる）"] if extra else []
        return _TimelineRun("sampled", window, names, hz, address=address, rate_hz=rate_hz, warnings=warnings,
                            sampler=True)

    def _start_itm(self, names: dict, hz: float, swo_baud: int | None, core_clock_hz: float | None,
                   exceptions: bool = False, extra: list[tuple[int, int]] | None = None,
                   memory_rate_hz: float = 1000.0, ap: int = 0, kernel: bool = True,
                   caller: dict | None = None) -> _TimelineRun:
        flag = self._trace_flag()
        if flag is None:
            raise RuntimeError(f"ファームにトレースフックが無い（fprobe_trace.h の {TRACE_FLAG}）: method sampled を使う")
        if extra:
            self._check_slots(extra)
            self._host._claim_sampler("rtos", caller)
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
            target = self._host.target
            original = write_registers(target, writes)                 # 止めるとき DWT_CTRL（同期パケット等）を戻す
            if baud is not None:
                self._host.swo_start(baud)
            baud = baud or self._host._swo_baud
            if baud is None:
                warnings.append("SWO の速度が分からない（swo_start か swo_baud）: 時刻の分解能は分からない（None）")
            window = self._host._open_window()
            target.write_memory(flag, 1)
            restore: dict[int, int] = {DWT_CTRL: original[DWT_CTRL]}
            if exceptions:                                            # 受信と窓を先に、例外トレースは最後に
                write_registers(target, exception_trace_writes())
            if extra:
                sampler = Sampler(self._host.session.device)
                sampler.configure(extra, 1.0 / memory_rate_hz, ap)
                sampler.start()
        except Exception:
            if extra:
                self._host._release_sampler("rtos")
            raise
        bits = SWO_BITS_PER_BYTE.get(self._host._swo_mode)
        if baud is not None and bits is None:
            warnings.append(f"SWO の方式 {self._host._swo_mode} の 1 バイトのビット数を数えていない: 時刻の分解能は分からない"
                            "（None）、帯域の使用率は出さない")
        resolution = ITM_WORD_BYTES * bits / baud * hz if baud and bits else None
        return _TimelineRun("itm", window, names, hz, flag=flag, resolution=resolution, warnings=warnings,
                            sampler=bool(extra), memory_rate_hz=memory_rate_hz if extra else None,
                            exceptions=exceptions, restore=restore, baud=baud, core_clock_hz=core_clock_hz,
                            bits_per_byte=bits)
