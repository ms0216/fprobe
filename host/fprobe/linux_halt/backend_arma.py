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

import time
from typing import Iterable, Sequence

from fprobe.arm_debug import DSCR_HALTED, ArmDebugCore, ArmDebugError, enter_all, read_state_all
from fprobe.armv7a_debug import BCR, DSCR, WCR, dscr_all, read_all

from .arma_halt import ArmAHaltMixin
from .arma_regmem import MODE_SVC, ArmARegMemMixin
from .arma_run import ArmARunMixin
from .arma_watch import ArmAWatchMixin
from .core_access import CoreState
from .skew import NotHaltedBefore


class ArmACores(ArmAWatchMixin, ArmAHaltMixin, ArmARunMixin, ArmARegMemMixin):
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
