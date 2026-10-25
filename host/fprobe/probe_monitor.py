"""Probe の道具: デバッグの方式（停止方式 halt / モニタ方式 monitor）。J-Link の Monitor Mode Debugging の相当（U37）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。モニタ方式のとき、Probe.target は fprobe.monitor.MonitorTarget（pyOCD の
CortexM の口を、ターゲットのモニタの箱の読み書きで実現する包み）を返す。止める・再開・ステップ・レジスタ・ブレーク
ポイント・行単位のステップ・呼び出し履歴・関数の呼び出し・セミホスティング・GDB サーバは、すべて Probe.target を使う
ので、方式に関係なく同じ道具がそのまま使える。

- debug_mode_get: いまの方式・モニタの箱・DebugMon の優先度・止まっている間も動く割り込み。
- debug_mode_set: 方式を切り替える（止まっていれば同じ PC で止まったまま）。priority で DebugMon の優先度。
- _halt_inject: クロストリガの「ターゲット停止」の書き込み（停止方式は DHCSR ← C_HALT、モニタ方式は DEMCR ← MON_PEND）。
- _require_halt_mode: 停止方式だけの操作（Flash の書き込み等）の前に呼ぶ。

仕様: docs/superpowers/specs/2026-10-18-u37-monitor-mode-design.md
"""

from . import generated as g
from .api import NotConnected
from .flash_bp import FlashBreakpoints
from .monitor import (DEMCR, MON_EN, MON_PEND, TRCENA, MonitorError, MonitorTarget, check_architecture, enter_monitor,
                      find_box, keeps_running, leave_monitor, read_priority, write_priority)
from .xtrig import DHCSR, HALT, CrossTrigger

__all__ = ["MODES", "MonitorModeTools"]

MODES = ("halt", "monitor")
MONITOR_HALT = TRCENA | MON_EN | MON_PEND                 # クロストリガの停止（モニタ方式）: DEMCR に書く値


class MonitorModeTools:
    def _init_monitor(self) -> None:
        self._monitor: MonitorTarget | None = None

    # --- Probe.target から ---------------------------------------------------------------------------

    def _monitor_wrap(self, target):
        """モニタ方式なら包んだもの（同じもの）、停止方式ならそのまま。"""
        monitor = self._monitor
        if monitor is None:
            return target
        monitor.inner = target                                 # Flash のブレークポイントの包みを作り直しても追う
        return monitor

    def _monitor_detach(self) -> None:
        """切断の前に: 止まっていれば再開して離す（モニタ方式は切る）。"""
        monitor, self._monitor = self._monitor, None
        if monitor is None:
            return
        try:
            monitor.resume()
        except Exception:                                      # ファームが止まっている等: 切断は続ける
            pass

    def _require_halt_mode(self, what: str) -> None:
        if self._monitor is not None:
            raise MonitorError(f"モニタ方式では{what}はできない（コアを停止方式で止める必要がある）: "
                               "target_debug_mode_set mode=\"halt\" の後に")

    def _halt_inject(self) -> tuple[int, int]:
        """クロストリガの「ターゲット停止」の書き込み（番地, 値）。"""
        return (DEMCR, MONITOR_HALT) if self._monitor is not None else (DHCSR, HALT)

    def _switch_halt_inject(self, old: tuple[int, int], new: tuple[int, int]) -> bool:
        """割り込み書き込みが old（既定の停止）なら new にする。利用者が替えたものには触らない。"""
        device = self.session.device
        if not getattr(device.capabilities(), "inject", False):
            return False
        address, value = device.read(g.XTRIG_INJECT_ADDR, 2)
        if (address, value) != old:
            return False
        CrossTrigger(device).set_inject(*new, 0)
        return True

    # --- 道具 ----------------------------------------------------------------------------------------

    def debug_mode_get(self) -> dict:
        """いまのデバッグの方式。mode: halt（停止方式。ブレークポイント等でコア全体を止める）/ monitor（モニタ方式。
        DebugMon の中で止まり、優先度の高い割り込みは動き続ける）。available: ファームにモニタがあるか（無ければ
        reason）。モニタがあれば box（箱の番地）・priority（DebugMon の優先度）・prio_bits・keeps_running（止まって
        いる間も動く例外・割り込み）、モニタ方式なら state・stops・stack_overflow も。"""
        with self._lock:
            if self._debug is None:
                raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")
            raw = self.target if self._monitor is None else self._monitor.inner
            out: dict = {"mode": "monitor" if self._monitor is not None else "halt"}
            try:
                box = self._monitor.box if self._monitor is not None else find_box(raw)
            except MonitorError as e:
                return {**out, "available": False, "reason": str(e)}
            priority = read_priority(raw)
            out.update(available=True, box=box, priority=priority, keeps_running=keeps_running(raw, priority))
            status = (self._monitor or MonitorTarget(raw, box)).status()
            out.update(prio_bits=status["prio_bits"], fpu=status["fpu"], stack_size=status["stack_size"],
                       stack_overflow=status["stack_overflow"])
            if self._monitor is not None:
                out.update(state=status["state"], stops=status["stops"])
            return out

    def debug_mode_set(self, mode: str, priority: int | None = None) -> dict:
        """デバッグの方式を切り替える。mode: halt / monitor。止まっていれば同じ PC で止まったまま切り替わる。
        monitor にはファームのモニタ（host/firmware/monitor）が要る。priority: DebugMon の優先度（0〜255。数が小さい
        ほど高い。これより高い割り込みは止まっている間も動く）。結果は debug_mode_get と同じ。"""
        if mode not in MODES:
            raise ValueError(f"mode は {' / '.join(MODES)}（{mode!r}）")
        with self._lock:
            if self._debug is None:                            # スナップショットの読み手には方式が無い
                raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")
            self.target
            raw = self._debug.target
            left = None
            if mode == "monitor" and self._monitor is None:
                self._enter_monitor_mode(raw, priority)
            elif mode == "halt" and self._monitor is not None:
                left = self._leave_monitor_mode(raw)
            if priority is not None:
                write_priority(raw, priority)
            out = self.debug_mode_get()
            if left is not None:
                out["left"] = left                             # 停止方式へ戻るときにどう止めたか（leave_monitor）
            return out

    def _enter_monitor_mode(self, raw, priority: int | None) -> None:
        check_architecture(raw)
        target = self.target
        if isinstance(target, FlashBreakpoints):
            if target.status()["flash"]:
                raise MonitorError("Flash の BKPT のブレークポイントがある（モニタ方式では Flash を書き換えられない）: "
                                   "debug_breakpoint_clear で外すか、比較器に収まる数にしてから")
            if target.dormant:
                target.restore_all()                           # 外したが Flash に残る BKPT を戻す
        box = find_box(raw)
        if priority is not None:
            write_priority(raw, priority)
        self._watch_stops(False)
        enter_monitor(raw, box)
        self._monitor = MonitorTarget(self._flash_bp_wrap(raw), box)
        self._switch_halt_inject((DHCSR, HALT), (DEMCR, MONITOR_HALT))
        self._repoint_debugger()

    def _leave_monitor_mode(self, raw) -> dict:
        monitor = self._monitor
        self._watch_stops(False)
        left = leave_monitor(monitor, raw)
        self._monitor = None
        self._switch_halt_inject((DEMCR, MONITOR_HALT), (DHCSR, HALT))
        self._repoint_debugger()
        return left

    def _repoint_debugger(self) -> None:
        """Debugger（置いたブレークポイントの名前を持つ）を作り直さずに、新しい Probe.target に向ける。"""
        if self._dbg is not None:
            self._dbg.target = self.target

