"""`ArmACores` の部品: 全コアを同時に（または FPGA の力で）止める仕組みの設定と、FPGA の停止の見張りの後始末。

CTI（`use_cross_halt`）・FPGA の停止の見張り（`use_fpga_halt_watch`）・FPGA の要求時刻（`use_fpga_request_times`）を設定し、
見張りを外して発火の記録を読む（`_stop_watch`・`_fpga_record`）。停止の報告そのものは `arma_halt`、再開と待ちは `arma_run`。
"""
from __future__ import annotations

import time

from fprobe.arm_debug import DSCR_HALTED, DSCR_RESTARTED
from fprobe.armv7a_debug import DRCR, DRCR_CSE, DRCR_HRQ, DRCR_RRQ, DSCR
from fprobe.halt_watch import WORDS as HALT_WATCH_WORDS


class ArmAWatchMixin:
    """停止の仕組みの設定（`ArmACores` の状態 `_cores`・`_watch` 等を使う）。"""

    def use_fpga_request_times(self, times) -> None:
        """こちらから全コアを止める（CTI なしの順の停止）の時間差の上限を、FPGA が停止要求の書き込みに付けた時刻で出す
        （times: `fprobe.linux_halt.request_times.EventRequestTimes`。docs/linux-halt.md）。使えない停止（イベント記録が無い・
        欠けた・溢れた、止める前から止まっていたコアがある、コアが要求以外の理由で止まった等）は PC の時計の上限のまま、理由を
        根拠の文に添える。呼ばなければ PC の時計の上限だけ（既定で切り替えない）。"""
        if self._cross is not None:
            raise ValueError("the FPGA request times are for the stop without a CTI (the CTI halts in hardware)")
        self._request_times = times

    def use_fpga_halt_watch(self, watch, *, event_skew_upper_bound_ns: int | None, poll_s: float) -> None:
        """FPGA の停止の見張りを使う（CTI の無い SoC）。event_skew_upper_bound_ns: 1 つのコアが止まってから FPGA が最後のコアを
        止めるまでの上限（呼び出し側が目標の側で測った値。測っていなければ None = 分からない。既定なし）。poll_s: 待つ間に FPGA の
        状態を読む間隔（USB の制御の経路。JTAG は使わない）。番地は各コアの DBGDSCR / DBGDRCR（ARMv7 の決まり）を渡す。"""
        if self._cross is not None:
            raise ValueError("the CTI and the FPGA halt watch are not used together (the CTI halts in hardware)")
        ports = [c.port for c in self._cores]
        if len({(id(p.dap), p.ap) for p in ports}) != 1:
            raise ValueError("the FPGA halt watch needs every core behind one DAP and one APB-AP")
        halts = [(p.base + DRCR, DRCR_HRQ) for p in ports]
        if watch.can_restart and 2 * len(ports) <= HALT_WATCH_WORDS:
            # 再開も FPGA が書く: 再開の要求の後に読んだ DBGDSCR で HALTED = RESTARTED = 1 は「再開の後に止まった」
            stopped = DSCR_HALTED | DSCR_RESTARTED
            watch.configure(ap=ports[0].ap, watch=[p.base + DSCR for p in ports], halted_mask=stopped, halted_value=stopped,
                            halt_writes=halts, restart_writes=[(p.base + DRCR, DRCR_RRQ | DRCR_CSE) for p in ports])
            self._watch_restarts = True
        else:
            watch.configure(ap=ports[0].ap, watch=[p.base + DSCR for p in ports], halted_mask=DSCR_HALTED,
                            halted_value=DSCR_HALTED, halt_writes=halts)
            self._watch_restarts = False
        self._watch, self._watch_bound, self._watch_poll_s = watch, event_skew_upper_bound_ns, poll_s

    def _disarm_watch(self) -> None:
        if self._watch_armed:
            self._watch.disarm()
            self._watch_armed = False

    def _stop_watch(self) -> dict | None:
        """見張りを外し（値の一致を先に止める: その後は発火しない）、外す前に FPGA が発火していたなら、その停止の記録
        （`last_wait` の形）を返す。外す前の状態を見ずに外すと、正しく発火して FPGA が全コアを止めた停止を、ホストの停止として
        報告してしまう（レビュー 2026-11-03）。"""
        if not self._watch_armed:
            return None
        self._disarm_watch()
        return self._fpga_record(self._watch.poll())

    def _fpga_record(self, poll) -> dict | None:
        """poll が発火を示していれば、割り込み書き込みが終わるのを待って（各コアの timeout_s のうち最長まで）停止の記録を返す。
        終わらなければ、その事実を結果に書く（ホストが残りを止める）。"""
        if not poll.matched:
            return None
        deadline = time.monotonic() + max(c.timeout_s for c in self._cores)
        st = self._watch.status(poll)
        while not st.fired and time.monotonic() < deadline:
            st = self._watch.status()
        core = st.core if st.fired else poll.index // 16
        record = {"hit": (core,), "requested": tuple(n for n in range(self.n_cores) if n != core and n not in self.parked),
                  "by": "fpga", "fpga": {"result": st.result if st.fired else "still writing", "written": st.written,
                                         "t_prev": st.t_prev, "t_fire": st.t_fire, "t_first_write": st.t_first_write,
                                         "t_last_write": st.t_last_write, "fire_first": st.fire_first}}
        if self._watch_restarts and self._fpga_restart is not None:
            r = self._fpga_restart
            record["fpga"].update(restarted_by_fpga=r.written, t_restart_first=r.t_first, t_restart_last=r.t_last)
        missed = sorted(n for n in self._watch_halted if not poll.seen_running[n])
        if missed:                                     # 発火とは別に、FPGA が走っているのを読む前に止まっていたコア（事実）
            record["fpga_missed"] = tuple(missed)
        return record

    def use_cross_halt(self, cross, *, request_skew_upper_bound_ns: int, event_skew_upper_bound_ns: int | None,
                       event_trigin: int | None, hardware_halt_wait_s: float) -> None:
        """CTI（CrossHalt）で全コアを同時に止める。**時間差の上限は呼び出し側が測った値を渡す**（既定なし。この層は推測しない）:
        request_skew_upper_bound_ns = こちらから止めるときの上限、event_skew_upper_bound_ns = コアが自分で止まって他をハードウェアが
        止めるときの上限（測っていなければ None = 分からない）。event_trigin = コアのデバッグ確認応答（DBGACK）が出るトリガー入力
        （None なら、自分で止まったコアの後の他のコアはソフトウェアの停止要求で止める = 順になる）。hardware_halt_wait_s: ハードウェアが
        全コアを止めるのを待つ時間。その間に止まらなかったコアは、ソフトウェアの要求で止めて、順の停止として報告する。"""
        if self._watch is not None:
            raise ValueError("the CTI and the FPGA halt watch are not used together (the CTI halts in hardware)")
        if self._request_times is not None:
            raise ValueError("the FPGA request times are for the stop without a CTI (the CTI halts in hardware)")
        self._cross = cross
        self._cross_bounds = (request_skew_upper_bound_ns, event_skew_upper_bound_ns)
        self._event_route = event_trigin is not None
        self._hw_wait_s = hardware_halt_wait_s
        cross.arm(event_sync_trigin=event_trigin)
