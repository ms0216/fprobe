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
import functools
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from pyocd.core import exceptions as pyocd_errors
from pyocd.core.target import Target

from .calling import FunctionCaller, hard_float, read_return_value, signature_of
from .dwarf.access import AccessError
from .dwarf.info import DebugInfo
from .dwarf.inspector import Inspector
from .stepping import STEP_MODES, Halt, LineStepper, Runner
from .symbols import Location, Symbols, is_line_location

__all__ = ["Debugger", "Frame", "PyocdAccess", "RESET_KINDS", "Stop", "VECTOR_CATCH", "Watch"]


@functools.lru_cache(maxsize=4)
def _load_debug_info(path: str, stamp: tuple[int, int]) -> DebugInfo | None:
    """ELF の DWARF の索引（パスと更新時刻・大きさで共有。Debugger を作り直しても読み直さない）。無ければ None。"""
    try:
        return DebugInfo(path)
    except ValueError:
        return None

_WATCH_KINDS = {"read": Target.WatchpointType.READ, "write": Target.WatchpointType.WRITE,
                "access": Target.WatchpointType.READ_WRITE}
_REASONS = {Target.HaltReason.BREAKPOINT: "breakpoint", Target.HaltReason.WATCHPOINT: "watchpoint",
            Target.HaltReason.DEBUG: "step", Target.HaltReason.USER: "user",
            Target.HaltReason.VECTOR_CATCH: "vector_catch", Target.HaltReason.EXTERNAL: "external"}
# ベクタキャッチの名前 → pyOCD の VectorCatch（DEMCR の VC_CORERESET・VC_MMERR・VC_NOCPERR・VC_CHKERR・VC_STATERR・
# VC_BUSERR・VC_INTERR・VC_HARDERR・VC_SFERR）
VECTOR_CATCH = {"core_reset": Target.VectorCatch.CORE_RESET, "mem_manage": Target.VectorCatch.MEM_FAULT,
                "no_coprocessor": Target.VectorCatch.COPROCESSOR_ERR, "check_error": Target.VectorCatch.CHECK_ERR,
                "state_error": Target.VectorCatch.STATE_ERR, "bus_fault": Target.VectorCatch.BUS_FAULT,
                "interrupt_error": Target.VectorCatch.INTERRUPT_ERR, "hard_fault": Target.VectorCatch.HARD_FAULT,
                "secure_fault": Target.VectorCatch.SECURE_FAULT}
# リセットの種類 → pyOCD の ResetType（default は pyOCD の既定 = セッションの reset_type かコアの既定）
RESET_KINDS = {"default": None, "system": Target.ResetType.SYSRESETREQ, "core": Target.ResetType.VECTRESET,
               "hardware": Target.ResetType.HARDWARE, "emulated": Target.ResetType.EMULATED}
VTOR = 0xE000ED08
XPSR_THUMB = 1 << 24


@dataclass(frozen=True)
class Stop:
    """実行が止まったときの様子。reason: breakpoint / watchpoint / step / user / timeout / vector_catch / external。"""
    reason: str
    pc: int
    where: Location
    elapsed_s: float
    value: dict | None = None     # step_line の out: 出た関数の戻り値（{"type", "value"}）


@dataclass(frozen=True)
class Watch:
    address: int
    size: int
    kind: str                     # read / write / access


@dataclass(frozen=True)
class Frame:
    pc: int                       # 呼び出し元のフレームでは戻り先、例外で中断された処理では正確な番地
    where: Location               # 呼び出し元では呼び出し命令の行（戻り先 − 1 で引く）
    note: str = ""                # "exception" = 例外の入口で積まれたフレームから復元した位置
    cfa: int | None = None        # CFI で巻き戻したときの CFA（呼び出し元の sp）


class PyocdAccess:
    """pyOCD のターゲット（CortexM）を fprobe.dwarf の読み手（TargetAccess / WritableAccess）に包む。"""

    def __init__(self, target):
        self.target = target

    def read_memory(self, address: int, length: int) -> bytes:
        try:
            return bytes(self.target.read_memory_block8(address, length))
        except pyocd_errors.Error as e:
            raise AccessError(f"0x{address:08X} の {length} バイト: {e}") from None

    def write_memory(self, address: int, data: bytes) -> None:
        try:
            self.target.write_memory_block8(address, list(data))
            self.target.flush()
        except pyocd_errors.Error as e:
            raise AccessError(f"0x{address:08X} への書き込み: {e}") from None

    def read_register(self, name: str) -> int:
        if name.startswith("d") and name[1:].isdigit():          # d*n* = s*2n*:s*2n+1*（単精度の FPU にも d の名前で）
            n = int(name[1:])
            return self.read_register(f"s{2 * n}") | self.read_register(f"s{2 * n + 1}") << 32
        try:
            return self.target.read_core_register_raw(name)
        except (pyocd_errors.Error, KeyError, ValueError) as e:
            raise AccessError(f"レジスタ {name}: {e}") from None


@dataclass
class Debugger:
    target: object                # pyocd.coresight.cortex_m.CortexM
    symbols: Symbols | None = None
    breakpoints: dict[int, str] = field(default_factory=dict)    # 番地 → 指定した場所の名前
    watches: dict[int, Watch] = field(default_factory=dict)

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
        """変数名か番地にウォッチポイントを置く。size は変数名なら変数の大きさ（1 / 2 / 4）。"""
        address = self.address(location)
        if size is None:
            size = self.symbols.variable(location).size if self.symbols and isinstance(location, str) \
                and not location.startswith("0x") else 4
        if kind not in _WATCH_KINDS:
            raise ValueError(f"kind は {', '.join(_WATCH_KINDS)} のどれか")
        if not self.target.set_watchpoint(address, size, _WATCH_KINDS[kind]):
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
        """再開する。いまの PC にブレークポイントがあれば、まず 1 命令踏み越える（ソフトの BKPT で止まり直さない）。"""
        if self.target.get_state() == Target.State.HALTED:
            pc = self.target.read_core_register("pc")
            if pc in self.breakpoints:
                self.target.remove_breakpoint(pc)
                self.target.step()
                self.target.set_breakpoint(pc, Target.BreakpointType.AUTO)
                if self.target.get_halt_reason() == Target.HaltReason.WATCHPOINT:
                    return                              # 踏み越えた 1 命令でウォッチポイントが成立: その停止を残す
        self.target.resume()

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
            self.target.halt()
            return self._stop(start, "timeout")

    def step(self) -> Stop:
        start = time.monotonic()
        self.target.step()
        return self._stop(start, "step")

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
        return self._halt_stop(halt, start, value)

    def _halt_stop(self, halt: Halt, start: float, value: dict | None = None) -> Stop:
        reason = "step" if halt.reason in ("stepped", "arrived") else halt.reason
        return Stop(reason, halt.regs.pc, self.where(halt.regs.pc), round(time.monotonic() - start, 4), value)

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

    # --- 変数 ------------------------------------------------------------------------

    def read_variable(self, name: str, index: int | None = None) -> int | bytes:
        """変数を読む。大きさ 1 / 2 / 4 / 8 なら符号なし整数、それ以外（配列・構造体）はバイト列。
        index を指定すると 4 バイトの要素の配列として index 番目を読む。"""
        var = self.symbols.variable(name)
        if index is not None:
            if not 0 <= index * 4 < var.size:
                raise IndexError(f"{name} は {var.size // 4} 要素")
            return self.target.read32(var.address + 4 * index)
        data = bytes(self.target.read_memory_block8(var.address, var.size))
        return int.from_bytes(data, "little") if var.size in (1, 2, 4, 8) else data

    def write_variable(self, name: str, value: int, index: int | None = None) -> None:
        var = self.symbols.variable(name)
        if index is not None:
            self.target.write32(var.address + 4 * index, value)
        else:
            if var.size not in (1, 2, 4, 8):
                raise ValueError(f"{name} は {var.size} バイト。整数として書けるのは 1 / 2 / 4 / 8 バイト")
            self.target.write_memory_block8(var.address, list(value.to_bytes(var.size, "little")))
        self.target.flush()

    # --- 呼び出しの様子 ------------------------------------------------------------------

    def backtrace(self, max_frames: int = 64, inspector: Inspector | None = None) -> list[Frame]:
        """呼び出し履歴。ELF に CFI（.debug_frame）があれば CFI で巻き戻す（例外のフレームも越える）。
        無ければ簡易版（いまの PC、LR。例外の中なら積まれたフレームの PC・LR）。停止中に呼ぶ。
        inspector を渡すとその巻き戻しを使う（続けて同じ窓口でフレームの変数を読むとき、巻き戻しは 1 回）。"""
        inspector = inspector if inspector is not None else self.inspector(max_frames)
        if inspector is not None and len(inspector.unwinder.table):
            frames = []
            for state in inspector.frames():
                where = self.where(state.lookup_pc)
                if state.lookup_pc != state.pc:                      # 呼び出し元: 番地は戻り先、行は呼び出し命令
                    where = replace(where, address=state.pc,
                                    offset=None if where.offset is None else where.offset + state.pc - state.lookup_pc)
                frames.append(Frame(state.pc, where, state.note, state.cfa))
            return frames
        return self._simple_backtrace()

    def _simple_backtrace(self) -> list[Frame]:
        regs = dict(zip(("pc", "lr", "xpsr", "msp", "psp"),
                        self.target.read_core_registers_raw(["pc", "lr", "xpsr", "msp", "psp"])))
        frames = [Frame(regs["pc"], self.where(regs["pc"]))]
        lr = regs["lr"]
        if regs["xpsr"] & 0x1FF and lr >> 24 == 0xFF:             # 例外の中で、LR が EXC_RETURN
            sp = regs["psp"] if lr & 0x4 else regs["msp"]
            stacked = self.target.read_memory_block32(sp, 8)       # r0 r1 r2 r3 r12 lr pc xpsr
            frames.append(Frame(stacked[6], self.where(stacked[6]), "exception"))
            frames.append(Frame(stacked[5] & ~1, self.where(stacked[5] & ~1)))
        else:
            frames.append(Frame(lr & ~1, self.where(lr & ~1)))
        return frames

    # --- ソースの言葉の変数（DWARF） ---------------------------------------------------------

    def debug_info(self) -> DebugInfo | None:
        """ELF の DWARF（型・変数・CFI）。DWARF が無い・ELF が無いなら None。symbols を替えれば、その ELF のものになる。"""
        if self.symbols is None or getattr(self.symbols, "path", None) is None:
            return None
        path = Path(self.symbols.path)
        stat = path.stat()
        return _load_debug_info(str(path), (stat.st_mtime_ns, stat.st_size))

    def inspector(self, max_frames: int = 64) -> Inspector | None:
        """いまの停止の様子を調べる窓口（停止のたびに作る）。DWARF が無ければ None。"""
        info = self.debug_info()
        if info is None:
            return None
        return Inspector(info, PyocdAccess(self.target), self.symbols, max_frames)

    def _require_inspector(self) -> Inspector:
        inspector = self.inspector()
        if inspector is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: 式・ローカル変数は読めない")
        return inspector

    def frame_variables(self, frame: int = 0, max_elements: int = 64) -> dict:
        """そのフレームの引数とローカル変数: {"function", "arguments": [{name, type, value}], "locals": [...]}。"""
        return self._require_inspector().frame_variables(frame, max_elements)

    def evaluate(self, expression: str, frame: int = 0, max_elements: int = 64, live: bool = False) -> dict:
        """C の式を評価して読む: {"expression", "type", "address"（メモリ上なら）, "value"}。live: フレームを使わずに
        読む（走っているコア。大域・静的の変数とメモリだけ）。"""
        if live:
            return self._require_inspector().read_global(expression, max_elements)
        return self._require_inspector().read(expression, frame, max_elements)

    def write_expression(self, expression: str, value: int | float | str, frame: int = 0) -> None:
        """C の式の指す値（メモリ上の基本型・列挙（名前でも）・ポインタ・ビットフィールド）に書く。"""
        self._require_inspector().write(expression, value, frame)

    def type_info(self, name: str) -> dict:
        """型の名前（"struct Sensor"・typedef 名等）→ 大きさ・メンバー・列挙子。"""
        return self._require_inspector().type_info(name)

    # --- RAM へ読み込んで起動 -------------------------------------------------------------

    def load_to_ram(self) -> Stop:
        """ELF の中身を RAM に書き、そのベクタ表（ELF の最初の区画）から起動できる状態で止める。
        Flash には書かない（Flash は pyOCD の FileProgrammer = MCP の flash を使う）。"""
        if self.symbols is None:
            raise RuntimeError("ELF が読み込まれていない")
        # 先にリセットして止める: 元のファームウェアが動かしていた割り込み（SysTick・周辺）が、差し替えたベクタ表へ
        # 飛んでこないように（2026-10-17 に実機で、読み込んだプログラムが既定の例外処理に落ちた）
        self.target.reset_and_halt()
        for segment in self.symbols.segments:
            self.target.write_memory_block8(segment.address, list(segment.data))
        vectors = min(s.address for s in self.symbols.segments)
        sp, pc = self.target.read_memory_block32(vectors, 2)
        self.target.write32(VTOR, vectors)
        self.target.write_core_register("msp", sp)
        self.target.write_core_register("sp", sp)
        self.target.write_core_register("pc", pc & ~1)
        self.target.write_core_register("xpsr", XPSR_THUMB)
        self.target.flush()
        return self._stop(time.monotonic(), "user")
