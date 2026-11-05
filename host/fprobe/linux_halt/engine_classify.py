"""`Engine` の部品: 自分で止まったコアの分類（対象のプロセスの停止か、他人のイベントか）・終了/シグナルの配送・保留・ウォッチの当たり。

`_classify` は止まった理由（DSCR.MOE と命令）から `Stop` を作り、他人のイベントは数えるだけで隠さない。
同時に止まった複数のスレッドは 1 つを報告して残りを `pending` に保つ。
"""
from __future__ import annotations

from .core_access import MOE_BKPT_EVT, MOE_BKPT_INSN, MOE_WATCH_SYNC, WATCH_MOES
from .engine_types import SIGKILL, Stop
from .mmu import Unmapped


class EngineClassifyMixin:
    """停止の分類（`Engine` の状態 `core`・`bps`・`watches` 等を使う）。"""

    def _classify(self, halted):
        """自分で止まったコアのうち、対象のプロセスの停止を返す。無ければ None。他人のイベントは `stats` に数える（隠さない）。
        複数のスレッドが同じ瞬間に止まりうる。1 つを今報告し、他は止まったまま `pending` に残して、次に走らせる前に報告する
        （gdbserver の保留状態と同じ）。踏み越えると当たりを黙って失う。"""
        events = []                                      # (種類, コア, pc)
        for n in halted:
            s = self.core[n]
            if s.pc == self.exit_pc and not s.user:
                ev = self._exit_event(n, s)
                if ev is not None:
                    events.append(ev)
                continue
            if s.tid not in self.p.tids() and (s.user or s.moe in WATCH_MOES):
                self.p.refresh_threads()                  # 前の停止の後で作られたスレッドか
                self._identify()
                s = self.core[n]
            ours = s.tid in self.p.tids()
            if s.moe in WATCH_MOES:
                if not ours:
                    self.stats_counters["foreign_watch_hits"] += 1
                elif not s.user:
                    self.stats_counters["kernel_mode_watch_hits"] += 1   # カーネルがスレッドのために触っている
                else:
                    events.append(("watch", n, s.pc))
                continue
            if not s.user:
                continue
            if not ours:
                pa = self._foreign_pa(n, s.pc)
                if (pa is not None and self.bps.sw_at_pa(pa) is not None) or self.bps.get_hw(s.pc) is not None:
                    self.stats_counters["foreign_bp_hits"] += 1
                    self.foreign_hit_seq += 1
                    self.foreign_hits.append({"seq": self.foreign_hit_seq, "core": n, "tid": s.tid, "address": s.pc,
                                              "moe": s.moe})
                continue
            bp = self.bps.lookup(self.pid, s.pc)
            if bp is not None and s.moe in (MOE_BKPT_INSN, MOE_BKPT_EVT, -1):
                if bp.tmp and not bp.user:
                    if self._want is not None and self._want == (s.pc, s.tid):
                        events.append(("trap", n, s.pc))
                    continue                              # （他の内部ブレークポイントは無視）
                events.append(("bp", n, s.pc))
            elif s.moe in (MOE_BKPT_INSN, MOE_BKPT_EVT):
                # 対象のスレッドがブレークポイントで止まったのに、表に無い番地（または入った理由が合わない）: 数える（隠さない）
                self.stats_counters["own_stops_without_breakpoint"] += 1
        if not events:
            return None
        for e in events:
            if e[0] in ("exit", "signal"):               # プロセスが死ぬ: 他は関係ない
                return self._deliver(e)
        self.pending.extend(e for e in events[1:] if e[0] != "trap")
        return self._deliver(events[0])

    def _exit_event(self, n: int, s):
        """スレッドが do_exit に入った。
        * スレッドグループが終了中（SIGNAL_GROUP_EXIT。do_group_exit が最初の do_exit の前に立てる）: プロセスの終了で、
          最初のスレッドで見える。signal->group_exit_code が最終コード。コードにシグナル番号があれば、そのシグナルで死ぬ:
          まず**停止**として報告し（pt_regs はシグナルが配送された瞬間のもの。SIGSEGV なら落ちた命令）、死は次の再開で。
          SIGKILL は横取りできない。
        * そうでなければ、1 スレッドが抜けるだけ。最後の 1 つなら終了。
        * 別のプロセスの終了は関係ない（再開して数える）。"""
        task = self.p.task(s.tid)
        if task is None:
            self.stats_counters["foreign_exit_hits"] += 1
            return None
        o = self.k.o
        sig = self.k.u32(task.addr + o["signal"])
        if self.k.u32(sig + o["signal_flags"]) & o["signal_group_exit"]:
            code = self.k.u32(sig + o["group_exit_code"])
            signo = code & 0x7F
            if signo and signo != SIGKILL:
                return ("signal", n, code)
            return ("exit", n, code)
        if self.k.u32(sig + o["live"]) > 1:
            self.stats_counters["thread_exits"] += 1
            return None
        return ("exit", n, self.c.regs(n)[0])            # r0 = do_exit() に渡されたコード

    def _deliver(self, ev):
        kind, n, pc = ev
        s = self.core[n]
        if kind == "exit":
            return Stop("exit", s.tid, code=pc)           # （ここでは 3 つ目が終了コード）
        if kind == "signal":
            self.dying = (s.tid, pc)                      # 死は次の再開で報告する
            return Stop("signal", s.tid, signal=pc & 0x7F, code=pc)
        if kind == "watch":
            return self._finish_watch(n)
        if kind == "trap":
            return Stop("trap", s.tid, pc)
        bp = self.bps.lookup(self.pid, pc)
        self.reported[s.tid] = pc                      # このスレッドは、報告済みのブレークポイントの上にいる
        return Stop("hwbreak" if bp.hw else "swbreak", s.tid, pc)

    def _take_pending(self):
        """まだ有効な保留イベントの次の 1 つを、何も走らせずに Stop として返す。無ければ None。止まっている間にスレッドが
        動いた・死んだ・ブレークポイントが外された、イベントは古い。"""
        while self.pending:
            kind, n, pc = self.pending.pop(0)
            s = self.core.get(n)
            if s is None or not s.halted or not s.user or s.pc != pc or s.tid not in self.p.tids():
                continue
            if kind == "bp" and self.bps.lookup(self.pid, pc) is None:
                continue
            return self._deliver((kind, n, pc))
        return None

    # --------------------------------------------------------------- watch
    def _watch_values(self):
        out = {}
        for w in self.watches or ():
            try:
                out[w] = self.mem.read(w.addr, w.length)
            except Unmapped:
                out[w] = None
        return out

    def _finish_watch(self, n: int) -> Stop:
        """コア n（対象のスレッド、ユーザモード）がウォッチポイントで止まった。同期のウォッチポイントは当たった命令が完了する
        **前**に止まるが、デバッガは完了後の状態を期待する。そのコアのウォッチを外して 1 命令進めてから報告する。
        どのウォッチかはハードウェアが記録しない（DFAR/WFAR にデータのアドレスは入らない）。前後の値で書き込みの範囲を
        特定する。そうでなければ登録の全部が候補で、`Stop.candidates` がその数（最初のものを報告）。"""
        s = self.core[n]
        tid = s.tid
        before = self._watch_values()
        if s.moe == MOE_WATCH_SYNC and self.watches is not None:
            self.watches.suspend(n)
            try:
                self._step_core(n)
            finally:
                self.watches.restore(n)
        after = self._watch_values()
        watches = list(self.watches or ())
        changed = [w for w in watches if before[w] is not None and after[w] is not None and before[w] != after[w]]
        picked = changed or watches
        if not picked:
            return Stop("watch", tid)
        w = picked[0]
        return Stop("watch", tid, w.addr, w.kind, len(picked))
