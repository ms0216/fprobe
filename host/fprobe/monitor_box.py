"""モニタ方式の箱と切り替え: 箱（struct fprobe_monitor）の位置・レジスタの番地などの定数、箱の見つけ方（find_box）、
停止方式とモニタ方式の切り替え（enter_monitor / leave_monitor）、優先度の読み書き。pyOCD のターゲット（停止方式の口）に
直接触れ、箱の読み書きで実現する包み MonitorTarget は monitor.py。"""

import time
from typing import TYPE_CHECKING

from pyocd.core.target import Target

if TYPE_CHECKING:
    from .monitor import MonitorTarget

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
_SYSTEM_HANDLERS = {4: "MemManage", 5: "BusFault", 6: "UsageFault", 11: "SVCall", 14: "PendSV", 15: "SysTick"}


class Offsets:
    """箱（struct fprobe_monitor、版 1）のメンバーの位置。C の _Static_assert と試験（DWARF）で照合する。"""
    magic, version, size, flags = 0x00, 0x04, 0x08, 0x0C
    state, reason, stops = 0x10, 0x14, 0x18
    seq_cmd, cmd, arg, seq_ack, result = 0x1C, 0x20, 0x24, 0x28, 0x2C
    exc_return, msp, psp = 0x30, 0x34, 0x38
    primask, basepri, faultmask, control = 0x3C, 0x40, 0x44, 0x48
    sp, frame, r4_r11, s16_s31 = 0x4C, 0x50, 0xB8, 0xD8
    exit_frame, step_basepri, step_forced, prio_bits, stack_size = 0x118, 0x11C, 0x120, 0x124, 0x128


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


