"""Apache NuttX の認識（ARMv7-M）: タスク・スレッドの一覧・状態・待っているもの・スタック、タスクごとの呼び出し履歴、
カーネルのオブジェクト（nuttx_objects）。

仕様: docs/superpowers/specs/2026-10-19-u45-nuttx-design.md
構造体の位置は DWARF から（struct tcb_s・sem_t・struct mqueue_inode_s。設定 CONFIG_* でメンバーが増減する）。
- タスクの一覧は g_pidhash[g_npidhash]（位置は pid & (g_npidhash - 1)）。位置の合わないものは警告して外す。PID の順に並べる。
- 実行中は g_running_tasks[0]（無ければ準備完了の列 g_readytorun の先頭）。
- 状態は task_state（enum tstate_e。値は設定で変わるので、DWARF の列挙子の名前で見る）。待つものは waitobj。
- 残りのティックはタスクの waitdog（func が NULL でなければ動いている。expired は絶対のティック、時計は g_system_ticks）。
- 優先度は大きい方が高い（POSIX、0 はアイドル）。元の優先度は base_priority（CONFIG_PRIORITY_INHERITANCE）。
- スタックは stack_base_ptr から adj_stack_size。CONFIG_STACK_COLORATION なら全体が語 0xdeadbeef で塗られる。
- 保存された文脈は xcp.regs が指す配列（armv7-m の arm_exception.S）。s16〜s31 の枠（CONFIG_ARCH_FPU）の有無は
  REG_R13 − regs（XCPTCONTEXT_SIZE: 216 か 80）で見分け、分からなければ ELF の Tag_FP_arch で決める。ベースライン
  （ARMv6-M）の port は配列の並びが違うので、文脈は読まない（一覧・状態・スタックは読める）。
"""

from __future__ import annotations

from ..dwarf.access import AccessError, TargetAccess
from ..dwarf.info import DebugInfo
from ..dwarf.typesys import ArrayType, EnumType, strip
from .base import NAME_LIMIT, KernelAwareness
from .cortexm import SavedContext, arm_baseline, unstack_exception_frame
from .model import KernelObject, StackInfo, Task, TaskState, WaitInfo, field_location
from .nuttx_objects import MHOLDER_MASK, MRESET, NO_MHOLDER, SEM_TYPE_MUTEX, NuttXObjects
from .stackcheck import check_stack
from .structs import StructLayout

__all__ = ["STACK_COLOR", "NuttX", "read_nuttx_context"]

STACK_COLOR = (0xDEADBEEF).to_bytes(4, "little")     # arch/arm/src/common/arm_internal.h
MAX_PIDHASH = 4096
# xcp.regs の配列（arch/arm/include/armv7-m/irq.h）
REG_R13, REG_R4, REG_EXC_RETURN, SW_INT_REGS, SW_FPU_REGS = 0, 2, 11, 12, 16
CONTEXT_SIZES = {4 * (SW_INT_REGS + SW_FPU_REGS + 26): SW_FPU_REGS, 4 * (SW_INT_REGS + 8): 0}   # REG_R13 − regs → 枠
SIGNALS = {1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE",
           9: "SIGKILL", 10: "SIGUSR1", 11: "SIGSEGV", 12: "SIGUSR2", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM",
           17: "SIGCHLD", 18: "SIGCONT", 19: "SIGSTOP", 20: "SIGTSTP", 21: "SIGTTIN", 22: "SIGTTOU", 23: "SIGURG",
           24: "SIGXCPU", 25: "SIGXFSZ", 26: "SIGVTALRM", 27: "SIGPROF", 28: "SIGWINCH", 29: "SIGPOLL", 31: "SIGSYS"}
READY_STATES = {"TSTATE_TASK_READYTORUN", "TSTATE_TASK_PENDING", "TSTATE_TASK_ASSIGNED"}
# 待ちの状態 → (共通の言葉 WaitInfo.reason, 待つものの種類)。WAIT_SIG は合図の印で delay と event を分ける
WAITS = {"TSTATE_WAIT_SEM": ("receive", "semaphore"), "TSTATE_WAIT_EVENT": ("event", "event"),
         "TSTATE_WAIT_MQNOTEMPTY": ("receive", "message_queue"), "TSTATE_WAIT_MQNOTFULL": ("send", "message_queue"),
         "TSTATE_WAIT_PAGEFILL": ("pend", "page_fill")}
NOT_STARTED = {"OSINIT_POWERUP", "OSINIT_BOOT", "OSINIT_TASKLISTS", "OSINIT_MEMORY", "OSINIT_HARDWARE"}


def _words(read, address: int, count: int) -> list[int]:
    data = read(address & 0xFFFF_FFFF, 4 * count)
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, 4 * count, 4)]


def read_nuttx_context(read, regs: int, fpu_slots: int | None) -> SavedContext:
    """xcp.regs の配列（R13・BASEPRI・r4〜r11・CONTROL・EXC_RETURN・[s16〜s31 の枠]・ハードのフレーム）を読む。
    fpu_slots（s16〜s31 の枠の語数 16 / 0）が None なら REG_R13 − regs から決める（分からなければ例外）。"""
    words = _words(read, regs, SW_INT_REGS)
    if fpu_slots is None:
        fpu_slots = CONTEXT_SIZES.get((words[REG_R13] - regs) & 0xFFFF_FFFF)
        if fpu_slots is None:
            raise ValueError(f"xcp.regs 0x{regs:08X} の REG_R13 が文脈の大きさと合わない")
    exc_return = words[REG_EXC_RETURN]
    registers = {f"r{4 + n}": words[REG_R4 + n] for n in range(8)}
    fp = not exc_return & 0x10
    if fpu_slots and fp:
        registers.update({f"s{16 + n}": w for n, w in enumerate(_words(read, regs + 4 * SW_INT_REGS, 16))})
    hw, sp = unstack_exception_frame(read, regs + 4 * (SW_INT_REGS + fpu_slots), exc_return)
    registers.update(hw)
    registers["sp"] = registers["psp"] = sp
    return SavedContext(regs, registers, exc_return, fp)


def signal_names(mask: int) -> str:
    """sigset_t のビット（ビット n = 合図 n）→ "SIGUSR1|SIGUSR2"。"""
    return "|".join(SIGNALS.get(n, f"SIG{n}") for n in range(mask.bit_length()) if mask >> n & 1)


class NuttX(KernelAwareness):
    name = "NuttX"
    priority_order = 1            # 大きい方が高い（SCHED_PRIORITY_MIN 1 〜 MAX 255、アイドルは 0）

    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None, name_limit: int = NAME_LIMIT):
        super().__init__(info, access, symbols, name_limit)
        self.tcb = StructLayout.of(info, "struct tcb_s")
        self.states = self._enum(self.tcb.member_type("task_state"))
        self.sem = self._optional_layout("sem_t", "struct sem_s")
        self.mq = self._optional_layout("struct mqueue_inode_s")
        self.port = "armv6m" if arm_baseline(info.elf) else "armv7m"

    def _optional_layout(self, *names: str) -> StructLayout | None:
        try:
            return StructLayout.of(self.info, *names)
        except KeyError:
            return None

    @staticmethod
    def _enum(t) -> dict[int, str]:
        """列挙の値 → 名前（同じ値は先の名前。TSTATE_SLEEPING は TSTATE_WAIT_SIG の別名）。"""
        s = strip(t)
        out: dict[int, str] = {}
        if isinstance(s, EnumType):
            for name, value in s.enumerators:
                out.setdefault(value, name)
        return out

    @staticmethod
    def detect(info: DebugInfo) -> bool:
        try:
            info.global_variable("g_pidhash")
            info.global_variable("g_npidhash")
            tcb = StructLayout.of(info, "struct tcb_s")
            return tcb.has("task_state") and tcb.has("xcp.regs")
        except KeyError:
            return False

    # --- 読み --------------------------------------------------------------------------------------

    def _field(self, handle: int, path: str, signed: bool = False) -> int:
        return self.mem.uint(handle + self.tcb.offset(path), self.tcb.member_size(path), signed)

    def _variable(self, name: str, signed: bool = False) -> int | None:
        """カーネルの変数の値（大きさは DWARF の型）。無ければ None。"""
        try:
            var = self.info.global_variable(name)
        except KeyError:
            return None
        address = self.optional_address(name)
        if address is None:
            return None
        return self.mem.uint(address, var.type.size or 4, signed)

    def symbol_name(self, address: int) -> str | None:
        """その番地から始まる変数の名前（無ければ None）。"""
        if self.symbols is not None:
            hit = self.symbols.symbol_at(address)
            if hit is not None and hit[1] == 0 and hit[0].kind == "object":
                return hit[0].name
        return None

    def object_label(self, address: int) -> str:
        return self.symbol_name(address) or f"0x{address:08X}"

    def task_name(self, handle: int) -> str:
        if self.tcb.has("name"):
            try:
                name = self.mem.cstring(handle + self.tcb.offset("name"), self.tcb.member_size("name"), whole=True)
                if name:
                    return name
            except AccessError:
                pass
        return f"pid {self._field(handle, 'pid', signed=True)}"

    handle_name = task_name

    def current_address(self) -> int:
        """g_running_tasks[0] の番地（無い版では g_readytorun の先頭 = this_task()）。"""
        address = self.optional_address("g_running_tasks")
        return address if address is not None else self.address("g_readytorun")

    def tick_count(self) -> int | None:
        return self._variable("g_system_ticks")

    def scheduler_state(self) -> str:
        init = self.optional_address("g_nx_initstate")
        if init is not None:
            names = self._enum(self.info.global_variable("g_nx_initstate").type)
            state = names.get(self._variable("g_nx_initstate") or 0)
            if state in NOT_STARTED:
                return "not_started"
            if state == "OSINIT_PANIC":
                return "unknown"
        current = self.current()
        if current and self._field(current, "lockcount", signed=True) > 0:
            return "suspended"                                             # sched_lock の中
        return "running"

    def version(self) -> str | None:
        return None                                                        # 版の変数は無い（CONFIG_VERSION_* は定数）

    # --- タスク ------------------------------------------------------------------------------------

    def tcb_of_pid(self, pid: int) -> int | None:
        """PID → TCB の番地（g_pidhash の位置 pid & (n − 1) にあり、pid が合うもの）。"""
        n = self._variable("g_npidhash", signed=True) or 0
        table = self._variable("g_pidhash")
        if n <= 0 or not table:
            return None
        tcb = self.mem.u32(table + 4 * (pid & (n - 1)))
        return tcb if tcb and self._field(tcb, "pid", signed=True) == pid else None

    def handles(self) -> list[int]:
        n = self._variable("g_npidhash", signed=True) or 0
        table = self._variable("g_pidhash") or 0
        if n <= 0 or not table:
            return []
        if n > MAX_PIDHASH:
            self.warnings.append(f"g_npidhash = {n}。読む上限 {MAX_PIDHASH} 個まで読んだ（続きは読んでいない）")
            n = MAX_PIDHASH
        found: dict[int, int] = {}                                          # TCB → pid
        for i, tcb in enumerate(_words(self.access.read_memory, table, n)):
            if not tcb:
                continue
            pid = self._field(tcb, "pid", signed=True)
            if pid & (n - 1) != i:
                self.warnings.append(f"g_pidhash[{i}] の TCB 0x{tcb:08X}（PID {pid}）は位置が違う")
                continue
            found.setdefault(tcb, pid)
        return sorted(found, key=found.get)

    def tasks(self) -> list[Task]:
        self.warnings = []
        current = self.current()
        tick = self.tick_count()
        tasks = [self._task(h, current, tick) for h in self.handles()]
        self._names = {t.handle: t.name for t in tasks}
        return tasks

    def _task(self, handle: int, current: int, tick: int | None) -> Task:
        state_name = self.states.get(self._field(handle, "task_state"), "?")
        wait = None
        notes: list[str] = []
        running = state_name == "TSTATE_TASK_RUNNING"
        if running:
            state = TaskState.RUNNING
        elif state_name in READY_STATES:
            state = TaskState.READY
            if state_name == "TSTATE_TASK_PENDING":
                notes.append("横取りの許しを待つ（sched_lock の間に準備完了になった）")
        elif state_name == "TSTATE_TASK_STOPPED":
            state, wait = TaskState.SUSPENDED, WaitInfo("suspended")
            notes.append("SIGSTOP で止められている（SIGCONT を待つ）")
        elif state_name == "TSTATE_TASK_INACTIVE":
            state, wait = TaskState.SUSPENDED, WaitInfo("suspended")
            notes.append("まだ起動していない（nxtask_activate の前）")
        elif state_name == "TSTATE_TASK_INVALID":
            state = TaskState.DELETED
        else:
            state = TaskState.BLOCKED
            wait = self._wait(handle, state_name, tick)
        if running and handle != current:
            notes.append("TSTATE_TASK_RUNNING だが g_running_tasks のタスクではない")
        stack = self._stack(handle, running)
        if running and handle == current and stack.pointer is None and self._handler_mode():
            notes.append("ハンドラモードで止まった: 中断されたタスクの SP（psp か msp か）はコアの状態から分からない")
        if stack.overflow:
            notes.append("SP がスタックの底（stack_base_ptr）より下")
        base = self._field(handle, "base_priority") if self.tcb.has("base_priority") else None
        task = Task(handle, self.task_name(handle), self._field(handle, "sched_priority"), state, stack, wait, base,
                    self._field(handle, "pid", signed=True), notes=notes)
        task.locations["priority"] = field_location(self.tcb, handle, "sched_priority")
        if base is not None:
            task.locations["base_priority"] = field_location(self.tcb, handle, "base_priority")
        return task

    def _wait(self, handle: int, state_name: str, tick: int | None) -> WaitInfo:
        if state_name == "TSTATE_WAIT_SIG":
            mask = self._signal_mask(handle)
            wait = WaitInfo("event", kind="signal", name=signal_names(mask)) if mask else WaitInfo("delay")
        else:
            reason, kind = WAITS.get(state_name, ("unknown", None))
            wait = WaitInfo(reason, kind=kind)
        if self.tcb.has("waitdog.func") and self._field(handle, "waitdog.func"):
            expired = self._field(handle, "waitdog.expired", signed=True)
            if tick is not None:
                wait.remaining_ticks = max(expired - tick, 0)
                wait.wake_tick = expired
        obj = self._field(handle, "waitobj") if self.tcb.has("waitobj") else 0
        if wait.kind in ("semaphore", "message_queue", "event") and obj:
            wait.object = wait.list = obj
            wait.name = self.object_label(obj)
            if wait.kind == "semaphore" and self.sem is not None and self.is_mutex(obj):
                wait.kind = "mutex"
                holder = self.mutex_holder(obj)
                wait.holder = self.task_name(holder) if holder else None
        return wait

    def _signal_mask(self, handle: int) -> int:
        if not self.tcb.has("sigwaitmask"):
            return 0
        size = self.tcb.member_size("sigwaitmask")
        return self.mem.uint(handle + self.tcb.offset("sigwaitmask"), size)

    def is_mutex(self, sem: int) -> bool:
        return bool(self.mem.u8(sem + self.sem.offset("flags")) & SEM_TYPE_MUTEX)

    def mutex_holder(self, sem: int) -> int | None:
        """ミューテックスの持ち主の TCB（val.mholder の TID。持ち主がいなければ None）。"""
        holder = self.mem.u32(sem + self.sem.offset("val.mholder"))
        tid = holder & MHOLDER_MASK
        if tid in (NO_MHOLDER, MRESET):
            return None
        return self.tcb_of_pid(tid)

    def _stack(self, handle: int, running: bool) -> StackInfo:
        base = self._field(handle, "stack_base_ptr")
        size = self._field(handle, "adj_stack_size")
        end = base + size if base and size else None
        if running:
            pointer = self._running_sp()
        else:
            pointer = self._field(handle, "xcp.regs") or None             # 文脈はスタックの上に積まれる
        if not base:
            return StackInfo(0, None, pointer)
        return check_stack(self.access.read_memory, base, end, pointer, STACK_COLOR)

    def _running_sp(self) -> int | None:
        """実行中のタスクの SP。値の見た目（スタックの範囲に入るか）では選ばず、コアの状態で決める: スレッドモード
        （xPSR の IPSR = 0）なら CONTROL.SPSEL の示す方（1 = psp、0 = msp）。ハンドラモードでは、中断されたスレッドが
        どちらを使っていたかはコアのレジスタから分からない（EXC_RETURN は LR から消えうる）ので None。
        xPSR・CONTROL が読めなければ None。"""
        try:
            if self.access.read_register("xpsr") & 0x1FF:
                return None
            name = "psp" if self.access.read_register("control") & 0b10 else "msp"
            return self.access.read_register(name)
        except AccessError:
            return None

    def _handler_mode(self) -> bool:
        try:
            return bool(self.access.read_register("xpsr") & 0x1FF)
        except AccessError:
            return False

    # --- 呼び出し履歴 ------------------------------------------------------------------------------

    def saved_context(self, key: Task | str | int) -> SavedContext | None:
        task = self.task(key)
        if task.state is TaskState.RUNNING:
            return None
        if self.port != "armv7m":
            raise ValueError(f"{self.port} の NuttX の文脈（xcp.regs の並び）は扱わない（armv7m だけ）")
        regs = self._field(task.handle, "xcp.regs")
        # FPU の枠は文脈の大きさ（REG_R13 − regs）で決める。合わなければ誤り（ELF の FP の属性で決めて読まない:
        # FPU 向けに作ったことと CONFIG_ARCH_FPU は別）
        return read_nuttx_context(self.access.read_memory, regs, None)

    def idle_handles(self) -> set[int]:
        address = self.optional_address("g_idletcb")
        if address is None:
            return set()
        t = strip(self.info.global_variable("g_idletcb").type)
        count = (t.length or 1) if isinstance(t, ArrayType) else 1
        return {address + i * self.tcb.size for i in range(count)}

    # --- カーネルのオブジェクト ------------------------------------------------------------------------

    def objects(self, kinds: list[str] | None = None, tasks: list[Task] | None = None) -> list[KernelObject]:
        """semaphore・mutex・message_queue（nuttx_objects）。"""
        tasks = self.tasks() if tasks is None else tasks
        return self.select(NuttXObjects(self).objects(tasks), kinds)

    def handle_priority(self, handle: int) -> int:
        return self._field(handle, "base_priority" if self.tcb.has("base_priority") else "sched_priority")

    def saved_sp_address(self, handle: int) -> int:
        """xcp.regs（切り替えのたびに書かれ、スタックの上に積んだ文脈を指す。SP より文脈の分だけ低い）。"""
        return handle + self.tcb.offset("xcp.regs")

    def mutex_words(self, obj: KernelObject) -> tuple[int, int, int] | None:
        return None                       # 持ち主は val.mholder の TID で、TCB の番地ではない（サンプラでは辿れない）

    def snapshot(self) -> dict:
        tasks = self.tasks()
        current = self.current()
        return {"rtos": self.name, "version": self.version(), "port": self.port, "scheduler": self.scheduler_state(),
                "tick": self.tick_count(), "current": self._names.get(current) if current else None,
                "tasks": [t.to_dict() for t in tasks], "warnings": list(self.warnings)}
