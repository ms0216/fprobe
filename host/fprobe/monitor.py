"""モニタ方式（止めずにデバッグする。J-Link の Monitor Mode Debugging の相当）のホスト側の手続き。

ターゲットのファームに組み込んだモニタ（host/firmware/monitor/fprobe_monitor.c）が、デバッグの出来事を DebugMonitor 例外
（DebugMon）で受け、その中で待つ。割り込まれた処理のレジスタはモニタの箱（RAM の struct fprobe_monitor）にある。
ホストは箱を SWD で直接読み書きし（コアは走ったまま。AHB-AP のアクセス）、再開・1 命令だけを箱の命令で頼む。

- find_box: ベクタ表の DebugMon（VTOR + 0x30）が指すハンドラの頭の印（"FPMN" と箱の番地）から箱を見つける。ELF は要らない。
- MonitorTarget: pyOCD の CortexM の口（get_state・halt・resume・step・read/write_core_register(s)(_raw)・
  get_halt_reason・set/remove_breakpoint・read32 等）を、箱の読み書きで実現する包み。Debugger・行単位のステップ・
  関数の呼び出し・セミホスティング・GDB サーバは、これをそのまま使えばモニタ方式で動く。
- enter_monitor / leave_monitor: 停止方式（C_DEBUGEN = 1）とモニタ方式（C_DEBUGEN = 0、DEMCR.MON_EN = 1）の切り替え。
  止まっていれば、止まった場所を保ったまま切り替える。

仕様: docs/superpowers/specs/2026-10-18-u37-monitor-mode-design.md
"""

import struct
import time
from dataclasses import dataclass

from pyocd.core.target import Target

from .flash_bp import FlashBreakpoints

__all__ = ["BOX_SIZE", "MAGIC", "MonitorError", "MonitorTarget", "Offsets", "VERSION", "check_architecture",
           "enter_monitor", "find_box", "keeps_running", "leave_monitor", "read_priority", "write_priority"]

MAGIC = 0x4E4D5046                        # "FPMN"
VERSION = 1
BOX_SIZE = 0x12C
CMD_GO, CMD_STEP = 1, 2
STEP_MASK_INTERRUPTS = 1
FLAG_FPU, FLAG_STACK_OVERFLOW = 1, 2

DHCSR, DFSR, DEMCR, VTOR = 0xE000EDF0, 0xE000ED30, 0xE000EDFC, 0xE000ED08
SHPR1, SHPR2, SHPR3, SHCSR, ICSR = 0xE000ED18, 0xE000ED1C, 0xE000ED20, 0xE000ED24, 0xE000ED04
SYST_CSR, ICTR, NVIC_ISER, NVIC_IPR = 0xE000E010, 0xE000E004, 0xE000E100, 0xE000E400
DBGKEY, C_DEBUGEN, C_HALT = 0xA05F0000, 1 << 0, 1 << 1
S_HALT = 1 << 17
TRCENA, MON_EN, MON_PEND, MON_STEP = 1 << 24, 1 << 16, 1 << 17, 1 << 18
DFSR_HALTED, DFSR_BKPT, DFSR_DWTTRAP, DFSR_VCATCH, DFSR_EXTERNAL = 0x1, 0x2, 0x4, 0x8, 0x10
EXC_RETURN_PSP, EXC_RETURN_BASIC = 1 << 2, 1 << 4
XPSR_ALIGNED = 1 << 9
HANDLER_SCAN = 16                         # ハンドラの頭の印を探す大きさ（バイト）


class Offsets:
    """箱（struct fprobe_monitor、版 1）のメンバーの位置。C の _Static_assert と試験（DWARF）で照合する。"""
    magic, version, size, flags = 0x00, 0x04, 0x08, 0x0C
    state, reason, stops = 0x10, 0x14, 0x18
    seq_cmd, cmd, arg, seq_ack, result = 0x1C, 0x20, 0x24, 0x28, 0x2C
    exc_return, msp, psp = 0x30, 0x34, 0x38
    primask, basepri, faultmask, control = 0x3C, 0x40, 0x44, 0x48
    sp, frame, r4_r11, s16_s31 = 0x4C, 0x50, 0xB8, 0xD8
    exit_frame, step_basepri, step_forced, prio_bits, stack_size = 0x118, 0x11C, 0x120, 0x124, 0x128


_FRAME_NAMES = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"] + [f"s{i}" for i in range(16)] + ["fpscr"]
_FRAME_INDEX = {name: i for i, name in enumerate(_FRAME_NAMES)}
_ALIASES = {"r13": "sp", "r14": "lr", "r15": "pc"}
_FP_NAMES = {f"s{i}" for i in range(32)} | {"fpscr"}
_REASONS = ((DFSR_VCATCH, Target.HaltReason.VECTOR_CATCH), (DFSR_DWTTRAP, Target.HaltReason.WATCHPOINT),
            (DFSR_BKPT, Target.HaltReason.BREAKPOINT), (DFSR_EXTERNAL, Target.HaltReason.EXTERNAL),
            (DFSR_HALTED, Target.HaltReason.DEBUG))
_SYSTEM_HANDLERS = {4: "MemManage", 5: "BusFault", 6: "UsageFault", 11: "SVCall", 14: "PendSV", 15: "SysTick"}


class MonitorError(RuntimeError):
    """モニタ方式の誤り（モニタが無い・入らない・モニタ方式ではできない操作）。"""


def _w(words: list[int], offset: int) -> int:
    return words[offset // 4]


# --- 見つける・調べる ------------------------------------------------------------------------------


def check_architecture(target) -> None:
    """DebugMonitor のあるコア（ARMv7-M・ARMv8-M Mainline）か。分からなければ（偽物等）通す。"""
    core = getattr(target, "selected_core", None) or target
    arch = getattr(core, "architecture", None)
    if arch is not None and getattr(arch, "name", "") in ("ARMv6M", "ARMv8M_BASE"):
        raise MonitorError(f"このコア（{arch.name}）に DebugMonitor 例外が無い: モニタ方式は Cortex-M3 / M4 / M7 / M33 等だけ")


def find_box(target) -> int:
    """ベクタ表の DebugMon のハンドラの頭の印から箱の番地を引き、箱の magic と版を確かめる。"""
    table = target.read32(VTOR) & ~0x7F
    handler = target.read32(table + 4 * 12)
    if handler == 0 or not handler & 1:
        raise MonitorError(f"ベクタ表（0x{table:08X}）の DebugMon（12 番）にハンドラが無い: ファームにモニタ"
                           "（host/firmware/monitor/fprobe_monitor.c）を組み込み、DebugMon_Handler をベクタ表に置く。Zephyr は host/firmware/monitor/zephyr のモジュール")
    start = (handler & ~1) & ~3
    words = target.read_memory_block32(start, HANDLER_SCAN // 4 + 2)
    box = None
    for i in range(len(words) - 1):
        if words[i] == MAGIC:
            box = words[i + 1]
            break
    if box is None:
        raise MonitorError(f"DebugMon のハンドラ（0x{handler & ~1:08X}）が fprobe のモニタではない: ファームにモニタ"
                           "（host/firmware/monitor/fprobe_monitor.c）を組み込む")
    magic, version, size = target.read_memory_block32(box, 3)
    if magic == 0:
        raise MonitorError(f"モニタの箱（0x{box:08X}）が初期化されていない: ファームの起動時に fprobe_monitor_init(優先度) を呼ぶ")
    if magic != MAGIC or version != VERSION or size != BOX_SIZE:
        raise MonitorError(f"モニタの箱（0x{box:08X}）の版が違う（magic 0x{magic:08X}・版 {version}・大きさ {size}。"
                           f"ホストは版 {VERSION}）: ファームのモニタを host/firmware/monitor の版で作り直す")
    return box


def read_priority(target) -> int:
    """DebugMon の優先度（SHPR3 の下位 8 ビット）。"""
    return target.read32(SHPR3) & 0xFF


def write_priority(target, priority: int) -> int:
    """DebugMon の優先度を書き、読み直した値（実装のビットに丸めたもの）を返す。"""
    if not 0 <= priority <= 0xFF:
        raise ValueError("priority は 0〜255")
    target.write_memory_block8(SHPR3, [priority])
    target.flush()
    return read_priority(target)


def keeps_running(target, priority: int) -> list[dict]:
    """DebugMon（優先度 priority）より高い優先度で有効な例外・割り込み（止まっている間も動くもの）。"""
    shpr = target.read_memory_block32(SHPR1, 3)
    shcsr, systick = target.read32(SHCSR), target.read32(SYST_CSR)
    prio = {n: shpr[(n - 4) // 4] >> 8 * ((n - 4) % 4) & 0xFF for n in _SYSTEM_HANDLERS}
    enabled = {4: bool(shcsr & 1 << 16), 5: bool(shcsr & 1 << 17), 6: bool(shcsr & 1 << 18), 11: True, 14: True,
               15: systick & 3 == 3}
    out = [{"exception": n, "name": name, "priority": prio[n]} for n, name in _SYSTEM_HANDLERS.items()
           if enabled[n] and prio[n] < priority]
    lines = 32 * ((target.read32(ICTR) & 0xF) + 1)
    iser = target.read_memory_block32(NVIC_ISER, lines // 32)
    ipr = bytes(target.read_memory_block8(NVIC_IPR, lines))
    for irq in range(lines):
        if iser[irq // 32] >> irq % 32 & 1 and ipr[irq] < priority:
            out.append({"exception": 16 + irq, "name": f"IRQ{irq}", "priority": ipr[irq]})
    return out


# --- 切り替え ------------------------------------------------------------------------------------


def _wait(predicate, timeout_s: float, poll_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def enter_monitor(raw, box: int, timeout_s: float = 0.5, poll_s: float = 0.0005) -> None:
    """停止方式 → モニタ方式。raw は pyOCD のターゲット（停止方式の口）。止まっていれば同じ PC でモニタの停止にする
    （MON_PEND を立ててから再開すると、最初の命令より前に DebugMon に入る）。"""
    halted = raw.get_state() == Target.State.HALTED
    demcr = raw.read32(DEMCR) & ~(MON_STEP | MON_PEND)
    raw.write32(DEMCR, demcr | TRCENA | MON_EN | (MON_PEND if halted else 0))
    raw.write32(DFSR, 0x1F)                                    # 停止方式の理由を消す（モニタは入口で DFSR を写す）
    if halted:
        raw.resume()
    raw.write32(DHCSR, DBGKEY)                                 # C_DEBUGEN = 0: デバッグの出来事は DebugMon へ
    raw.flush()
    if halted and not _wait(lambda: raw.read32(box + Offsets.state) == 1, timeout_s, poll_s):
        raise MonitorError("モニタ方式に切り替えたが DebugMon に入らない（優先度の高い処理・割り込み禁止の区間に留まって"
                           "いる）。target_debug_mode_get の priority を確かめる")


def leave_monitor(monitor: "MonitorTarget", raw) -> dict:
    """モニタ方式 → 停止方式。止まっていれば、割り込まれた処理の PC に一時のブレークポイントを置いて GO し、戻った処理を
    停止方式で止める（同じ PC で止まったまま）。走っていれば走ったまま。どう止めたかを返す: stop = running（走ったまま）/
    breakpoint_at_pc（PC のブレークポイントで止まった）/ halt_request（置けなかった・戻らなかったので停止要求で止めた。
    PC は近くだが同じとは限らない）と、pc_before・pc_after。"""
    stopped = monitor.get_state() == Target.State.HALTED
    pc = monitor.read_core_register_raw("pc") if stopped else None
    raw.write32(DEMCR, raw.read32(DEMCR) & ~(MON_EN | MON_PEND | MON_STEP))
    raw.write32(DHCSR, DBGKEY | C_DEBUGEN)
    raw.flush()
    if not stopped:
        monitor.forget()
        return {"stop": "running"}
    existing = raw.find_breakpoint(pc) is not None             # 利用者のブレークポイントがあれば、それで止まる
    placed = not existing and bool(raw.set_breakpoint(pc, Target.BreakpointType.AUTO))
    if placed:
        _flush_breakpoints(raw)
    monitor.command(CMD_GO, 0)
    monitor.forget()
    how = "breakpoint_at_pc"
    if not (existing or placed) or not _wait(lambda: raw.get_state() == Target.State.HALTED,
                                              monitor.timeout_s, monitor.poll_s):
        raw.halt()                                             # 置けなかった・戻らない: すぐ近くで止める（結果に出す）
        how = "halt_request"
    if placed:
        raw.remove_breakpoint(pc)
        _flush_breakpoints(raw)
    return {"stop": how, "pc_before": pc, "pc_after": raw.read_core_register("pc")}


def _flush_breakpoints(target) -> None:
    """pyOCD はブレークポイントの変更を再開の直前に書く。モニタ方式ではコアが走っているので、すぐ書く。"""
    core = getattr(target, "selected_core", None) or target
    manager = getattr(core, "bp_manager", None)
    if manager is not None:
        manager.flush()


# --- 包み ----------------------------------------------------------------------------------------


@dataclass
class _Context:
    """止まっている間の箱の写し（読みの度に SWD を往復しない）。書けば写しも直す。"""
    words: list[int]

    def get(self, offset: int) -> int:
        return self.words[offset // 4]

    def put(self, offset: int, value: int) -> None:
        self.words[offset // 4] = value & 0xFFFFFFFF

    @property
    def exc_return(self) -> int:
        return self.get(Offsets.exc_return)

    @property
    def on_psp(self) -> bool:
        return bool(self.exc_return & EXC_RETURN_PSP)

    @property
    def extended(self) -> bool:
        return not self.exc_return & EXC_RETURN_BASIC


class MonitorTarget:
    """モニタ方式のターゲット: pyOCD の CortexM の口を、モニタの箱の読み書きで実現する。包んだもの以外の属性
    （メモリ・メモリマップ・ウォッチポイント等）はそのまま中のターゲットへ渡す。

    inner: pyOCD のターゲット（Flash のブレークポイントの包み FlashBreakpoints でもよい。Flash の番地のブレークポイントは
    比較器だけを頼む）。box: 箱の番地（find_box）。timeout_s: halt・step・再開の応答を待つ時間。"""

    def __init__(self, inner, box: int, timeout_s: float = 0.5, poll_s: float = 0.0005):
        self.inner = inner
        self.box = box
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._context: _Context | None = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    # --- 箱 ---------------------------------------------------------------------------------------

    def _read(self, offset: int) -> int:
        return self.inner.read32(self.box + offset)

    def _write(self, offset: int, value: int) -> None:
        self.inner.write32(self.box + offset, value & 0xFFFFFFFF)

    def forget(self) -> None:
        """箱の写しを捨てる（コアが動いた・箱を外から書いた）。"""
        self._context = None

    def _stopped_context(self) -> _Context:
        if self._context is None:
            words = list(self.inner.read_memory_block32(self.box, BOX_SIZE // 4))
            if _w(words, Offsets.state) != 1:
                raise MonitorError("ターゲットが止まっていない（モニタ方式: target_halt の後に）")
            self._context = _Context(words)
        return self._context

    def status(self) -> dict:
        """箱の様子（debug_mode.get 用）。"""
        words = self.inner.read_memory_block32(self.box, BOX_SIZE // 4)
        return {"state": "stopped" if _w(words, Offsets.state) == 1 else "running",
                "stops": _w(words, Offsets.stops), "prio_bits": _w(words, Offsets.prio_bits),
                "fpu": bool(_w(words, Offsets.flags) & FLAG_FPU),
                "stack_size": _w(words, Offsets.stack_size),
                "stack_overflow": bool(_w(words, Offsets.flags) & FLAG_STACK_OVERFLOW)}

    def command(self, cmd: int, arg: int) -> None:
        """命令を出し、モニタが受け取るまで待つ（受け取った時点でモニタは戻り始めている）。"""
        seq = (self._read(Offsets.seq_ack) + 1) & 0xFFFFFFFF
        self._write(Offsets.cmd, cmd)
        self._write(Offsets.arg, arg)
        self._write(Offsets.seq_cmd, seq)                     # cmd・arg の後に書く（SWD の書き込みは順に届く）
        self.inner.flush()
        if not _wait(lambda: self._read(Offsets.seq_ack) == seq, self.timeout_s, self.poll_s):
            raise MonitorError("モニタが命令に応えない（DebugMon の待ちの輪にいない。ファームが止まった・リセットされた）")

    # --- 状態 -------------------------------------------------------------------------------------

    def get_state(self):
        if self._context is not None:
            return Target.State.HALTED
        return Target.State.HALTED if self._read(Offsets.state) == 1 else Target.State.RUNNING

    def is_halted(self) -> bool:
        return self.get_state() == Target.State.HALTED

    def is_running(self) -> bool:
        return not self.is_halted()

    def get_halt_reason(self):
        reason = self._stopped_context().get(Offsets.reason)
        for bit, value in _REASONS:
            if reason & bit:
                return value
        # DFSR のビットが 1 つも無い入口は MON_PEND（ホストの停止の要求）だけ: DebugMonitor の入口で DFSR に印が残るのは
        # BKPT・DWTTRAP・HALTED（MON_STEP）・EXTERNAL・VCATCH で、MON_PEND は印を残さない（ARMv7-M ARM C1.6.1・C1.7.3）。
        # モニタ（host/firmware/monitor）は入るたびに DFSR を箱に写して消すので、前の停止の印は混ざらない
        return Target.HaltReason.USER

    # --- 実行 -------------------------------------------------------------------------------------

    def halt(self) -> None:
        """DEMCR.MON_PEND で DebugMon を起こし、止まるまで待つ。入らなければ要求を下ろして誤り。"""
        if self.get_state() == Target.State.HALTED:
            return
        inner = self.inner
        inner.write32(DEMCR, inner.read32(DEMCR) | MON_PEND)
        inner.flush()
        if _wait(lambda: self._read(Offsets.state) == 1, self.timeout_s, self.poll_s):
            return
        inner.write32(DEMCR, inner.read32(DEMCR) & ~MON_PEND)
        inner.flush()
        if self._read(Offsets.state) == 1:                     # 下ろす直前に入った
            return
        raise MonitorError(f"DebugMon に入らない（{self.timeout_s} 秒）: DebugMon（優先度 0x{read_priority(inner):02X}）より"
                           "優先度の高い処理か割り込み禁止（PRIMASK・BASEPRI）の区間に留まっている。停止方式なら止められる"
                           "（target_debug_mode_set mode=\"halt\"）")

    def resume(self) -> None:
        if self.get_state() != Target.State.HALTED:
            return
        self.command(CMD_GO, 0)
        self.forget()

    def step(self, disable_interrupts: bool = True, start: int = 0, end: int = 0, hook_cb=None) -> None:
        """1 命令（DEMCR.MON_STEP）。disable_interrupts なら、DebugMon より低い割り込みに入らない（BASEPRI）。"""
        stops = self._stopped_context().get(Offsets.stops)
        self.command(CMD_STEP, STEP_MASK_INTERRUPTS if disable_interrupts else 0)
        self.forget()
        if not _wait(lambda: self._read(Offsets.state) == 1 and self._read(Offsets.stops) != stops,
                     self.timeout_s, self.poll_s):
            raise MonitorError("1 命令の後に DebugMon に戻らない（命令が故障を起こした可能性。停止方式"
                               "（target_debug_mode_set mode=\"halt\"）にして target_fault で調べる）")

    def reset(self, reset_type=None) -> None:
        self.forget()
        self.inner.reset(reset_type)

    def reset_and_halt(self, reset_type=None) -> None:
        raise MonitorError("モニタ方式では止めてのリセットはできない（リセットの直後はモニタが動いていない）: "
                           "target_debug_mode_set mode=\"halt\" の後に")

    def set_vector_catch(self, mask: int) -> None:
        if mask:
            raise MonitorError("ベクタキャッチは停止方式だけの機能（DEMCR.VC_* は C_DEBUGEN = 1 のときだけ効く）: "
                               "target_debug_mode_set mode=\"halt\" の後に")
        self.inner.set_vector_catch(0)

    # --- ブレークポイント ------------------------------------------------------------------------------

    def set_breakpoint(self, address: int, type=Target.BreakpointType.AUTO) -> bool:
        """Flash（書けない領域）の番地は比較器だけ（Flash の BKPT は書かない）。RAM は pyOCD に任せる（BKPT）。
        コアは走っているので、すぐ書く。"""
        if type == Target.BreakpointType.AUTO and not self._writable(address):
            type = Target.BreakpointType.HW
        ok = self.inner.set_breakpoint(address, type)
        _flush_breakpoints(self._core_target())
        return ok

    def remove_breakpoint(self, address: int) -> None:
        self.inner.remove_breakpoint(address)
        _flush_breakpoints(self._core_target())

    def _core_target(self):
        """pyOCD のターゲット（Flash のブレークポイントの包みの中）。"""
        return self.inner.target if isinstance(self.inner, FlashBreakpoints) else self.inner

    def _writable(self, address: int) -> bool:
        memory_map = getattr(self.inner, "memory_map", None)
        region = memory_map.get_region_for_address(address) if memory_map is not None else None
        return region is None or region.is_writable

    # --- DFSR（止まった理由。stepping.Runner・GDB サーバが直接読む） --------------------------------------

    def read32(self, address: int, now: bool = True):
        if address == DFSR:
            value = self._stopped_context().get(Offsets.reason) if self.is_halted() else 0
            return value if now else (lambda: value)
        return self.inner.read32(address) if now else self.inner.read32(address, now)

    def write32(self, address: int, value: int) -> None:
        if address == DFSR:                                    # 1 を書いて消す（箱の写しの理由を）
            if self._context is not None:
                self._context.put(Offsets.reason, self._context.get(Offsets.reason) & ~value)
            return
        self.inner.write32(address, value)

    def read_memory(self, address: int, transfer_size: int = 32, now: bool = True):
        if address == DFSR and transfer_size == 32:
            value = self.read32(address)
            return value if now else (lambda: value)
        return self.inner.read_memory(address, transfer_size, now)

    def write_memory(self, address: int, value: int, transfer_size: int = 32) -> None:
        if address == DFSR and transfer_size == 32:
            self.write32(address, value)
            return
        self.inner.write_memory(address, value, transfer_size)

    # --- レジスタ ----------------------------------------------------------------------------------

    def _name(self, reg) -> str:
        if isinstance(reg, int):
            from pyocd.coresight.core_registers import CortexMCoreRegisterInfo
            reg = CortexMCoreRegisterInfo.get(reg).name
        name = reg.lower()
        return _ALIASES.get(name, name)

    def _value(self, context: _Context, name: str) -> int:
        if name in ("r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11"):
            return context.get(Offsets.r4_r11 + 4 * (int(name[1:]) - 4))
        if name == "sp":
            return context.get(Offsets.sp)
        if name in ("msp", "psp"):
            if (name == "psp") == context.on_psp:
                return context.get(Offsets.sp)
            return context.get(Offsets.msp if name == "msp" else Offsets.psp)
        if name in ("primask", "basepri", "faultmask"):
            mask = 1 if name != "basepri" else 0xFF
            return context.get(getattr(Offsets, name)) & mask
        if name == "control":
            return (context.get(Offsets.control) & 1) | int(context.on_psp) << 1 | int(context.extended) << 2
        if name == "cfbp":
            return (self._value(context, "control") << 24 | self._value(context, "faultmask") << 16
                    | self._value(context, "basepri") << 8 | self._value(context, "primask"))
        if name in ("ipsr", "apsr", "epsr", "iapsr", "eapsr", "iepsr"):
            xpsr = self._value(context, "xpsr")
            mask = {"ipsr": 0x1FF, "apsr": 0xF80F0000, "epsr": 0x0700FC00, "iapsr": 0xF80F01FF,
                    "eapsr": 0xFF0FFC00, "iepsr": 0x0700FDFF}[name]
            return xpsr & mask
        if name in _FP_NAMES:
            self._require_fp(context, name)
            if name in _FRAME_INDEX:
                return context.get(Offsets.frame + 4 * _FRAME_INDEX[name])
            return context.get(Offsets.s16_s31 + 4 * (int(name[1:]) - 16))
        if name in _FRAME_INDEX:
            return context.get(Offsets.frame + 4 * _FRAME_INDEX[name])
        raise MonitorError(f"モニタ方式では {name} は読めない（割り込まれた処理の文脈に無い）")

    @staticmethod
    def _require_fp(context: _Context, name: str) -> None:
        if not context.extended:
            raise MonitorError(f"割り込まれた処理は FPU の文脈を持たない（FP 付きでない例外の枠）: {name} は読めない")

    def read_core_registers_raw(self, regs) -> list[int]:
        context = self._stopped_context()
        return [self._value(context, self._name(r)) for r in regs]

    def read_core_register_raw(self, reg) -> int:
        return self.read_core_registers_raw([reg])[0]

    def read_core_register(self, reg):
        name = self._name(reg)
        value = self.read_core_register_raw(name)
        if name in _FP_NAMES and name != "fpscr":
            return struct.unpack("<f", struct.pack("<I", value))[0]
        return value

    def write_core_register(self, reg, value) -> None:
        name = self._name(reg)
        if isinstance(value, float):
            value = struct.unpack("<I", struct.pack("<f", value))[0]
        self.write_core_registers_raw([name], [value])

    def write_core_register_raw(self, reg, value: int) -> None:
        self.write_core_registers_raw([reg], [value])

    def write_core_registers_raw(self, regs, values) -> None:
        """箱に書く（出口でモニタが割り込まれた処理に戻す）。sp を先に（枠の積み直しの場所）。"""
        context = self._stopped_context()
        pairs = [(self._name(r), int(v) & 0xFFFFFFFF) for r, v in zip(regs, values)]
        pairs.sort(key=lambda p: p[0] not in ("sp", "msp", "psp"))
        for name, value in pairs:
            offset = self._register_offset(context, name, value)
            if offset is None:
                continue
            if name == "pc":
                value &= ~1
            elif name == "xpsr":
                value &= ~XPSR_ALIGNED
            elif name == "primask":
                value &= 1
            elif name == "basepri":
                value &= 0xFF
            context.put(offset, value)
            self._write(offset, value)
        self.inner.flush()

    def _register_offset(self, context: _Context, name: str, value: int) -> int | None:
        """書く箱の位置。書けないレジスタは、いまと同じ値なら None（何もしない）、違えば誤り。"""
        if name in ("r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11"):
            return Offsets.r4_r11 + 4 * (int(name[1:]) - 4)
        if name == "sp" or (name in ("msp", "psp") and (name == "psp") == context.on_psp):
            if value & 3:
                raise MonitorError(f"sp は 4 バイト境界（0x{value:08X}）")
            return Offsets.sp
        if name in ("primask", "basepri"):
            return getattr(Offsets, name)
        if name in _FP_NAMES:
            self._require_fp(context, name)
            if name in _FRAME_INDEX:
                return Offsets.frame + 4 * _FRAME_INDEX[name]
            return Offsets.s16_s31 + 4 * (int(name[1:]) - 16)
        if name in _FRAME_INDEX:
            return Offsets.frame + 4 * _FRAME_INDEX[name]
        try:
            current = self._value(context, name)
        except MonitorError:
            current = None
        if current is not None and current == value:
            return None
        raise MonitorError(f"モニタ方式では {name} は書けない（例外からの戻りで決まる・モニタの外の状態）")
