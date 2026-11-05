"""`ArmACores` の部品: コアが自分で止まるのを待つ（`wait_event`）・再開・ステップ。

FPGA の見張りの状態を読んで待つ経路と、ホストが DSCR を読んで待つ経路、全コアをまとめて再開する経路（ホストが書く／FPGA が書く）。
停止の報告は `arma_halt`。
"""
from __future__ import annotations

import time
from typing import Iterable

from fprobe.arm_debug import DSCR_HALTED, DSCR_RESTARTED, ArmDebugError, prepare_restart_all
from fprobe.armv7a_debug import halt_request_all, restart_request_all


class ArmARunMixin:
    """待ちと再開（`ArmACores` の状態を使う）。"""

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
