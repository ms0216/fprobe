"""実行の制御: ソース行単位のステップ（into / over / out / until）と、その土台（1 命令・踏み台まで走らせる）。

GDB の step / next / finish / until・advance、Ozone・IAR の Step Into / Over / Out / Run to Cursor の相当。
ターゲットは pyOCD の CortexM の口（step・resume・halt・set_breakpoint・read_core_registers_raw・read32）。

- Runner: 機器の手続き。1 命令進める（いまの PC のブレークポイントは外して踏む）、踏み台（一時のブレークポイント）まで
  走らせて待つ（待つ間は機器のロックを放す）、止まった理由は DFSR で見る（pyOCD の get_halt_reason は 1 命令の実行の
  ビットを優先し、同時に成立したウォッチポイントを見落とすため）。
- LineStepper: 行表（Symbols.line_entry）と呼び出しの検出で「次の文」まで進める規則。
  - 1 命令ずつ進め、文の始まり（行表の is_stmt の行）で行が変わったら止まる。同じ行・行の途中は続ける（GDB と同じ）。
  - 呼び出し（BL / BLX: LR が直前の命令の次の番地になった）は、戻り先の踏み台まで走らせて踏み越える（over・until。
    into でも行の情報の無い関数は踏み越える）。踏み台に、より深い同じ関数の呼び出し（再帰。sp が小さい）で着いたら続ける。
  - 割り込みの入口（IPSR が変わり LR が EXC_RETURN）は、積まれた戻り先まで走らせて踏み越える。故障（HardFault 等）の
    入口ならその場で止める（reason "fault"。原因は target.fault）。
  - 呼び出し元へ戻ったら（CFI の CFA が大きくなった / CFI が無ければ関数が変わった）、呼び出しの行の途中でも止まる（GDB と同じ）。
  - until: over と同じだが、同じフレームで後ろへ戻る分岐（ループ）では止まらない。location を指定すると、そこか
    いまの関数から戻るところまで走る（GDB の until LOCATION）。
  - out: CFI で戻り先と呼び出し元の sp が分かれば、戻り先の踏み台まで一気に走る。CFI が無ければ 1 命令ずつ（呼び出しは
    踏み越える）関数から出るまで。
- 割り込みを止める（DHCSR.C_MASKINTS）のは 1 命令ずつ進める間だけ。呼び出しを踏み越えて走る間は割り込みも動く
  （止めると割り込みを待つ関数が終わらない）。
"""

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pyocd.core.target import Target

__all__ = ["DFSR", "Halt", "LineStepper", "Regs", "Runner", "STEP_MODES", "dfsr_reason", "is_exc_return"]

STEP_MODES = ("into", "over", "out", "until")
DFSR = 0xE000ED30
_HALTED, _BKPT, _DWTTRAP, _VCATCH, _EXTERNAL = 0x1, 0x2, 0x4, 0x8, 0x10
FAULTS = range(3, 8)                       # HardFault・MemManage・BusFault・UsageFault・SecureFault


def is_exc_return(value: int) -> bool:
    return value >> 24 == 0xFF


def dfsr_reason(dfsr: int, stepping: bool) -> str | None:
    """DFSR → 止まった理由。1 命令の実行（HALTED だけ）は stepping なら None、走らせた後なら外からの停止 "user"。"""
    if dfsr & _VCATCH:
        return "vector_catch"
    if dfsr & _DWTTRAP:
        return "watchpoint"
    if dfsr & _BKPT:
        return "breakpoint"
    if dfsr & _EXTERNAL:
        return "external"
    return None if stepping else "user"


@dataclass(frozen=True)
class Regs:
    pc: int
    lr: int
    sp: int
    xpsr: int

    @property
    def ipsr(self) -> int:
        return self.xpsr & 0x1FF


@dataclass(frozen=True)
class Halt:
    """止まった様子。reason: stepped（1 命令・行のステップを終えた）/ arrived（踏み台に着いた）/ breakpoint /
    watchpoint / vector_catch / external / user / fault / timeout。"""
    reason: str
    regs: Regs


class Runner:
    """機器の手続き。breakpoints は利用者のブレークポイント（番地 → 名前。踏み越えに使う）。
    on_breakpoint: 利用者のブレークポイント・BKPT 命令で止まったときに呼ぶ（ロックの中で）。True なら止まらずに続ける
    （条件付きのブレークポイント・セミホスティング）。"""

    def __init__(self, target, breakpoints: Mapping[int, str], lock: contextlib.AbstractContextManager | None = None,
                 deadline: float = float("inf"), on_breakpoint: Callable[[], bool] | None = None,
                 poll_s: float = 0.002):
        self.target = target
        self.breakpoints = breakpoints
        self.lock = lock if lock is not None else contextlib.nullcontext()
        self.deadline = deadline
        self.on_breakpoint = on_breakpoint
        self.poll_s = poll_s
        self.temporary: set[int] = set()

    # --- 読む ----------------------------------------------------------------------------------

    def regs(self) -> Regs:
        with self.lock:
            return Regs(*self.target.read_core_registers_raw(["pc", "lr", "sp", "xpsr"]))

    def read32(self, address: int) -> int:
        with self.lock:
            return self.target.read32(address)

    def locked(self, fn: Callable):
        with self.lock:
            return fn()

    def timed_out(self) -> bool:
        return time.monotonic() >= self.deadline

    # --- 1 命令 --------------------------------------------------------------------------------

    def step(self, mask_interrupts: bool = True) -> Halt:
        """1 命令進める。普通に進めば stepped。BKPT 命令（セミホスティング等）で on_breakpoint が続けると言えば stepped。"""
        with self.lock:
            reason = self._step_here(mask_interrupts)
            if reason == "breakpoint" and self.on_breakpoint is not None and self.on_breakpoint():
                reason = None                                    # 要求を処理して PC は進めてある
            return Halt(reason or "stepped", self.regs())

    def _step_here(self, mask_interrupts: bool) -> str | None:
        target = self.target
        pc = target.read_core_register("pc")
        lifted = pc in self.breakpoints or pc in self.temporary
        if lifted:                                               # ブレークポイントの上で進めると止まり直す
            target.remove_breakpoint(pc)
        try:
            target.step(disable_interrupts=mask_interrupts)
        finally:
            if lifted:
                target.set_breakpoint(pc, Target.BreakpointType.AUTO)
        return dfsr_reason(target.read32(DFSR), stepping=True)

    # --- 踏み台まで走らせる ------------------------------------------------------------------------

    def run_to(self, targets: Mapping[int, Callable[[Regs], bool]]) -> Halt:
        """踏み台（番地 → 着いたと認める条件）のどれかに着くまで走らせる。着けば arrived（regs.pc がその番地）。
        条件が偽なら（再帰の深い呼び出し等）続ける。ほかの理由で止まればその理由、時間切れなら止めて timeout。"""
        placed = []
        try:
            with self.lock:
                for address in targets:
                    if address in self.breakpoints or address in self.temporary:
                        continue
                    if not self.target.set_breakpoint(address, Target.BreakpointType.AUTO):
                        raise RuntimeError(f"0x{address:08X} に踏み台のブレークポイントを置けない（比較器が足りない。"
                                           "ブレークポイントを減らす）")
                    self.temporary.add(address)
                    placed.append(address)
            while True:
                with self.lock:
                    pc = self.target.read_core_register("pc")
                    if pc in self.breakpoints or pc in self.temporary:   # 踏み台・ブレークポイントの上から: 1 命令踏む
                        reason = self._step_here(mask_interrupts=True)
                        regs = self.regs()
                        if reason is not None:
                            return Halt(reason, regs)
                        if regs.pc in targets and targets[regs.pc](regs):
                            return Halt("arrived", regs)
                    self.target.resume()
                reason = self._wait()
                regs = self.regs()
                if reason == "timeout":
                    return Halt("timeout", regs)
                if reason == "breakpoint":
                    if regs.pc in targets:
                        if targets[regs.pc](regs):
                            return Halt("arrived", regs)
                        continue
                    if regs.pc in self.temporary:                # ほかの踏み台（呼び手が別に置いたもの）
                        continue
                    if self.on_breakpoint is not None and self.locked(self.on_breakpoint):
                        continue
                return Halt(reason, regs)
        finally:
            with self.lock:
                for address in placed:
                    self.target.remove_breakpoint(address)
                    self.temporary.discard(address)

    def _wait(self) -> str:
        """止まるまで待つ（待つ間はロックを放す）。止まった理由（DFSR）か、時間切れなら止めて "timeout"。"""
        while True:
            with self.lock:
                if self.target.get_state() == Target.State.HALTED:
                    return dfsr_reason(self.target.read32(DFSR), stepping=False)
                if self.timed_out():
                    self.target.halt()
                    return "timeout"
            time.sleep(self.poll_s)


def _is_call(before: Regs, after: Regs) -> bool:
    """BL / BLX を実行した: LR が直前の命令の次の番地（Thumb のビット付き）になり、順に進んでいない。"""
    return (after.lr != before.lr and after.lr & 1 == 1 and (after.lr & ~1) - before.pc in (2, 4)
            and not 0 < after.pc - before.pc <= 4)


class LineStepper:
    """行単位のステップの規則。symbols は fprobe.symbols.Symbols（function_at・line_entry・after_prologue）。
    frame_cfa: いまのフレームの CFA（CFI。分からなければ None）、caller: (戻り先, いまのフレームの CFA)（CFI。
    分からなければ None）。どちらも機器を読むので Runner のロックの中で呼ぶ。"""

    def __init__(self, runner: Runner, symbols, frame_cfa: Callable[[], int | None] = lambda: None,
                 caller: Callable[[], tuple[int, int] | None] = lambda: None, mask_interrupts: bool = True):
        self.runner = runner
        self.symbols = symbols
        self.frame_cfa = frame_cfa
        self.caller = caller
        self.mask = mask_interrupts

    def step(self, mode: str, location: int | None = None) -> Halt:
        if mode not in STEP_MODES:
            raise ValueError(f"mode は {', '.join(STEP_MODES)} のどれか")
        if mode == "out":
            return self._out()
        if mode == "until" and location is not None:
            return self._advance(location)
        start = self.runner.regs()
        if self.symbols.line_entry(start.pc) is None:
            return self._out()                     # 行の情報の無いコード: 呼び出し元へ（GDB の step と同じ）
        return self._lines(mode, start)

    # --- 次の文まで ---------------------------------------------------------------------------------

    def _lines(self, mode: str, start: Regs) -> Halt:
        symbols, runner = self.symbols, self.runner
        first = symbols.line_entry(start.pc)
        key = (first.file, first.line)
        function = symbols.function_at(start.pc)
        cfa0 = runner.locked(self.frame_cfa)
        prev = start
        while True:
            if runner.timed_out():
                return Halt("timeout", prev)
            halt = runner.step(self.mask)
            if halt.reason != "stepped":
                return halt
            now = halt.regs
            jumped = not 0 < now.pc - prev.pc <= 4
            if now.ipsr != prev.ipsr and now.ipsr != 0 and is_exc_return(now.lr):
                if now.ipsr in FAULTS:
                    return Halt("fault", now)      # 故障の入口: その場で止める（原因は target.fault）
                halt = self._over_exception(prev, now)
                if halt.reason != "arrived":
                    return halt
                now = halt.regs
                jumped = now.pc != prev.pc and not 0 < now.pc - prev.pc <= 4
            elif _is_call(prev, now):
                if mode == "into" and (body := self._step_in_target(now.pc)) is not None:
                    if body == now.pc:
                        return Halt("stepped", now)
                    halt = runner.run_to({body: lambda r: True})
                    return Halt("stepped", halt.regs) if halt.reason == "arrived" else halt
                sp_at_call = now.sp
                halt = runner.run_to({now.lr & ~1: lambda r: r.sp >= sp_at_call})
                if halt.reason != "arrived":
                    return halt
                now = halt.regs
                jumped = False                     # 呼び出しの次の命令（同じフレーム）
            entry = symbols.line_entry(now.pc)
            if entry is None:
                return Halt("stepped", now)        # 行の情報の無いところへ出た（-g の無い呼び出し元等）
            if jumped and self._returned(function, cfa0, now):
                return Halt("stepped", now)        # 呼び出し元へ戻った（呼び出しの行の途中でも止まる）
            if now.pc == entry.address and (entry.file, entry.line) != key:
                backward = now.pc < first.address and symbols.function_at(now.pc) == function
                if not (mode == "until" and backward):
                    return Halt("stepped", now)
            prev = now

    def _returned(self, function, cfa0: int | None, now: Regs) -> bool:
        if cfa0 is not None:
            cfa = self.runner.locked(self.frame_cfa)
            if cfa is not None:
                return cfa > cfa0
        return self.symbols.function_at(now.pc) != function

    def _step_in_target(self, pc: int) -> int | None:
        """呼ばれた関数に入って止まる番地（前置きの後）。行の情報が無ければ None（踏み越える）。"""
        if self.symbols.line_entry(pc) is None:
            return None
        function = self.symbols.function_at(pc)
        return self.symbols.after_prologue(pc) if function is not None and function.address == pc else pc

    def _over_exception(self, before: Regs, entered: Regs) -> Halt:
        """割り込みのハンドラを走らせ、積まれた戻り先（例外のフレームの PC）に元の IPSR で戻るまで待つ。"""
        frame = self.runner.locked(lambda: self.runner.target.read_core_register("psp")) if entered.lr & 0x4 \
            else entered.sp
        back = self.runner.read32(frame + 24) & ~1
        ipsr = before.ipsr
        return self.runner.run_to({back: lambda r: r.ipsr == ipsr})

    # --- 関数から出る -----------------------------------------------------------------------------

    def _out(self) -> Halt:
        site = self.runner.locked(self.caller)
        if site is not None:
            back, cfa = site
            return self.runner.run_to({back: lambda r: r.sp >= cfa})
        return self._out_by_steps()

    def _out_by_steps(self) -> Halt:
        """CFI が無い: 1 命令ずつ（呼び出し・割り込みは踏み越える）いまの関数から出るまで進める。"""
        runner = self.runner
        prev = runner.regs()
        function = self.symbols.function_at(prev.pc)
        if function is None:
            raise RuntimeError("いまの関数が分からない（ELF のシンボルにも CFI にも無い）: 呼び出し元へ出られない")
        while True:
            if runner.timed_out():
                return Halt("timeout", prev)
            halt = runner.step(self.mask)
            if halt.reason != "stepped":
                return halt
            now = halt.regs
            if now.ipsr != prev.ipsr and now.ipsr != 0 and is_exc_return(now.lr):
                if now.ipsr in FAULTS:
                    return Halt("fault", now)
                halt = self._over_exception(prev, now)
                if halt.reason != "arrived":
                    return halt
                now = halt.regs
            elif _is_call(prev, now):
                sp_at_call = now.sp
                halt = runner.run_to({now.lr & ~1: lambda r: r.sp >= sp_at_call})
                if halt.reason != "arrived":
                    return halt
                now = halt.regs
            elif self.symbols.function_at(now.pc) != function:
                return Halt("arrived", now)
            prev = now

    # --- 場所まで（いまの関数から戻るところでも止まる） --------------------------------------------

    def _advance(self, location: int) -> Halt:
        runner = self.runner
        cfa0 = runner.locked(self.frame_cfa)
        site = runner.locked(self.caller)

        def in_this_or_outer_frame(r: Regs) -> bool:   # 再帰の深い呼び出しの中の location では止まらない
            if cfa0 is None:
                return True
            cfa = runner.locked(self.frame_cfa)
            return cfa is None or cfa >= cfa0

        targets: dict[int, Callable[[Regs], bool]] = {location: in_this_or_outer_frame}
        if site is not None and site[0] != location:
            back, cfa = site
            targets[back] = lambda r: r.sp >= cfa
        return runner.run_to(targets)
