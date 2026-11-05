"""`ArmACores` の部品: 全コアを止めて、停止の時間差の上限つきの報告（`StopReport`）を作る。

ソフトウェアの要求（順の停止）・CTI（同時の停止）・FPGA の見張り・FPGA の要求時刻の 4 つの経路の報告を組み立てる。
上限の式は `fprobe.linux_halt.skew`、根拠の文は `bases`。再開と待ちは `arma_run`。
"""
from __future__ import annotations

import time

from fprobe.arm_debug import DSCR_HALTED, DSCR_MOE_SHIFT, ArmDebugError, enter_all
from fprobe.armv7a_debug import DRCR, DRCR_HRQ, DRCR_RRQ, DSCR, dscr_all, halt_request_all

from .bases import FPGA_BASIS, FPGA_EVENT_BASIS, FPGA_WATCH_BASIS, REACH, SAME_HALT_TIME, SEQ_BASIS
from .core_access import MOE_HALT, StopReport
from .request_times import NotTimed
from .skew import stop_report


class ArmAHaltMixin:
    """全コアを止めて報告する（`ArmACores` の状態を使う）。"""

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
        basis = wait["note"] + "; " + SEQ_BASIS if wait and wait.get("note") else SEQ_BASIS
        host = self._bound_report("sequential", halted, t_end, basis, running, explained)
        if wait is not None and wait.get("by") == "host" and not running:
            return self._event_report(wait, host)
        if not timed:
            return host
        if isinstance(span, str):
            return StopReport(host.method, host.cores_halted, host.skew_upper_bound_ns,
                              host.basis + "; the FPGA timestamps were not used: " + span)
        return StopReport("sequential", host.cores_halted, span.ns,
                          FPGA_BASIS % (span.ticks, span.timebase_hz, "%d ns" % host.skew_upper_bound_ns
                                         if host.skew_upper_bound_ns is not None else "unknown", host.basis),
                          assumptions=(SAME_HALT_TIME,), bound_without_assumptions_ns=host.skew_upper_bound_ns)

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
        return StopReport("sequential", host.cores_halted, span.assumed_ns, FPGA_EVENT_BASIS % (
            hits, span.lower_core, what, span.assumed_ticks, span.timebase_hz, REACH,
            "%d ns" % measured if measured is not None else "not every core was read halted after its request",
            host_text + " (" + host.basis + ")"),
            assumptions=(REACH,), bound_without_assumptions_ns=without)

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
                                      "by software requests; " % (what, running) + SEQ_BASIS, running, explained)
        halted = self._confirm_halted()
        caller = "upper bound given by the caller" if self._watch_bound is not None else "bound not measured"
        host = self._bound_report("sequential", halted, self._clock(), SEQ_BASIS, [], set(range(self.n_cores)))
        timed = self._watch_span(wait, halted)
        if isinstance(timed, str):
            return StopReport("sequential", tuple(halted), self._watch_bound,
                              what + "; " + caller + "; the FPGA timestamps were not used: " + timed)
        ticks, hz = timed
        host_text = "%d ns" % host.skew_upper_bound_ns if host.skew_upper_bound_ns is not None else "unknown"
        return StopReport("sequential", tuple(halted), -(-ticks * 1_000_000_000 // hz), what + "; " + FPGA_WATCH_BASIS % (
            wait["hit"][0], ticks, hz, REACH, "%d ns" % self._watch_bound if self._watch_bound is not None else "none",
            host_text), assumptions=(REACH,), bound_without_assumptions_ns=host.skew_upper_bound_ns)

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
            return self._bound_report("sequential", halted, self._clock(), SEQ_BASIS, running, explained)
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
                    stragglers, self._hw_wait_s) + SEQ_BASIS, running, explained | set(stragglers))
        if by_event:
            basis = ("cti hardware: the core's debug acknowledge routed to the others; "
                     + ("upper bound given by the caller" if event_bound is not None else "bound not measured"))
            return StopReport("simultaneous", tuple(halted), event_bound, basis)
        if running:
            return self._bound_report("simultaneous", halted, t_end, "", running, set(range(self.n_cores))) \
                if self.parked else StopReport("simultaneous", tuple(halted), request_bound,
                                               "cti hardware: one write halts every core; upper bound given by the caller")
        return StopReport("simultaneous", tuple(halted), None, "no core needed halting: when each stopped is not known")
