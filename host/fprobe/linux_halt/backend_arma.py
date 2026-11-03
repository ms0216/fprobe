"""fprobe 自身の経路の `CoreAccess`: 1 本の DAP（JTAG-DP → APB-AP）の向こうの Cortex-A × 4（`ArmDebugCore` をコアごとに）。

停止の時間差の上限は、`fprobe.linux_halt.skew` の式で毎回求める: 各コアが「ここより前には止まっていない」と分かっている時刻（再開した時刻、
走っているのを読んだ時刻）のうち最小のものから、全コアが止まったのを確認した時刻まで。検出が遅れた分も含む（常に上限）。止まった時刻の
分からないコア（デバッガの知らないうちに止まっていた、再開されずに止まったままだった）があれば「分からない」（None）。
CTI（`use_cross_halt`）を渡すと、ハードウェアが全コアを止めたことを**確認できたときだけ**同時停止と言う（呼び出し側が測った上限を添えて）。止まらなかった
コアはソフトウェアの要求で止め、順の停止として報告する。
CTI の無い SoC では、FPGA の停止の見張り（`use_fpga_halt_watch`、fprobe.halt_watch）を渡せる: 全コアが走っている間 FPGA が DSCR を読み続け、
1 つが止まったら FPGA が全コアに停止要求を書く（USB の往復を挟まない。docs/superpowers/specs/2026-10-29-halt-watch-design.md）。
それでも 1 コアずつ書くので順の停止で、上限は呼び出し側が目標の側で測った値を渡す。見張りは再開の要求の前（全コアが止まっている間）に
始め、FPGA が走っている値を読む前に止まったコアは、FPGA の印（sampler_match の unmatched）とホストの DSCR の読みで「発火しない」と
確定してホストの方式に移る（§3.6）。FPGA が全コアの走っている値を読んだ後は、待つ間ホストは JTAG を使わない（FPGA の見張りを邪魔しない）。
回路が再開を書ける（ケイパビリティ watch_start）なら、再開の要求そのものを FPGA が書き、その後の読みで DBGDSCR の HALTED と
RESTARTED が両方 1（再開の後に止まった。ARMv7 の定義）なら発火する。再開と見張りの間の窓が無いので、上の見逃しの確定は使わず、
待つ間ずっと JTAG を使わない（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md）。
`use_fpga_request_times` を渡すと、こちらから全コアを止めたときの上限を、FPGA が停止要求の書き込みに付けた時刻（イベント記録）で出す
（仮定と、PC の時計の上限を根拠の文に並べる。使えない停止は PC の時計の上限と、その理由。docs/linux-halt.md）。
物理アドレスの読み書きは、カーネルの線形マップの仮想アドレスを、コア 0 を通して読み書く（この SoC の DAP には止めずに/物理で読む AP がない）。
コアが USR モードで止まっていても、メモリとキャッシュの操作は特権モードで行う（`ArmDebugCore.privileged`）。
"""
from __future__ import annotations

import struct
import time
from typing import Iterable, Sequence

from fprobe.arm_debug import (DSCR_HALTED, DSCR_MOE_SHIFT, DSCR_RESTARTED, REGISTER_NAMES, ArmDebugCore, ArmDebugError,
                              enter_all, prepare_restart_all, read_state_all)
from fprobe.armv7a_debug import (BCR, DRCR, DRCR_CSE, DRCR_HRQ, DRCR_RRQ, DSCR, WCR, dscr_all, halt_request_all, read_all,
                                 restart_request_all)
from fprobe.halt_watch import WORDS as HALT_WATCH_WORDS

from .core_access import MOE_HALT, CoreState, OutsideLinearMap, StopReport
from .request_times import NotTimed
from .skew import NotHaltedBefore, stop_report

MODE_SVC = 0x13
_WATCH_KIND = {"w": "write", "r": "read", "rw": "access"}
_SEQ_BASIS = "host-timed: from the last instant each core was known not to have halted until all were seen halted"
_SAME_HALT_TIME = ("every core takes the same time from its DBGDRCR write to its halt (the target's APB transfer and the "
                   "core's entry to debug state; not measured)")
_FPGA_BASIS = (
    "fpga-timed: the FPGA's timestamps of the accepted halt-request writes (DBGDRCR.HRQ; DAP events of kind 1, ack OK), "
    "first to last core: %d ticks at %d Hz, rounded up to ns; from the moment each write reaches the target (JTAG "
    "Update-DR) to its timestamp the FPGA takes the same number of cycles for every write (docs/linux-halt.md; on a "
    "device declaring DAP_LINE_TIME the timestamp is that Update-DR point itself), which adds "
    "0; every core reported a halt request as its entry reason (DSCR.MOE); assumes every core takes the same time from "
    "its DBGDRCR write to its halt (the target's APB transfer and the core's entry to debug state; not measured); "
    "host clock: %s (without that assumption, %s)")
_REACH = ("each core halted by a halt request halts at the moment the request's write reaches it (the target's APB "
          "transfer and the core's entry to debug state after the write are not measured)")
_FPGA_EVENT_BASIS = (
    "fpga-timed (stop by a core): cores %s were found halted by the host and the others were sent halt requests; from "
    "the earliest moment any core could have halted (core %d: %s, FPGA timestamp of that transfer) to the last accepted "
    "halt-request write (DBGDRCR.HRQ): %d ticks at %d Hz, rounded up to ns; assumes %s; without that assumption (until "
    "each core was read halted, FPGA timestamps): %s; host clock: %s")
_FPGA_WATCH_BASIS = (
    "fpga-timed: from the FPGA's last sample that read core %d running (its read request on the wire, DAP_LINE_TIME) "
    "to the FPGA's last halt-request write reaching the target (DBGDRCR.HRQ, Update-DR): %d ticks at %d Hz, rounded up "
    "to ns; every other core reported a halt request as its entry reason (DSCR.MOE); assumes %s; caller's bound: %s; "
    "host clock: %s")


class ArmACores:
    def __init__(self, cores: Sequence[ArmDebugCore], *, page_offset: int, phys_offset: int, linear_end: int, dap=None,
                 link=None, clock=time.perf_counter_ns):
        """cores: 開いて停止デバッグを有効にした ArmDebugCore（コア番号の順）。page_offset・phys_offset: カーネルの線形マップ
        （va = pa - phys_offset + page_offset）。linear_end: 線形マップの上端の仮想アドレス（これ以上の物理アドレスは仮想に直せない）。
        clock: ns を返す時計（試験で差し替える）。"""
        self._cores = list(cores)
        self.n_cores = len(self._cores)
        self.page_offset, self.phys_offset, self.linear_end = page_offset, phys_offset, linear_end
        self.hw_slots = min(c.breakpoint_count - 1 for c in self._cores)       # 最後の 1 組はステップ用
        self.watch_slots = min(c.watchpoint_count for c in self._cores)
        self.parked: set[int] = set()
        self.last_wait: dict | None = None
        self._dap, self._link, self._clock = dap, link, clock
        self.debug_ap: int | None = None
        self.discovery: str | None = None
        self._ledger = NotHaltedBefore()
        self._hw_bps: dict[tuple[int, int], tuple[int, int]] = {}             # (コア, 組) → (番地, 幅)
        self._cross = None                       # CrossHalt（CTI）
        self._cross_bounds: tuple[int, int | None] = (0, None)
        self._event_route = False
        self._hw_wait_s = 0.0
        self._cti_dirty = False                  # CTI で止めた/止まったあと、まだ release していない
        self._needs_rearm = False                # release のあと、再開の最後に停止要求のゲートを開け直す
        self._watch = None                       # fprobe.halt_watch.HaltWatch（FPGA の停止の見張り）
        self._watch_armed = False
        self._watch_halted: set[int] = set()     # 見張りの間に、ホストが（再開の後に）止まっているのを読んだコア
        self._watch_covered = False              # 全コアについて、FPGA が走っているのを読んだのを見た（以後の停止は必ず発火する）
        self._watch_bound: int | None = None
        self._watch_poll_s = 0.0
        self._watch_restarts = False             # FPGA が再開を書く（門の後だけ比べる）回路で見張る
        self._fpga_restart = None                # 最後に FPGA が書いた再開（HaltWatchRestart）
        self._request_times = None               # request_times.EventRequestTimes（FPGA の時刻での上限）
        self._resume_mark = None                 # 最後の再開の前のイベント記録の位置（コアが自分で止まった停止の上限に使う）

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

    @classmethod
    def open(cls, link, part, *, frequency_hz: int, page_offset: int, phys_offset: int, linear_end: int,
             jtag_chain: list[int] | None = None, clock=time.perf_counter_ns) -> "ArmACores":
        """link（開いた CMSIS-DAP の層）で JTAG を起こし、チェーンから part の装置を見つけ、APB-AP の向こうの全コアの停止デバッグを
        有効にする。止めはしない。frequency_hz は必須（配線の性質。既定なし）。"""
        from fprobe.arm_a_target import _find_tap
        from fprobe.arm_debug import enable_all
        from fprobe.armv7a_debug import V7DebugPort, find_apb_ap, find_debug_bases, find_mem_ap, unlock_all
        from fprobe.jtag_chain import dap_link_wire
        from fprobe.raw_dap import RawDap
        if part.kind != "v7":
            raise ValueError("ArmACores is for ARMv7-A parts (got %r)" % part.kind)
        dap = RawDap(link)
        try:
            dap.start("jtag", frequency_hz, jtag_chain or [4], jtag_idle_cycles=part.jtag_idle_cycles)
            dap.switch_to_jtag()
            chain, index = _find_tap(dap_link_wire(link), part, jtag_chain)
            dap.start("jtag", frequency_hz, chain, dap_index=index, jtag_idle_cycles=part.jtag_idle_cycles)
            dap.read_dpidr()
            if not dap.power_up()["debug_power"]:
                raise ArmDebugError(f"{part.name}: DP のデバッグ電源が上がらない")
            dap.clear_errors()                                  # 前のデバッガが残した STICKYERR 等（最初の AP の読みが FAULT になる）
            ap = find_apb_ap(dap)
            if ap is None and part.ahb_debug_bases:             # PJTAG の Zynq: AHB-AP のシステムのアドレスで届く
                ap = find_mem_ap(dap)
                if ap is None:
                    raise ArmDebugError(f"{part.name}: APB-AP も MEM-AP も無い（コアのデバッグレジスタに届かない）")
                bases, discovery = list(part.ahb_debug_bases), "ahb_system_map"
            elif ap is None:
                raise ArmDebugError(f"{part.name}: APB-AP が無い（コアのデバッグレジスタに届かない）")
            else:
                bases, discovery = find_debug_bases(dap, ap) or list(part.debug_bases), "apb_rom_table"
            if not bases:
                raise ArmDebugError(f"{part.name}: コアのデバッグの番地が分からない（ROM テーブルも部品の表も無い）")
            ports = [V7DebugPort(dap, ap, base) for base in bases]
            unlocked = unlock_all(ports)                         # 全コアのロックを解き DBGDIDR を読む（1 回の往復）
            cores = [ArmDebugCore(p, didr=u["didr"]) for p, u in zip(ports, unlocked)]
            enable_all(cores)                                    # 全コアの HDBGen（3 回の往復）
        except BaseException:
            try:
                dap.stop()
            finally:
                link.close()
            raise
        self = cls(cores, page_offset=page_offset, phys_offset=phys_offset, linear_end=linear_end, dap=dap, link=link,
                   clock=clock)
        self._connect = (part.name, frequency_hz, chain, index, part.jtag_idle_cycles)   # reconnect が同じ設定でつなぎ直す
        self.debug_ap = ap                                       # コアのデバッグ（と CTI）に使う AP の番号
        self.discovery = discovery                               # "apb_rom_table"（APB-AP の番地）か "ahb_system_map"（AHB-AP のシステムのアドレス）。CTI の番地の足し方が違う
        return self

    def reconnect(self) -> None:
        """同じ接続の上で DP をつなぎ直す（`open` と同じ設定で start → デバッグの電源を求め直す → 誤りの印を消す）。線の化けで
        CTRL/STAT が 0 に書かれる（電源の要求が消える）等で DAP が使えなくなった後、後始末（BKPT・HW の bp を外し停止デバッグを切る）を
        通すためにだけ使う。コアの状態（止まっているか・HDBGen・bp）は変えない。失敗は例外のまま（呼び出し側が報告する）。"""
        name, frequency_hz, chain, index, idle = self._connect
        self._dap.start("jtag", frequency_hz, chain, dap_index=index, jtag_idle_cycles=idle)
        self._dap.read_dpidr()
        if not self._dap.power_up()["debug_power"]:
            raise ArmDebugError(f"{name}: DP のデバッグ電源が上がらない（つなぎ直し）")
        self._dap.clear_errors()
        for c in self._cores:
            c.port._ready = False                                # 前の命令が終わったかは分からない（次の命令の前に確かめる）

    def disable_halting_debug(self) -> None:
        """全コアの停止デバッグ（DSCR.HDBGen）を切る。デバッグの持ち主（attach）が終わるときに呼ぶ: 立てたままだと、メモリに残った BKPT 命令で
        コアが止まり続け、ターゲットが固まる（切れば BKPT はアプリの SIGTRAP になる）。`close` は呼ばない（読むだけの別の接続が、動いている
        attach の HDBGen を切らないため）。止まっているコアは止まったまま（再開は呼び出し側）。"""
        for c in self._cores:
            c.disable()

    def enable_halting_debug(self) -> None:
        """全コアの停止デバッグ（DSCR.HDBGen）を立て直す（`open` と同じ状態に。rescue・attach の終わりで切った後、同じ接続で
        また止めて読むとき）。止めはしない。"""
        for c in self._cores:
            c.enable()

    def release_cross_halt(self) -> None:
        """CTI を使っているなら、残っている CTI の停止要求を解く（rescue の前。`cross.release()`）。"""
        if self._cross is not None:
            self._cross.release()

    def rescue(self) -> tuple[int, ...]:
        """死んだデバッガの後始末（強制終了された `attach` が、コアを止めたまま残したとき）。**ターゲットのメモリは触らない**: 各コアの
        ブレークポイント・ウォッチポイントを消し、停止デバッグ（HDBGen）を切り、止まっているコアを再開する（CTI を使っていたなら、
        呼ぶ前に CTI の停止要求を解いておく。`cross.release()`）。メモリに残った BKPT 命令は、HDBGen を切ったので、アプリの SIGTRAP になるだけで
        コアは止まらない（戻すのは `recover`）。再開したコアの番号を返す。"""
        resumed = []
        for n, c in enumerate(self._cores):
            for slot in range(c.breakpoint_count):
                c.clear_breakpoint(slot)
            for slot in range(c.watchpoint_count):
                c.clear_watchpoint(slot)
            was_halted = bool(c.port.dscr() & DSCR_HALTED)
            c.disable()
            if was_halted:
                c.port.restart_request()
                resumed.append(n)
        self._hw_bps.clear()
        self.parked = set()
        return tuple(resumed)

    def close(self) -> None:
        """FPGA の停止の見張りを外し、クロストリガの経路も外してから DAP・リンクを閉じる。外さずに閉じると、プロセスが終わった後も
        FPGA が見張り続け（サンプラが DAP の線を使い続け、次のプロセスの DAP が応答しない）、後でコアが 1 つ止まると残りを勝手に止める。"""
        try:
            if self._watch is not None:
                self._watch_armed = False
                self._watch.release()
        finally:
            try:
                if self._dap is not None:
                    self._dap.stop()
            finally:
                if self._link is not None:
                    self._link.close()

    # ------------------------------------------------------------------ state
    def programmed(self) -> dict:
        """読み取りだけ（コアは止めない）: 各コアが走っているか、有効なブレークポイント・ウォッチポイントの組。試験の「きれいに
        戻した」確認に使う。ステップ用の最後のブレークポイントの組も含めて見る（残っていてはいけない）。"""
        out: dict = {"running": [], "breakpoints": {}, "watchpoints": {}}
        reads = [(c.port, offset) for c in self._cores
                 for offset in (DSCR, *(BCR + 4 * i for i in range(c.breakpoint_count)),
                                *(WCR + 4 * i for i in range(c.watchpoint_count)))]
        values = iter(read_all(reads))                                     # 全コアの全部の組を 1 回の往復で
        for n, c in enumerate(self._cores):
            out["running"].append(not next(values) & DSCR_HALTED)
            bps = [i for i in range(c.breakpoint_count) if next(values) & 1]
            wps = [i for i in range(c.watchpoint_count) if next(values) & 1]
            if bps:
                out["breakpoints"][n] = bps
            if wps:
                out["watchpoints"][n] = wps
        return out

    def _scan(self) -> list[bool]:
        """各コアの DSCR を読んで止まっているかを返す。走っているのを読んだら、「読み始めの時刻より前には止まっていない」を記録する。"""
        return self._scan_of(range(self.n_cores))

    def _scan_of(self, ns) -> list[bool]:
        """ns のコアが止まっているか（`_read_dscr`）。"""
        return [bool(d & DSCR_HALTED) for d in self._read_dscr(ns)]

    def _read_dscr(self, ns) -> list[int]:
        """ns のコアの DSCR を 1 回の往復で読む（読み始めの時刻を、走っていたコアの「ここより前には止まっていない」にする）。"""
        ns = list(ns)
        t = self._clock()
        out = dscr_all([self._cores[n].port for n in ns])
        for n, d in zip(ns, out):
            if not d & DSCR_HALTED:
                self._ledger.seen_running(n, t)
        return out

    def halted(self) -> tuple[bool, ...]:
        return tuple(self._scan())

    def snapshot(self, cores: Iterable[int] | None = None) -> dict[int, CoreState]:
        """各コアの状態。DSCR を 1 回の往復で読み、止まっているコアに入り（入っていなければ。全コアで 2 回の往復）、CONTEXTIDR と
        SVC の SP を全コアで 1 回の往復で読む。"""
        ns = list(range(self.n_cores) if cores is None else cores)
        dscrs = dscr_all([self._cores[n].port for n in ns]) if ns else []
        halted = [n for n, d in zip(ns, dscrs) if d & DSCR_HALTED]
        for n, d in zip(ns, dscrs):
            if not d & DSCR_HALTED:
                self._cores[n].forget()                                    # 走っている: 覚えた値は使わない
        fresh = [(self._cores[n], d) for n, d in zip(ns, dscrs) if d & DSCR_HALTED and not self._cores[n].entered]
        enter_all([c for c, _ in fresh], [d for _, d in fresh])
        state = dict(zip(halted, read_state_all([self._cores[n] for n in halted], ("contextidr",), (MODE_SVC,))))
        out: dict[int, CoreState] = {}
        for n in ns:
            if n not in state:
                out[n] = CoreState(False)
                continue
            c, (ctx, sp_svc) = self._cores[n], state[n]
            pc, cpsr = c.read_registers(["pc", "cpsr"])
            moe = c.entry_reason()
            out[n] = CoreState(True, pc, cpsr, ctx, moe if moe is not None else -1, sp_svc, ctx >> 8)
        return out

    # ---------------------------------------------------------- run control
    def _confirm_halted(self, known: dict[int, int] | None = None) -> list[int]:
        """各コアが止まるのを待って状態を読む。止まらないコアは外す。DSCR を 1 回の往復で読み（known: 止まっているのを読んだ
        コアの DSCR。止まったコアは止まったままなので読み直さない）、止まっているコアにまとめて入る（全コアで 2 回の往復）。
        止まっていないコアだけ、1 つずつ要求して待つ。"""
        known = {n: d for n, d in (known or {}).items() if d & DSCR_HALTED}
        rest = [n for n in range(self.n_cores) if n not in known]
        known.update(zip(rest, dscr_all([self._cores[n].port for n in rest]) if rest else []))
        dscrs = [known[n] for n in range(self.n_cores)]
        fresh = [(c, d) for c, d in zip(self._cores, dscrs) if d & DSCR_HALTED and not c.entered]
        enter_all([c for c, _ in fresh], [d for _, d in fresh])
        halted = []
        for n, (c, d) in enumerate(zip(self._cores, dscrs)):
            try:
                if not d & DSCR_HALTED:
                    c.halt()
                halted.append(n)
            except ArmDebugError:
                pass
        return halted

    def _bound_report(self, method: str, halted: list[int], t_end: int, basis: str, running: list[int],
                      explained: set[int]) -> StopReport:
        return stop_report(self._ledger, self.n_cores, method, halted, t_end, basis, running, explained, self.parked)

    def halt_all(self) -> StopReport:
        fired = self._stop_watch()                     # 待つのを終えた後（タイムアウト・中断）に FPGA が止めていれば、その停止
        wait, self.last_wait = self.last_wait, None
        if fired is not None:
            wait = fired
        by_fpga = wait is not None and wait.get("by") == "fpga"
        timed = self._request_times is not None and not by_fpga
        mark = self._request_times.mark() if timed else None   # 走っているのを読む前に（読みと要求の間に往復を足さない）
        first = self._read_dscr(range(self.n_cores))
        running = [n for n, d in enumerate(first) if not d & DSCR_HALTED]
        explained = set(wait["hit"]) | set(wait["requested"]) if wait else set()
        if by_fpga:
            return self._halt_all_fpga(wait, running, explained)
        if self._cross is not None:
            return self._halt_all_cti(wait, running, explained)
        writes = [(self._cores[n].port.base + DRCR, DRCR_HRQ) for n in running]
        halt_request_all([self._cores[n].port for n in running])
        t_seen, dscr = self._seen_all_halted(running)
        span = self._request_span(mark, writes, running, t_seen, dscr) if timed else None   # 状態の読み出し（後の要求）の前に
        halted = self._confirm_halted({**dict(enumerate(first)), **dict(zip(running, dscr))})
        t_end = t_seen if t_seen is not None and set(running) <= set(halted) else self._clock()
        basis = wait["note"] + "; " + _SEQ_BASIS if wait and wait.get("note") else _SEQ_BASIS
        host = self._bound_report("sequential", halted, t_end, basis, running, explained)
        if wait is not None and wait.get("by") == "host" and not running:
            return self._event_report(wait, host)
        if not timed:
            return host
        if isinstance(span, str):
            return StopReport(host.method, host.cores_halted, host.skew_upper_bound_ns,
                              host.basis + "; the FPGA timestamps were not used: " + span)
        return StopReport("sequential", host.cores_halted, span.ns,
                          _FPGA_BASIS % (span.ticks, span.timebase_hz, "%d ns" % host.skew_upper_bound_ns
                                         if host.skew_upper_bound_ns is not None else "unknown", host.basis),
                          assumptions=(_SAME_HALT_TIME,), bound_without_assumptions_ns=host.skew_upper_bound_ns)

    def _event_report(self, wait, host: StopReport) -> StopReport:
        """コアが自分で止まり、ホストが他のコアに停止要求を書いた停止（wait_event のホストの方式）の報告。FPGA の時刻で上限を
        出せれば出し（仮定と、仮定なしの上限を並べる）、出せなければ PC の時計の上限のまま、出せない理由を根拠の文に足す。"""
        def not_used(reason: str) -> StopReport:
            return StopReport(host.method, host.cores_halted, host.skew_upper_bound_ns,
                              host.basis + "; the FPGA timestamps were not used: " + reason)
        if self._request_times is None:
            return not_used("fpga_request_times is off (the FPGA's event log is not read)")
        if self.parked:
            return not_used("cores %s were not resumed" % sorted(self.parked))
        hits, requested = list(wait["hit"]), list(wait["requested"])
        if set(hits) | set(requested) != set(range(self.n_cores)):
            return not_used("cores %s were neither found halted nor sent a halt request"
                            % sorted(set(range(self.n_cores)) - set(hits) - set(requested)))
        ports = {n: self._cores[n].port for n in range(self.n_cores)}
        try:
            span = self._request_times.event_stop_span(
                self._resume_mark, hits=hits, requested=requested, dscr={n: p.base + DSCR for n, p in ports.items()},
                drcr={n: p.base + DRCR for n, p in ports.items()}, hrq=DRCR_HRQ, rrq=DRCR_RRQ, halted=DSCR_HALTED)
        except NotTimed as e:
            return not_used(str(e))
        what = ("its last read of DBGDSCR with HALTED = 0" if span.lower_from == "read"
                else "the write of its restart request")
        measured = span.measured_ns
        host_text = "%d ns" % host.skew_upper_bound_ns if host.skew_upper_bound_ns is not None else "unknown"
        without = measured if measured is not None else host.skew_upper_bound_ns
        return StopReport("sequential", host.cores_halted, span.assumed_ns, _FPGA_EVENT_BASIS % (
            hits, span.lower_core, what, span.assumed_ticks, span.timebase_hz, _REACH,
            "%d ns" % measured if measured is not None else "not every core was read halted after its request",
            host_text + " (" + host.basis + ")"),
            assumptions=(_REACH,), bound_without_assumptions_ns=without)

    def _request_span(self, mark, writes, running, t_seen, dscr):
        """FPGA の時刻での、最初から最後の停止要求の書き込みまで（request_times.RequestSpan）。使えなければ理由（str）。
        使えるのは、全コアが走っているのを読み、この書き込みの後に全コアが止まっているのを読み、どのコアも止まった理由が
        停止要求（DSCR.MOE。走っているのを読んだ後に自分で止まったコアは、止まった時刻が要求の時刻ではない）のときだけ。"""
        if len(running) != self.n_cores:
            return ("cores %s were already halted when the stop was requested (not halted by these writes)"
                    % [n for n in range(self.n_cores) if n not in running])
        if t_seen is None:
            return "not every core was seen halted after the halt requests"
        other = [n for n, d in zip(running, dscr) if (d >> DSCR_MOE_SHIFT) & 0xF != MOE_HALT]
        if other:
            return ("cores %s report another entry reason than a halt request (DSCR.MOE): they did not stop at their "
                    "request's write" % other)
        try:
            return self._request_times.span(mark, writes)
        except NotTimed as e:
            return str(e)

    def _seen_all_halted(self, ns, polls: int = 200) -> tuple[int | None, list[int]]:
        """(ns の全コアが止まっているのを（1 回の往復で）読めた直後の時刻, そのときの DSCR)。polls 回読んでも揃わなければ
        (None, 最後の DSCR)。上限の終わりに使う（その後の状態の読み出しの時間を含めない）。"""
        dscr: list[int] = []
        for _ in range(polls):
            dscr = self._read_dscr(ns)
            if all(d & DSCR_HALTED for d in dscr):
                return self._clock(), dscr
        return None, dscr

    def _halt_all_fpga(self, wait, running, explained) -> StopReport:
        f = wait["fpga"]
        what = ("fpga halt watch: core %d stopped by itself; the FPGA wrote the halt request of every core one after the "
                "other (injection %s, %d words)" % (wait["hit"][0], f["result"], f["written"]))
        if "restarted_by_fpga" in f:
            what += ("; the FPGA had written the restart request of every core (DBGDRCR, %d words, timebase %d to %d) and "
                     "compared only its reads after them: core %d was read with DBGDSCR.HALTED = RESTARTED = 1 (halted after "
                     "its restart)%s" % (f["restarted_by_fpga"], f["t_restart_first"], f["t_restart_last"], wait["hit"][0],
                                        ", on its first read after the restart writes" if f["fire_first"] else ""))
        if wait.get("fpga_missed"):
            what += ("; cores %s were read halted by the host while the FPGA had not read them running since the restart "
                     "(sampler_match unmatched = 0): they stopped without being seen by the watch" % list(wait["fpga_missed"]))
        if running or f["result"] != "ok":
            halt_request_all([self._cores[n].port for n in running])
            halted = self._confirm_halted()
            return self._bound_report("sequential", halted, self._clock(), "%s; cores %s were still running and were halted "
                                      "by software requests; " % (what, running) + _SEQ_BASIS, running, explained)
        halted = self._confirm_halted()
        caller = "upper bound given by the caller" if self._watch_bound is not None else "bound not measured"
        host = self._bound_report("sequential", halted, self._clock(), _SEQ_BASIS, [], set(range(self.n_cores)))
        timed = self._watch_span(wait, halted)
        if isinstance(timed, str):
            return StopReport("sequential", tuple(halted), self._watch_bound,
                              what + "; " + caller + "; the FPGA timestamps were not used: " + timed)
        ticks, hz = timed
        host_text = "%d ns" % host.skew_upper_bound_ns if host.skew_upper_bound_ns is not None else "unknown"
        return StopReport("sequential", tuple(halted), -(-ticks * 1_000_000_000 // hz), what + "; " + _FPGA_WATCH_BASIS % (
            wait["hit"][0], ticks, hz, _REACH, "%d ns" % self._watch_bound if self._watch_bound is not None else "none",
            host_text), assumptions=(_REACH,), bound_without_assumptions_ns=host.skew_upper_bound_ns)

    def _watch_span(self, wait, halted):
        """FPGA の見張りが止めた停止の、FPGA の時刻での上限 (刻み, Hz)。使えなければ理由（str）。
        止まったコアは、FPGA がそれを最後に走っていると読んだ標本の読みを頼んだ転送の点（t_prev）より後に止まり、他のコアは
        FPGA の最後の停止要求の書き込みの点（t_last_write。Update-DR = 対象に届く瞬間）に要求を受ける。他のコアが要求で止まった
        （DSCR.MOE）ときだけ: 自分で止まったコアは t_prev より前に止まりえる（その標本の時刻は CSR に無い）。"""
        dev = getattr(self._watch, "dev", None)
        caps = dev.capabilities() if dev is not None else None
        if caps is None or not caps.timebase_hz:
            return "the device's timebase is not known"
        if not caps.dap_line_time:
            return ("this circuit timestamps the samples when their values arrive (no DAP_LINE_TIME): the last running "
                    "sample's time is not a lower bound of the halt")
        if len(halted) != self.n_cores:
            return "not every core halted"
        hit = wait["hit"][0]
        dscr = dscr_all([c.port for c in self._cores])
        other = [n for n, d in enumerate(dscr) if n != hit and (d >> DSCR_MOE_SHIFT) & 0xF != MOE_HALT]
        if other:
            return ("cores %s report another entry reason than a halt request (DSCR.MOE): they may have stopped before "
                    "the FPGA's last running sample of core %d" % (other, hit))
        f = wait["fpga"]
        return (f["t_last_write"] - f["t_prev"]) & ((1 << 64) - 1), caps.timebase_hz

    def _halt_all_cti(self, wait, running, explained) -> StopReport:
        if wait is not None and not self._event_route:     # 自分で止まったコア + ソフトウェアの要求で止めた他のコア: 順の停止
            halted = self._confirm_halted()
            self._cti_dirty = True
            return self._bound_report("sequential", halted, self._clock(), _SEQ_BASIS, running, explained)
        by_event = wait is not None and self._event_route
        request_bound, event_bound = self._cross_bounds
        if by_event:
            explained = set(range(self.n_cores))              # DBGACK → チャンネル → 他のコアの停止要求: ハードウェアが止める
        elif running:
            self._cross.halt_all()
            self._cti_dirty = True
        deadline = time.monotonic() + self._hw_wait_s
        while True:
            state = self._scan()
            if all(state) or time.monotonic() >= deadline:
                break
        stragglers = [n for n, h in enumerate(state) if not h]
        for n in stragglers:
            self._cores[n].port.halt_request()
        halted = self._confirm_halted()
        t_end = self._clock()
        if stragglers:
            return self._bound_report(
                "sequential", halted, t_end, "cti did not halt cores %s within %.3f s: halted by software requests; " % (
                    stragglers, self._hw_wait_s) + _SEQ_BASIS, running, explained | set(stragglers))
        if by_event:
            basis = ("cti hardware: the core's debug acknowledge routed to the others; "
                     + ("upper bound given by the caller" if event_bound is not None else "bound not measured"))
            return StopReport("simultaneous", tuple(halted), event_bound, basis)
        if running:
            return self._bound_report("simultaneous", halted, t_end, "", running, set(range(self.n_cores))) \
                if self.parked else StopReport("simultaneous", tuple(halted), request_bound,
                                               "cti hardware: one write halts every core; upper bound given by the caller")
        return StopReport("simultaneous", tuple(halted), None, "no core needed halting: when each stopped is not known")

    def wait_event(self, timeout_s: float) -> tuple[int, ...]:
        """コアが自分で止まる（ブレークポイント等）のを timeout_s まで待つ。止まったコア（タイムアウトなら空）。止まった瞬間に、
        ハードウェアの経路が無ければ他のコアに停止要求を出す（デバッガの処理が先に走ると、その間も他のコアは走り続ける）。"""
        if self._watch_armed:
            return self._wait_fpga(timeout_s)
        watch = [n for n in range(self.n_cores) if n not in self.parked]
        deadline = time.monotonic() + timeout_s
        while True:
            hit = [n for n, halted in zip(watch, self._scan_of(watch)) if halted]
            if hit:
                requested = []
                if not self._event_route:                     # 他の全コアへの要求を 1 回の往復で（読み直しの往復を挟まない）
                    requested = [n for n in range(self.n_cores) if n not in hit and n not in self.parked]
                    halt_request_all([self._cores[n].port for n in requested])
                self.last_wait = {"hit": tuple(hit), "requested": tuple(requested), "by": "host"}
                self._cti_dirty = self._cross is not None
                return tuple(hit)
            if time.monotonic() >= deadline:
                self.last_wait = None
                return ()

    def _wait_fpga(self, timeout_s: float) -> tuple[int, ...]:
        """FPGA の見張りの状態を読んで待つ（1 回の CSR の読みずつ）。止まったのを見つけた FPGA が他のコアに停止要求を書き終えたら返す。
        見逃しの確定（docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.6）: 見張りは全コアが止まっている間に始めてある
        （`resume`）ので、コアが止まって発火するのは、FPGA がそのコアの走っている値を読んだ後（`seen_running`）だけ。ホストが止まって
        いるのを読んだコア（`_watch_halted`）について、その読みの**後**の poll で seen_running が False なら、FPGA はそのコアの停止で
        発火しない（止まったコアは止まったまま: 後から走っている値は読めない）。そのときは見張りを外してホストの方式で止める。
        読む順（DSCR → poll）が逆だと、DSCR の前に FPGA が読んで発火する場合を見逃しと取り違える。全コアの seen_running がそろう
        までは、そろっていないコアの DSCR だけを読む（JTAG）。そろった後は JTAG を使わない（以後の停止は必ず発火する）。"""
        deadline = time.monotonic() + timeout_s
        while True:
            p = self._watch.poll()
            if p.matched:
                record = self._fpga_record(p)
                self._disarm_watch()
                self.last_wait = record
                return record["hit"]
            missed = sorted(n for n in self._watch_halted if not p.seen_running[n])
            if missed:
                return self._fpga_missed(missed)
            if not self._watch_covered:
                unseen = [n for n in range(self.n_cores) if not p.seen_running[n]]
                if not unseen:
                    self._watch_covered = True
                else:
                    halted = [n for n, h in zip(unseen, self._scan_of(unseen)) if h]
                    if halted:
                        self._watch_halted.update(halted)
                        continue                              # 確かめるのは、この読みの後の poll
            if time.monotonic() >= deadline:
                self.last_wait = None
                return ()
            if self._watch_poll_s:
                time.sleep(self._watch_poll_s)

    def _fpga_missed(self, missed: list[int]) -> tuple[int, ...]:
        """FPGA が発火できないコア（missed）が止まっている: 見張りを外し、外す前に別のコアで発火していたかを読み、ホストの方式で
        残りを止める。報告には、何を読んでそう決めたか（事実）を書く。"""
        self._disarm_watch()
        fired = self._fpga_record(self._watch.poll())
        note = ("fpga halt watch: cores %s were read halted by the host while the FPGA had not read them running since the "
                "restart (sampler_match unmatched = 0, read after the host's read): their stop cannot fire the watch; the "
                "host disarmed it and halted the rest" % missed)
        if fired is not None:
            f = fired["fpga"]
            hit = tuple(sorted(set(missed) | set(fired["hit"])))
            note += ("; before the disarm the FPGA fired on core %d and wrote the halt requests (injection %s, %d words)"
                     % (fired["hit"][0], f["result"], f["written"]))
        else:
            hit = tuple(n for n, h in enumerate(self._scan()) if h)
        requested = tuple(n for n in range(self.n_cores) if n not in hit and n not in self.parked)
        halt_request_all([self._cores[n].port for n in requested])
        self.last_wait = {"hit": hit, "requested": requested, "by": "host", "fpga_missed": tuple(missed), "note": note}
        if fired is not None:
            self.last_wait["fpga"] = fired["fpga"]
        return hit

    def resume(self, cores: Iterable[int]) -> None:
        """コアをまとめて再開する（docs/superpowers/specs/2026-11-01-stop-precision-design.md 3.8）:
        1. 全コアのレジスタを戻す（`ArmDebugCore.prepare_restart`。どのコアもまだ止まったまま。止まっていないコアがあれば、どのコアの
           要求も書かずに断る）。
        2. 全コアの再開の要求（DBGDRCR）と、その後の DSCR の読みを 1 回の往復で（`restart_request_all`）。
        3. RESTARTED がまだ 0 のコア（要求は届いたが、デバッグ状態を出ていない）だけ読み直す。
        要求は 1 回でまとめて書くので、途中で止めて残す「残り」は無い（`parked` は、デバッグ状態を出なかったコアだけ）。
        再開してすぐ止まったコア（HALTED と RESTARTED が両方 1）は、次の `wait_event` が見つけて他を止める（gdb の all-stop）。
        「ここより前には止まっていない」の時刻は、要求を送る直前（レジスタを戻した後）: どのコアも、それより後に再開する。
        CTI は、どのコアの再開の要求より前に release し、要求の後で停止要求のゲートを開け直す。
        FPGA の見張りは、全コアを再開するとき、レジスタを戻した後・要求の前（全コアが止まっている間）に始める: 止まったままの
        標本は「入った」にならず発火しない。走っている値を読んだ後に止まったコアで発火する。読む前に止まったコア（再開の直後の
        ブレークポイント等）は、`wait_event` が FPGA の印とホストの DSCR の読みで確かめてホストの方式に移る（`_wait_fpga`）。
        再開の後に始めると、始めるまでの往復の間に止まったコアで発火せず、見落とす（実機 2026-11-03: 20 回のうち 2 回）。"""
        ns = list(cores)
        if self._cross is not None and self._cti_dirty:
            self._cross.release()
            self._cti_dirty, self._needs_rearm = False, True
        self.parked = set()
        self._ledger.clear()
        try:
            if not ns:
                return
            prepare_restart_all([self._cores[n] for n in ns])  # 全コアのレジスタを戻す（1 回の往復）と ITR を切る（1 回の往復）
            # 再開の要求の前のイベント記録の位置（次にコアが自分で止まったとき、その後の記録だけを FPGA の時刻の上限に使う）
            self._resume_mark = self._request_times.mark() if self._request_times is not None else None
            watch = self._watch is not None and set(ns) == set(range(self.n_cores))
            if watch and self._watch_restarts:
                self._resume_by_fpga(ns)
                return
            try:
                if watch:
                    # 有効にしたと先に覚える: arm が途中で例外（リンクの時間切れ等）になっても、下の except と後の
                    # step・halt_all が外す（後に覚えると FPGA の見張りが有効のまま残った。レビュー 2026-11-03）
                    self._watch_armed, self._watch_halted, self._watch_covered = True, set(), False
                    self._watch.arm()
                t = self._clock()
                dscr = restart_request_all([self._cores[n].port for n in ns])
                for n in ns:
                    self._ledger.restarted(n, t)
                stopped_at_once = self._confirm_restarted(ns, dscr)
            except BaseException:
                self._disarm_watch()                          # 全コアが走ったと確かめられない: 見張りは使わない
                raise
            if watch:
                self._watch_halted = set(stopped_at_once)     # 再開の後に止まっているのを読んだ（`_wait_fpga` が確かめる）
        finally:
            if self._cross is not None and self._needs_rearm:
                self._cross.rearm_halt()
                self._needs_rearm = False

    def _resume_by_fpga(self, ns: list[int]) -> None:
        """全コアの再開を FPGA が書く（見張りを始めてから、始動の列で）。終わるのを待ち（各コアの timeout_s のうち最長まで）、
        成功なら、ホストが全コアの DSCR を読んでデバッグ状態を出たのを確かめる（`_confirm_restarted`）。再開の後のどの停止も
        FPGA が発火するので、`wait_event` は JTAG を使わない。失敗・時間切れは見張りを外し、事実（結果・受理された語の数）を
        例外にする。受理されなかった語のコアは再開の要求が書かれていない（parked）。"""
        t = self._clock()
        self._fpga_restart = None
        # 有効にしたと先に覚え、arm_and_restart も try の中で: 値の一致を有効にした後・go が届いた後に例外（リンクの時間切れ等）
        # になっても見張りを外す（後に覚えると FPGA の見張りが有効のまま残り、ホストの知らない停止を書きえた。レビュー 2026-11-03）
        self._watch_armed, self._watch_halted = True, set()
        try:
            self._watch.arm_and_restart()
            for n in ns:
                self._ledger.restarted(n, t)
            deadline = time.monotonic() + max(self._cores[n].timeout_s for n in ns)
            st = self._watch.restart_status()
            while st.pending and time.monotonic() < deadline:
                st = self._watch.restart_status()
            if st.pending:
                raise ArmDebugError("FPGA の再開の要求（始動の列）が終わらない: 割り込み書き込みが線を得ていない（要求は FPGA に"
                                    "残っている）")
            if st.result != "ok":
                self.parked = set(range(st.written, self.n_cores))   # 表はコアの順
                raise ArmDebugError("FPGA の再開の要求（始動の列）が失敗: %s、受理された語 %d / %d（受理されなかった語のコア %s は"
                                    "要求が書かれていない）" % (st.result, st.written, len(ns), sorted(self.parked)))
            self._fpga_restart = st
            self._confirm_restarted(ns, self._read_dscr(ns))
        except BaseException:
            self._disarm_watch()
            raise
        self._watch_covered = True                    # 門の後の停止は必ず発火する: 見逃しの確定（JTAG の読み）は要らない

    def _confirm_restarted(self, ns: list[int], dscr: list[int]) -> list[int]:
        """要求の後の DSCR（ns の順）から、全コアがデバッグ状態を出たのを確かめる。RESTARTED が 0 のコアだけ読み直す（各コアの
        timeout_s のうち最長まで。過ぎたら ArmDebugError）。返すのは、再開してすぐ止まったコア（HALTED と RESTARTED が 1）。"""
        state = dict(zip(ns, dscr))
        deadline = time.monotonic() + max(self._cores[n].timeout_s for n in ns)
        while True:
            late = [n for n in ns if not state[n] & DSCR_RESTARTED]
            if not late:
                return [n for n in ns if state[n] & DSCR_HALTED]
            if time.monotonic() >= deadline:
                self.parked = set(late)                       # 再開されずに止まったまま（次の停止の上限は「分からない」）
                raise ArmDebugError("再開しない: コア %s（DSCR %s）" % (late, ", ".join("0x%08X" % state[n] for n in late)))
            state.update(zip(late, self._read_dscr(late)))

    def resume_all(self) -> None:
        self.resume(range(self.n_cores))

    def step(self, core: int) -> None:
        """1 命令進める。PC にハードウェアのブレークポイントがあれば、ステップのあいだだけ外す（外さないと、再開と同時に成立して
        1 命令も進まない。fprobe.arm_a_target.ArmATarget.step と同じ）。"""
        self._disarm_watch()
        c = self._cores[core]
        pc = c.read_register("pc")
        lifted = [(slot, addr, width) for (n, slot), (addr, width) in self._hw_bps.items()
                  if n == core and addr & ~3 == pc & ~3]
        for slot, _, _ in lifted:
            c.clear_breakpoint(slot)
        try:
            c.step(mask_interrupts=True)
        finally:
            for slot, addr, width in lifted:
                c.set_breakpoint(slot, addr, thumb=(width == 2))

    # -------------------------------------------------------------- registers
    def regs(self, core: int) -> list[int]:
        return self._cores[core].read_registers(REGISTER_NAMES)            # 1 回の往復（r0・r1・PC・CPSR は覚えた値）

    def set_regs(self, core: int, vals: list[int]) -> None:
        self._cores[core].write_registers(list(zip(REGISTER_NAMES, vals)))  # 1 回の往復

    def read_reg(self, core: int, name: str) -> int:
        if name == "sp_svc":
            return self._cores[core].read_banked_sp(MODE_SVC)
        return self._cores[core].read_register(name)

    def write_reg(self, core: int, name: str, value: int) -> None:
        self._cores[core].write_register(name, value)

    def read_cp15(self, core: int, name: str) -> int:
        return self._cores[core].read_cp15(name)

    # ----------------------------------------------------------------- memory
    def _va(self, pa: int, length: int) -> int:
        va = pa - self.phys_offset + self.page_offset
        if va < self.page_offset or va + length > self.linear_end:
            raise OutsideLinearMap("physical 0x%x (+%d) is outside the kernel linear map (virtual 0x%08x..0x%08x)"
                                   % (pa, length, self.page_offset, self.linear_end))
        return va

    def read_kernel(self, va: int, length: int) -> bytes:
        """カーネルの仮想アドレスを、コア 0 の MMU を通して読む（USR で止まっていても特権モードで）。vmalloc も読める。"""
        a0 = va & ~3
        words = (va + length + 3 - a0) // 4
        c = self._cores[0]
        with c.privileged():
            vals = c.read_memory_fast(a0, words)
        if len(vals) != words:
            raise ArmDebugError("short read at 0x%x (+%d)" % (va, length))
        return b"".join(struct.pack("<I", v) for v in vals)[va - a0:va - a0 + length]

    def write_kernel(self, va: int, data: bytes) -> None:
        """書く。D キャッシュの PoU へのクリーンと I キャッシュ・分岐予測の無効化は `write_memory` が行う（MP 拡張があれば内側の
        共有域の全コアに）。"""
        c = self._cores[0]
        with c.privileged():
            if va % 4 == 0 and len(data) % 4 == 0:
                c.write_memory(va, list(struct.unpack("<%dI" % (len(data) // 4), data)), 4)
            else:
                c.write_memory(va, list(data), 1)

    def read_phys(self, addr: int, length: int) -> bytes:
        return self.read_kernel(self._va(addr, length), length)

    def write_phys(self, addr: int, data: bytes) -> None:
        self.write_kernel(self._va(addr, len(data)), data)

    def fetch(self, core: int, va: int, width: int) -> int:
        return self._cores[core].read_memory(va, 1, width)[0]

    def sync_caches(self, cores: Iterable[int] | None = None) -> None:
        for n in (range(self.n_cores) if cores is None else cores):
            self._cores[n].invalidate_instruction_caches()

    # ------------------------------------------------- hardware bp / watchpoints
    def set_hw_bp(self, core: int, slot: int, addr: int, width: int) -> None:
        self._cores[core].set_breakpoint(slot, addr, thumb=(width == 2))
        self._hw_bps[(core, slot)] = (addr, width)

    def clear_hw_bp(self, core: int, slot: int) -> None:
        self._cores[core].clear_breakpoint(slot)
        self._hw_bps.pop((core, slot), None)

    def set_hw_watch(self, core: int, slot: int, addr: int, length: int, kind: str) -> None:
        if not self._cores[core].set_watchpoint(slot, addr, length, _WATCH_KIND[kind]):
            raise RuntimeError("watchpoint of %d bytes at 0x%x is not possible: the hardware watches 1, 2 or 4 bytes "
                               "inside one word (size and alignment)" % (length, addr))

    def clear_hw_watch(self, core: int, slot: int) -> None:
        self._cores[core].clear_watchpoint(slot)
