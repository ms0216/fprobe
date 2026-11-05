"""ターゲット（pyOCD の Cortex-M）のデバッグ操作を、場所の名前（関数名・変数名・ファイル:行）で行う。

AI が「この関数で止めて、変数を読んで、どこで止まったかを関数名と行で知る」を 1 段ずつ行えるようにする層。
fprobe.symbols（ELF）が無くても番地で使える。MCP（fprobe.mcp_server）はこれを道具として出す。

    dbg = Debugger(session_debug.target, Symbols("app.elf"))
    dbg.breakpoint("reached_milestone")
    stop = dbg.run_until_halt(timeout_s=2)     # Stop(reason="breakpoint", where=reached_milestone+0 (app.c:12))
    dbg.read_variable("counter")
    dbg.backtrace()                            # ELF に CFI があれば CFI で巻き戻す（例外のフレームも越える）
    dbg.evaluate("head->samples[1]", frame=0)  # C の式（DWARF の型付き。ローカル変数・引数も）
"""

import contextlib
import time
from dataclasses import dataclass, field, replace

from pyocd.core.target import Target

from .calling import FunctionCaller, hard_float, read_return_value, signature_of
from .debug_inspect import InspectMixin
from .debug_types import RESET_KINDS, VECTOR_CATCH, VTOR, XPSR_THUMB, _REASONS, _WATCH_KINDS, Frame, PyocdAccess, Stop, Watch  # noqa: F401
from .fault import dfsr_bits
from .stepping import DFSR, STEP_MODES, Halt, LineStepper, Runner, dfsr_reason
from .symbols import Location, Symbols, is_line_location

__all__ = ["Debugger", "Frame", "PyocdAccess", "RESET_KINDS", "Stop", "VECTOR_CATCH", "Watch"]


@dataclass
class Debugger(InspectMixin):
    target: object                # pyocd.coresight.cortex_m.CortexM
    symbols: Symbols | None = None
    breakpoints: dict[int, str] = field(default_factory=dict)    # 番地 → 指定した場所の名前
    watches: dict[int, Watch] = field(default_factory=dict)
    last_dfsr: int = 0            # 最後の 1 命令のステップで読んだ DFSR

    # --- 場所 ------------------------------------------------------------------------

    def address(self, location: str | int) -> int:
        if isinstance(location, int) or self.symbols is None:
            return location if isinstance(location, int) else int(location, 0)
        return self.symbols.resolve(location)

    def where(self, address: int) -> Location:
        return self.symbols.describe(address) if self.symbols else Location(address)

    # --- ブレークポイント・ウォッチポイント ------------------------------------------------

    def breakpoint(self, location: str | int) -> int:
        """場所にブレークポイントを置く（Flash の命令はハードの比較器、RAM はソフトの BKPT。pyOCD が選ぶ）。
        関数名なら前置きの後（引数・ローカル変数が読める場所）に置く。番地を指定すればその番地そのもの。"""
        address = self.breakpoint_address(location)
        if not self.target.set_breakpoint(address, Target.BreakpointType.AUTO):
            raise RuntimeError(f"0x{address:08X} にブレークポイントを置けない（比較器が足りない等）")
        self.breakpoints[address] = str(location)
        self._apply_breakpoints()
        return address

    def _apply_breakpoints(self) -> None:
        """pyOCD はブレークポイントの変更を溜めて、再開の直前に書く。走っている間は再開が来ないので、すぐ書く
        （実機で、走行中に置いたブレークポイントが効かなかった。2026-10-18）。止まっていれば再開のときに書かれる。"""
        core = getattr(self.target, "selected_core", None) or self.target    # pyOCD の SoC のターゲットはコアに任せる
        manager = getattr(core, "bp_manager", None)
        if manager is not None and self.target.get_state() != Target.State.HALTED:
            manager.flush()

    def breakpoint_address(self, location: str | int) -> int:
        """ブレークポイントの番地: 関数名なら前置きの後、"ファイル:行"・番地はそのまま。"""
        address = self.address(location)
        if isinstance(location, str) and self.symbols is not None and not location.strip().lower().startswith("0x") \
                and not is_line_location(location):
            address = self.symbols.after_prologue(address)
        return address

    def remove_breakpoint(self, location: str | int) -> None:
        address = self.breakpoint_address(location)
        self.target.remove_breakpoint(address)
        self.breakpoints.pop(address, None)
        self._apply_breakpoints()

    def watchpoint(self, location: str | int, size: int | None = None, kind: str = "write") -> Watch:
        """変数名か番地にウォッチポイントを置く。size は変数名なら省略で変数の大きさ（1 / 2 / 4）。番地なら必須
        （大きさを仮定しない）。"""
        address = self.address(location)
        if size is None:
            if not (self.symbols and isinstance(location, str) and not location.startswith("0x")):
                raise ValueError("番地のウォッチポイントには size（1 / 2 / 4）が要る")
            size = self.symbols.variable(location).size
        if kind not in _WATCH_KINDS:
            raise ValueError(f"kind は {', '.join(_WATCH_KINDS)} のどれか")
        previous = self.watches.get(address)
        if previous is not None:                      # 帳簿は番地ごとに 1 つ: 前の比較器を先に外す（残すと clear でも外れない）
            self.target.remove_watchpoint(address, previous.size, _WATCH_KINDS[previous.kind])
        if not self.target.set_watchpoint(address, size, _WATCH_KINDS[kind]):
            if previous is not None:                  # 置けなければ前のを戻す（帳簿と機器を一致させる）
                self.target.set_watchpoint(address, previous.size, _WATCH_KINDS[previous.kind])
            raise RuntimeError(f"0x{address:08X} にウォッチポイントを置けない（比較器が足りない・大きさが不正）")
        watch = Watch(address, size, kind)
        self.watches[address] = watch
        return watch

    def remove_watchpoint(self, location: str | int) -> None:
        address = self.address(location)
        watch = self.watches.pop(address, None)
        if watch is not None:
            self.target.remove_watchpoint(address, watch.size, _WATCH_KINDS[watch.kind])

    def clear(self) -> None:
        """置いたブレークポイント・ウォッチポイントをすべて外す。"""
        for address in list(self.breakpoints):
            self.remove_breakpoint(address)
        for address in list(self.watches):
            self.remove_watchpoint(address)

    # --- 実行 ------------------------------------------------------------------------

    def resume(self) -> None:
        """再開する。いまの PC にブレークポイントがあれば、まず 1 命令踏み越える（ソフトの BKPT で止まり直さない）。
        踏み越えた 1 命令で DFSR に通常のステップ以外の理由（ベクタキャッチ・ウォッチポイント・BKPT・外部の停止）が
        立てば、再開せずその停止を残す。"""
        if self.target.get_state() == Target.State.HALTED:
            pc = self.target.read_core_register("pc")
            if pc in self.breakpoints and self._step_instruction() is not None:
                return
        self.target.resume()

    def _step_instruction(self) -> str | None:
        """1 命令進め、止まった理由を DFSR（機器の事実）で返す（通常のステップだけなら None）。いまの PC に置いた
        ブレークポイントは進める間だけ外し、失敗しても必ず置き直す（帳簿と機器を一致させる）。"""
        pc = self.target.read_core_register("pc")
        lifted = pc in self.breakpoints
        if lifted:
            self.target.remove_breakpoint(pc)
        try:
            self.target.step()
        finally:
            if lifted:
                self.target.set_breakpoint(pc, Target.BreakpointType.AUTO)
        self.last_dfsr = self.target.read32(DFSR)
        return dfsr_reason(self.last_dfsr, stepping=True)

    def run_until_halt(self, timeout_s: float = 5.0, poll_s: float = 0.005,
                       lock: contextlib.AbstractContextManager | None = None) -> Stop:
        """再開し、止まるか timeout_s が過ぎるまで待つ。時間切れなら止めて reason = "timeout" を返す。
        lock を渡すと、機器に触る間だけそれを持ち、待つ間は放す（同じ機器を使う背景の読み（RTT 等）を止めない）。"""
        lock = lock if lock is not None else contextlib.nullcontext()
        start = time.monotonic()
        with lock:
            self.resume()
        while time.monotonic() - start < timeout_s:
            with lock:
                if self.target.get_state() == Target.State.HALTED:
                    return self._stop(start, _REASONS.get(self.target.get_halt_reason(), "unknown"))
            time.sleep(poll_s)
        with lock:
            if self.target.get_state() == Target.State.HALTED:      # 最後の確認の後に自分で止まっていた: 理由を失わない
                return self._stop(start, _REASONS.get(self.target.get_halt_reason(), "unknown"))
            self.target.halt()
            return self._stop(start, "timeout")

    def step(self) -> Stop:
        """1 命令進める。reason は step か、その命令で止まった理由（DFSR。vector_catch / watchpoint / breakpoint / external）。"""
        start = time.monotonic()
        reason = self._step_instruction()
        return replace(self._stop(start, reason or "step"), halt_bits=dfsr_bits(self.last_dfsr))

    def step_instructions(self, count: int) -> int:
        """1 命令ずつ count 回進める。通常のステップ以外の理由で止まったらそこで打ち切る。実行した回数を返す
        （止まった理由は DFSR / target_status で読む）。"""
        done = 0
        while done < count:
            done += 1
            if self._step_instruction() is not None:
                break
        return done

    def step_line(self, mode: str = "over", location: str | int | None = None, timeout_s: float = 5.0,
                  mask_interrupts: bool = True, lock: contextlib.AbstractContextManager | None = None,
                  on_breakpoint=None) -> Stop:
        """ソース行単位のステップ（fprobe.stepping）。mode: into / over / out / until。until に location（関数名・
        "ファイル:行"・番地）を渡すとそこまで（いまの関数から戻れば、そこでも止まる）。mask_interrupts: 1 命令ずつ
        進める間は割り込みを止める（DHCSR.C_MASKINTS）。reason は step（終えた）か、途中で止まった理由
        （breakpoint / watchpoint / vector_catch / fault / timeout 等）。out で戻れば value に戻り値。
        lock を渡すと、機器に触る間だけそれを持つ（呼び手は持たずに呼ぶ）。on_breakpoint は Runner と同じ。"""
        if mode not in STEP_MODES:
            raise ValueError(f"mode は {', '.join(STEP_MODES)} のどれか")
        if self.symbols is None:
            raise RuntimeError("ELF が読み込まれていない: 行単位のステップには行表が要る（命令単位は target_step）")
        lock = lock if lock is not None else contextlib.nullcontext()
        start = time.monotonic()
        runner = Runner(self.target, self.breakpoints, lock, start + timeout_s, on_breakpoint)
        with lock:
            if self.target.get_state() != Target.State.HALTED:
                raise RuntimeError("ターゲットが止まっていない（target_halt の後に呼ぶ）")
            pc = self.target.read_core_register("pc")
        address = self.breakpoint_address(location) if location is not None else None
        stepper = LineStepper(runner, self.symbols, self._frame_cfa, self._caller_site, mask_interrupts)
        halt = stepper.step(mode, address)
        value = None
        if mode == "out" and halt.reason == "arrived":
            with lock:
                value = self._returned_value(pc)
        return self._halt_stop(halt, start, value, runner.dfsr, runner.steps, runner.runs)

    def _halt_stop(self, halt: Halt, start: float, value: dict | None = None, dfsr: int | None = None,
                   steps: int | None = None, runs: int | None = None) -> Stop:
        reason = "step" if halt.reason in ("stepped", "arrived") else halt.reason
        return Stop(reason, halt.regs.pc, self.where(halt.regs.pc), round(time.monotonic() - start, 4), value,
                    None if dfsr is None else dfsr_bits(dfsr), steps, runs)

    def _frame_cfa(self) -> int | None:
        """いまのフレームの CFA（CFI。無ければ None）。CFA は呼び出し元へ 1 段巻き戻すときに決まるので 2 段まで。"""
        inspector = self.inspector(max_frames=2)
        if inspector is None or not len(inspector.unwinder.table):
            return None
        try:
            return inspector.frames()[0].cfa
        except Exception:                                         # 巻き戻せない（CFI の外・壊れたスタック）
            return None

    def _caller_site(self) -> tuple[int, int] | None:
        """(戻り先, いまのフレームの CFA)。CFI で呼び出し元が分からなければ None。"""
        inspector = self.inspector(max_frames=2)
        if inspector is None or not len(inspector.unwinder.table):
            return None
        try:
            frames = inspector.frames()
        except Exception:
            return None
        if len(frames) < 2 or frames[0].cfa is None:
            return None
        return frames[1].pc & ~1, frames[0].cfa

    def _returned_value(self, pc_in_function: int) -> dict | None:
        """step out で出た関数の戻り値（DWARF の戻り値の型と AAPCS の置き場所）。分からなければ None。"""
        info = self.debug_info()
        function = info.function_at(pc_in_function) if info is not None else None
        if function is None:
            return None
        return read_return_value(self.target, info, info.types.type_of(function.die), hard_float(info.elf))

    def _stop(self, start: float, reason: str) -> Stop:
        pc = self.target.read_core_register("pc")
        return Stop(reason, pc, self.where(pc), round(time.monotonic() - start, 4))

    # --- リセット・ベクタキャッチ ------------------------------------------------------------------

    def reset(self, kind: str = "default", halt: bool = False) -> None:
        """リセットする。kind: default / system（AIRCR.SYSRESETREQ。周辺も）/ core（AIRCR.VECTRESET。コアだけ。
        ARMv7-M だけ）/ hardware（nRESET の線）/ emulated（デバッガがレジスタを初期値にする。周辺はそのまま）。
        halt なら入口（リセットのベクタキャッチ）で止める。コアに無い種類は黙って替えずに断る。"""
        if kind not in RESET_KINDS:
            raise ValueError(f"kind は {', '.join(RESET_KINDS)} のどれか")
        reset_type = RESET_KINDS[kind]
        core = getattr(self.target, "selected_core", None) or self.target   # pyOCD の SoC のターゲットはコアが知っている
        if reset_type is not None and reset_type not in core.supported_reset_types:
            raise ValueError(f"{kind} のリセットはこのコアに無い（core = VECTRESET は ARMv7-M だけ）")
        if halt:
            self.target.reset_and_halt(reset_type)
        else:
            self.target.reset(reset_type)

    def vector_catch(self, catch: list[str] | None = None) -> dict:
        """ベクタキャッチ（例外の入口でコアを止める）。catch を渡すとその集合にする（"all" ですべて、[] で無し）。
        返す enabled はコアから読み直したもの。コアに無いもの（ARMv6-M は hard_fault・core_reset だけ）は unsupported。"""
        names = None
        if catch is not None:
            names = list(VECTOR_CATCH) if "all" in catch else list(dict.fromkeys(catch))
            unknown = [n for n in names if n not in VECTOR_CATCH]
            if unknown:
                raise ValueError(f"知らないベクタキャッチ {unknown}（{', '.join(VECTOR_CATCH)}、all）")
            mask = 0
            for n in names:
                mask |= VECTOR_CATCH[n]
            self.target.set_vector_catch(mask)
        mask = self.target.get_vector_catch()
        out = {"enabled": [n for n, bit in VECTOR_CATCH.items() if mask & bit], "available": list(VECTOR_CATCH)}
        if names is not None:
            out["unsupported"] = [n for n in names if not mask & VECTOR_CATCH[n]]
        return out

    # --- 関数の呼び出し ---------------------------------------------------------------------

    def call(self, location: str | int, arguments=(), timeout_s: float = 2.0, disable_interrupts: bool = False,
             lock: contextlib.AbstractContextManager | None = None, on_breakpoint=None) -> dict:
        """止まったコアで関数を引数付きで実行し、戻り値を得る（fprobe.calling）。終われば（時間切れ・故障で打ち切っても）
        レジスタと、引数に使ったスタックの下のメモリを元に戻す。arguments の各要素: 数、C の式（文字列。いまの
        フレームで評価）、{"string": 文字列}・{"bytes": 16 進}（スタックの下に置いてその番地を渡す）。
        呼んだ関数の中の利用者のブレークポイント・ウォッチポイントでは止まらない。disable_interrupts: 呼ぶ間は
        PRIMASK = 1（割り込みを止める）。lock・on_breakpoint は step_line と同じ。"""
        lock = lock if lock is not None else contextlib.nullcontext()
        info = self.debug_info()
        if isinstance(location, str) and location.strip().lower().startswith("0x"):
            location = int(location, 16)
        signature = signature_of(info, self.symbols, location)

        def passing() -> bool:                                    # 利用者のブレークポイントは止まらずに続ける
            if self.target.read_core_register("pc") in self.breakpoints:
                return True
            return on_breakpoint is not None and on_breakpoint()

        def evaluate(expression: str):
            return self._require_inspector().number(expression)

        runner = Runner(self.target, self.breakpoints, lock, time.monotonic() + timeout_s, passing)
        caller = FunctionCaller(self.target, runner, info, self.symbols, evaluate if info is not None else None)
        return caller.call(signature, list(arguments), disable_interrupts)

    # --- RAM へ読み込んで起動 -------------------------------------------------------------

    def load_to_ram(self, vector_table: int | None = None) -> tuple[Stop, dict]:
        """ELF の中身を RAM に書き、起動できる状態で止める。vector_table（ベクタ表の番地。使う側が明示する）を渡せば
        その表の初期の MSP・リセットの処理から（VTOR もその番地に）、渡さなければ ELF のヘッダの入口（e_entry。リンカが
        書いた事実）から起動する（GDB の load と同じ。SP・VTOR は変えない）。ベクタ表を「最も低い番地の区画」等と
        仮定しない。Flash には書かない（Flash は pyOCD の FileProgrammer = MCP の flash を使う）。
        返す: (止まった様子, {"from": "vector_table" / "entry", "entry", "vector_table"})。"""
        if self.symbols is None:
            raise RuntimeError("ELF が読み込まれていない")
        # 先にリセットして止める: 元のファームウェアが動かしていた割り込み（SysTick・周辺）が、差し替えたベクタ表へ
        # 飛んでこないように（2026-10-17 に実機で、読み込んだプログラムが既定の例外処理に落ちた）
        self.target.reset_and_halt()
        for segment in self.symbols.segments:
            self.target.write_memory_block8(segment.address, list(segment.data))
        entry = self.symbols.entry
        if vector_table is not None:
            sp, pc = self.target.read_memory_block32(vector_table, 2)
            self.target.write32(VTOR, vector_table)
            self.target.write_core_register("msp", sp)
            self.target.write_core_register("sp", sp)
        else:
            pc = entry
        self.target.write_core_register("pc", pc & ~1)
        self.target.write_core_register("xpsr", XPSR_THUMB)
        self.target.flush()
        start = {"from": "entry" if vector_table is None else "vector_table", "entry": entry,
                 "vector_table": vector_table}
        return self._stop(time.monotonic(), "user"), start
