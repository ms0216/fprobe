"""1 つの Linux プロセスの all-stop 実行制御。

モデル: デバッガが止まっているときは、**全コアが止まっている**。デバッグ対象はプロセスの全スレッド。他のプロセスを走らせて
いるコアは見えないが、共有のブレークポイントに当たりうる（黙って踏み越える。数は `stats` に残す）。

不変条件（ターゲットを正気に保つもの）:
  * コアを走らせるものは、先に `_sync()` を通る。書いたコード（BKPT の出し入れ）を命令フェッチから見えるようにし、メモリの見えを捨てる。
  * コアの PC の下のソフトウェアブレークポイントは、再開の前の 1 命令のステップのあいだだけ外す。
  * ステップのあいだは他の全コアを止めておく（Cortex-M のデバッガと同じく決定的）。
  * 一時的なブレークポイント（内部の run-to）は、作った呼び出しが持つ。
  * 停止の時間差は隠さない: `stop()` が毎回返す `StopResult`（方式・上限・根拠・基準との比較）と、`stats().skew`。
"""
from __future__ import annotations

import collections
import struct
import time
from contextlib import nullcontext
from dataclasses import replace

from .bp import BreakpointTable, NoFreeSlot
from .core_access import CoreAccess, MOE_EXT, MOE_HALT, StopReport, WATCH_MOES
from .kernel import Kernel
from .mmu import Unmapped, translate
from .process import Process, ProcessExited
from .watch import WatchTable
from .engine_classify import EngineClassifyMixin
from .engine_types import SIGINT, SIGKILL, SIGTRAP, EngineError, EngineStats, Stop, StopResult

# 型は `engine_types`、停止の分類は `engine_classify` に分けた（旧来の `fprobe.linux_halt.engine.<名前>` を保つ）
__all__ = ["SIGINT", "SIGKILL", "SIGTRAP", "Engine", "EngineError", "EngineStats", "Stop", "StopResult"]


class Engine(EngineClassifyMixin):
    def __init__(self, cores: CoreAccess, kernel: Kernel, proc: Process, bps: BreakpointTable, *, skew_limit_ns: int,
                 thread_id: str, kernel_thumb: bool, max_step_retries: int, interrupted=lambda: False, sleep=time.sleep,
                 watches: WatchTable | None = None):
        """skew_limit_ns: 「同時」とみなす上限（ns）。必須（既定なし）。thread_id: "stack"（sp_svc がスレッドのカーネルスタックに
        あるか）か "contextidr"（CONTEXTIDR>>8。CONFIG_PID_IN_CONTEXTIDR のカーネルだけ）。kernel_thumb: カーネルのコードが Thumb か。
        max_step_retries: 1 命令がユーザモードを抜けて戻ってくる（アボート、デマンドページング）のを何回まで待つか（必須。既定なし）。"""
        if thread_id not in ("stack", "contextidr"):
            raise ValueError("thread_id must be 'stack' or 'contextidr'")
        self.c, self.k, self.p, self.bps = cores, kernel, proc, bps
        self.mem = proc.mem
        self.pid = proc.tgid
        self.max_step_retries = max_step_retries
        self.n = cores.n_cores
        self.watches = watches
        self.skew_limit_ns = skew_limit_ns
        self.interrupted, self.sleep = interrupted, sleep
        self.thread_id = thread_id
        self.core: dict[int, object] = {}
        self._want = None                  # run_to の目的 (addr, tid)
        self.pending: list[tuple] = []     # まだ報告していない、他のスレッドの (種類, コア, pc)
        self.reported: dict[int, int] = {}  # tid → 報告済みのブレークポイントの pc
        self.dying = None                  # 致命的シグナルを停止として報告した後の (tid, 終了コード)
        self.icache_stale: set[int] = set()
        self.stats_counters = {"foreign_bp_hits": 0, "foreign_watch_hits": 0, "kernel_mode_watch_hits": 0,
                               "foreign_exit_hits": 0, "thread_exits": 0, "own_stops_without_breakpoint": 0}
        # 対象外と判定したブレークポイントの当たりの直近（コア・見分けた tid・番地・入った理由）。数は上の数えが全部を持つ。
        # 見分け方（thread_id）が対象のカーネルに合わないと、自分の当たりもここに来る: その事実を使う側に見せるため
        self.foreign_hits: collections.deque = collections.deque(maxlen=16)
        self.foreign_hit_seq = 0
        self._skew = {k: {"stops": 0, "max_ns": None, "unknown": 0, "exceeded": 0}
                      for k in ("event", "interrupt", "request")}
        self._cut_short = 0
        self.last_stop_result: StopResult | None = None
        self.running = False               # run() が時間切れで、全コアを走らせたまま返した（次の run は再開せずに待つ）
        self.exit_pc = kernel.o["do_exit"]
        self.exit_width = 2 if kernel_thumb else 4

    # ---------------------------------------------------------------- stops
    def stats(self) -> EngineStats:
        return EngineStats(dict(self.stats_counters), {k: dict(v) for k, v in self._skew.items()}, self._cut_short)

    def _verdict(self, report: StopReport) -> str:
        if report.skew_upper_bound_ns is None:
            return "unknown"
        return "within" if report.skew_upper_bound_ns <= self.skew_limit_ns else "exceeded"

    def stop(self, kind: str = "request") -> StopResult:
        """全コアを止めて、止まった世界を読み直す。止まらないコアがあれば、止まったコアを再開して EngineError
        （止めたままにしない）。毎回、方式・上限・基準との比較を返す。"""
        try:
            report = self.c.halt_all()
        except Exception as e:                                  # バックエンドの失敗: 途中まで止まったコアを走らせ直す
            raise EngineError("halting the cores failed (%s: %s); %s" % (type(e).__name__, e, self._resume_halted(None))) from e
        missing = sorted(set(range(self.n)) - set(report.cores_halted))
        if missing:
            raise EngineError("cores %s did not halt; %s" % (missing, self._resume_halted(list(report.cores_halted))))
        self.running = False
        verdict = self._verdict(report)
        s = self._skew[kind]
        s["stops"] += 1
        if report.skew_upper_bound_ns is None:
            s["unknown"] += 1
        else:
            s["max_ns"] = report.skew_upper_bound_ns if s["max_ns"] is None else max(s["max_ns"], report.skew_upper_bound_ns)
            if verdict == "exceeded":
                s["exceeded"] += 1
        self.last_stop_result = StopResult(report, verdict)
        self.refresh()
        return self.last_stop_result

    def _resume_halted(self, cores) -> str:
        """停止に失敗した後始末: 止まっているコアを走らせ直す（cores=None なら、いま止まっているもの全部）。バックエンドの resume は、
        再開済みのコアが自分で止まったときに残りを走らせず "parked" に残すので、残りがなくなるまで繰り返す。結果を言葉で返す
        （走らせ直せなかったときは、そのコアと理由を言う。黙って成功にしない）。"""
        try:
            targets = [n for n, h in enumerate(self.c.halted()) if h] if cores is None else list(cores)
            pending = list(targets)
            for _ in range(self.n + 1):
                if not pending:
                    break
                self.c.resume(pending)
                pending = sorted(getattr(self.c, "parked", None) or ())
            still = [n for n, h in enumerate(self.c.halted()) if h and n in targets]
        except Exception as e:  # noqa: BLE001 - 後始末の失敗も事実として返す
            return "resuming them failed too (%s: %s); cores that may still be halted: %s" % (
                type(e).__name__, e, "unknown" if cores is None else list(cores))
        if still:
            return "cores %s are STILL HALTED (they stopped by themselves again)" % still
        return "the cores that had halted were resumed"

    def attach(self) -> StopResult:
        result = self.stop()
        self.bps.insert(self.pid, self.exit_pc, kind="hw", width=self.exit_width, tmp=True)
        return result

    def refresh(self, threads: bool = True) -> None:
        """止まるたびに呼ぶ。止まった世界を読み直す。複数命令の操作の途中なら、最後に 1 回だけ読むように threads=False。"""
        self.mem.invalidate()
        self.core = self.c.snapshot()
        if threads:
            self.p.refresh_threads()
        self._identify()

    def _identify(self, cores=None) -> None:
        """止まった各コアが、対象のどのスレッドを走らせているか決める（`CoreState.tid`。-1 = 対象ではない）。
        thread_id "stack": バンクされた SVC の SP は、そのコアが走らせているスレッドのカーネルスタックの中にある（ユーザモードでも、
        カーネルに戻るときのスタックの上端として）。標準のカーネルで動く。（TPIDRURO は使えない: ユーザモードでは TLS のポインタ。）
        thread_id "contextidr": CONTEXTIDR>>8（CONFIG_PID_IN_CONTEXTIDR のカーネルだけ。tid に既に入っている）。"""
        if self.thread_id != "stack":
            return
        for n in (self.core if cores is None else cores):
            s = self.core[n]
            if s.halted:
                t = self.k.owner_of_stack_address(self.p.threads, s.sp_svc)
                tid = t.pid if t is not None else -1
                if tid != s.tid:
                    self.core[n] = replace(s, tid=tid)

    def live(self, tid: int):
        """スレッド tid がユーザモードで止まっている（レジスタが生きている）コア。無ければ None。"""
        for n, s in self.core.items():
            if s.halted and s.user and s.tid == tid:
                return n
        return None

    def default_tid(self) -> int:
        """何も指定しないときの「現在の」スレッド: 対象のスレッドを走らせている最小番号のコア、無ければプロセスのリーダー。
        規則であって判断ではない（頼れるように書いておく）。"""
        tids = self.p.tids()
        for n, s in sorted(self.core.items()):
            if s.halted and s.tid in tids:
                return s.tid
        return self.p.tgid

    # ------------------------------------------------------------ registers
    def get_regs(self, tid: int) -> list[int]:
        n = self.live(tid)
        if n is not None:
            return self.c.regs(n)
        u = struct.unpack("<18I", self._ptregs(tid))
        return list(u[:17])      # r0-r15, cpsr（orig_r0 は除く）

    def set_regs(self, tid: int, vals: list[int]) -> None:
        n = self.live(tid)
        if n is not None:
            self.c.set_regs(n, vals)
        else:
            a = self._ptregs_addr(tid)
            cur = list(struct.unpack("<18I", self.c.read_kernel(a, 72)))
            cur[:17] = vals[:17]
            self.c.write_kernel(a, struct.pack("<18I", *cur))
        self.refresh()

    def _ptregs_addr(self, tid: int) -> int:
        t = self.p.task(tid)
        if t is None:
            raise EngineError("tid %d is not a thread of pid %d now (it exited, or the thread list is stale; threads: %s)"
                              % (tid, self.p.tgid, sorted(self.p.tids())))
        return self.k.pt_regs_addr(t)

    def _ptregs(self, tid: int) -> bytes:
        return self.c.read_kernel(self._ptregs_addr(tid), 72)

    # ------------------------------------------------------ execution gate
    def _sync(self, cores=None) -> None:
        """何かを走らせる前に呼ぶ。書いたコードを見えるようにし、メモリの見えを捨てる。命令キャッシュはコアごと:
        止まったままのコアにはまだ要らないので、古いままのコアを覚えておき、次の全体の _sync で走る前に流す。"""
        if self.mem.code_dirty or self.bps.code_dirty:
            self.icache_stale = set(range(self.n))
            self.mem.code_dirty = False
            self.bps.code_dirty = False
        todo = self.icache_stale if cores is None else self.icache_stale & set(cores)
        if todo:
            self.c.sync_caches(sorted(todo))
            self.icache_stale -= todo
        self.mem.invalidate()

    def _foreign_pa(self, n: int, va: int):
        """別のプロセスを走らせているコア n の pc の物理アドレス（そのコアの TTBR0 でウォーク）。短い記述子だけ実装。"""
        if self.mem.fmt != "short":
            raise EngineError("handling a breakpoint hit by another process is implemented for the 'short' translation "
                              "format only (this process uses %r)" % self.mem.fmt)
        table = self.c.read_cp15(n, "ttbr0") & 0xFFFFC000
        try:
            return translate(self.c.read_phys, table, va, fmt="short")
        except Unmapped:
            return None

    def _lifted(self, n: int):
        s = self.core[n]
        if s.tid in self.p.tids():
            return self.bps.lifted(self.pid, s.pc)
        pa = self._foreign_pa(n, s.pc) if s.user else None
        return self.bps.lifted_pa(pa) if pa is not None else nullcontext()

    def _step_core(self, n: int) -> None:
        """コア n だけを 1 命令ステップする（他は止めたまま）。PC の下の sw ブレークポイントはそのコアのために外す。"""
        with self._lifted(n):
            self._sync([n])
            self.c.step(n)
        # 走ったのはコア n だけ。他は止めたままの姿で、1 命令でスレッド一覧は変わらない（step() が最後に 1 回読み直す）。
        self.mem.invalidate()
        self.core[n] = self.c.snapshot([n])[n]
        self._identify([n])

    def _bp_under(self, n: int) -> bool:
        s = self.core[n]
        if s.tid in self.p.tids():
            return self.bps.lookup(self.pid, s.pc) is not None
        pa = self._foreign_pa(n, s.pc) if s.user else None
        return (pa is not None and self.bps.sw_at_pa(pa) is not None) or self.bps.get_hw(s.pc) is not None

    def _step_over_bps(self, cores) -> None:
        """報告してはいけない当たり（他のプロセスの）を踏み越えるためのステップ。"""
        for n in cores:
            if self._bp_under(n):
                self._step_core(n)

    def _step_over_reported(self) -> None:
        """再開の前に、**既に報告した**ブレークポイントの上のスレッドを 1 命令進める（さもないと同じ当たりを再び報告する）。
        報告していない当たりの上のスレッド（他のスレッドの当たりで止められた直前だった等）は、そのまま再開して当たらせる。"""
        for n in range(self.n):
            s = self.core[n]
            if s.halted and self.bps.lookup(self.pid, s.pc) is not None and self.reported.get(s.tid) == s.pc:
                del self.reported[s.tid]
                self._step_core(n)

    def _fetch(self, n: int):
        st = self.core[n]
        width = 2 if st.thumb else 4
        return self.c.fetch(n, st.pc, width), width, st.thumb

    @staticmethod
    def _is_svc(insn: int, thumb: bool) -> bool:
        return (insn & 0xFF00) == 0xDF00 if thumb else (insn & 0x0F000000) == 0x0F000000

    # ------------------------------------------------------------- continue
    def cont(self) -> Stop:
        try:
            return self._cont_until(None)[0]
        except ProcessExited:
            return Stop("exit")

    def run(self, timeout_s: float, clock=time.monotonic) -> Stop:
        """cont() と同じ道で走らせ、対象のプロセスが止まるまで最大 timeout_s 秒待つ（上限は呼び出し側が明示）。止まれば
        全コアを止めた Stop。上限を過ぎたら全コアを**走らせたまま** Stop("timeout") を返し、`running` を立てる（次の run は
        再開せずに待ちを続け、halt() は要求で止める）。上限の判定は wait_event を 1 回呼んだ後（0 でも、いまある当たりは拾う）。"""
        if timeout_s < 0:
            raise ValueError("timeout_s must be >= 0")
        deadline = clock() + timeout_s
        try:
            if self.running:
                return self._wait(deadline, clock)
            return self._cont_until(None, deadline, clock)[0]
        except ProcessExited:
            return Stop("exit")

    def halt(self) -> Stop:
        """run() が走らせたままの全コアを要求で止め、世界を読み直す。止め要求と同じ瞬間に自分のイベントで止まったコアが
        あれば（入った理由で分かる）、要求の停止ではなくその当たりを返す（_wait と同じ）。止まっていれば EngineError。"""
        if not self.running:
            raise EngineError("the cores are already halted")
        try:
            self.stop(kind="request")
        except ProcessExited:
            return Stop("exit")
        own = [n for n in range(self.n) if self.core[n].moe not in (MOE_HALT, MOE_EXT, -1)]
        if own:
            ev = self._classify(own)
            if ev is not None:
                return ev
            self._step_over_bps(own)                     # 別のプロセスの当たり: 踏み越えておく（止めたまま）
        return Stop("int", self.default_tid())

    def run_to(self, addr: int, width: int, tid: int, deadline: float | None = None,
               clock=time.monotonic) -> tuple[Stop, bool]:
        """スレッド tid がユーザアドレス addr に着くまで全コアを走らせる（一時的なブレークポイント）。(Stop, 着いたか)。
        途中で利用者のブレークポイント・終了等に当たれば、その Stop と False。deadline（clock の時刻。使う側が明示）を
        過ぎたら全コアを要求で止め、一時的なブレークポイントを外して Stop("timeout", tid) と False（止めたまま返す。
        止めた瞬間に自分のイベントで止まったコアがあればその当たり。halt と同じ）。"""
        try:
            return self._run_to(addr, width, tid, deadline, clock)
        except ProcessExited:
            return Stop("exit"), False

    def _cont_until(self, want, deadline=None, clock=time.monotonic):
        """対象のプロセスが止まるまで全部を走らせる。want=(addr, tid) は内部の 1 回限りの目的地。(Stop, 目的地に着いたか) を返す。"""
        if self.dying is not None:                       # 致命的シグナルの前で止めた。ここで死ぬ。
            tid, code = self.dying
            self.dying = None
            return Stop("exit", tid, code=code), False
        stop = self._take_pending()
        if stop is not None:
            return stop, False
        self._want = want
        try:
            while True:
                self._step_over_reported()
                self._sync()
                self.c.resume(range(self.n))
                self.running = True
                if getattr(self.c, "parked", None):
                    self._cut_short += 1                  # 最後のコアを再開する前に別のコアが止まった
                ev = self._wait(deadline, clock)
                if ev is not None:
                    return ev, bool(want and ev.kind == "trap" and ev.addr == want[0] and ev.tid == want[1])
        finally:
            self._want = None

    def _wait(self, deadline=None, clock=time.monotonic):
        while True:
            if self.interrupted():
                self.stop(kind="interrupt")
                return Stop("int", self.default_tid())
            halted = self.c.wait_event(0.02)
            if not halted:
                if deadline is not None and clock() >= deadline:
                    return Stop("timeout")                # 全コアは走ったまま（running は立ったまま）
                continue
            self.stop(kind="event")                      # 全コアを止めて読み直す。止めている間に対象が死ねば ProcessExited
            # wait_event が走査した後、止め要求が届く前に、別のコアが自分のイベントに当たりうる。入った方法で分かる
            # （こちらの止め要求は MOE_HALT、CTI の要求は MOE_EXT で入る）: 要求した停止として扱ってはいけない。
            halted = sorted(set(halted) | {n for n in range(self.n) if self.core[n].moe not in (MOE_HALT, MOE_EXT, -1)})
            ev = self._classify(halted)
            if ev is not None:
                return ev
            # 対象ではない（別のプロセスが共有のブレークポイントに当たった）: 踏み越えて走らせ、待ち続ける。
            self._step_over_bps(halted)
            self._sync()
            self.c.resume(range(self.n))
            self.running = True
            # 対象外の当たりが途切れなくても上限を守る（確かめるのが当たりの無い周だけだと、毎秒何千万回も通る行や
            # スレッドの見分けの不一致で永久に返らない。2026-11-03 実機）
            if deadline is not None and clock() >= deadline:
                return Stop("timeout")                    # 全コアは走ったまま（running は立ったまま）

    # ----------------------------------------------------------- run-to
    def _run_to(self, addr: int, width: int, tid: int, deadline=None, clock=time.monotonic, prefer_hw: bool = False):
        """内部: スレッド tid がユーザアドレス addr に戻るまで全コアを走らせる（deadline は run_to）。
        prefer_hw: 命令のメモリを書き換えないハードウェアのブレークポイントを使う（空きが無ければソフトウェア）。ステップで例外に入った
        命令（VFP の初回使用の未定義命令など）に戻るのを待つとき、カーネルが例外の処理でその命令をメモリから読み直すので、BKPT に
        書き換えていると SIGILL になる（実機 2026-11-05: data_types の step）。"""
        kind = "sw"
        if prefer_hw:
            try:
                self.bps.insert(self.pid, addr, kind="hw", width=width, tmp=True)
                kind = "hw"
            except NoFreeSlot:
                pass
        if kind == "sw":
            kind = self.bps.insert(self.pid, addr, kind="sw", width=width, tmp=True)   # 置けなくて hw に切り替わったら、その方式で外す
        try:
            stop, reached = self._cont_until((addr, tid), deadline, clock)
            if stop.kind == "timeout":                   # 走らせたまま: 止めてから一時的なブレークポイントを外す
                stop = self.halt()
                if stop.kind == "int":
                    stop = Stop("timeout", tid)
            return stop, reached
        finally:
            self.bps.remove(self.pid, addr, kind=kind, tmp=True)

    # ----------------------------------------------------------------- step
    def step(self, tid: int, deadline: float | None = None, clock=time.monotonic) -> Stop:
        """スレッド tid を 1 命令進める。眠っている・カーネルの中・SVC・ユーザモードを抜けた命令は run_to で戻るのを待つ
        （deadline はその待ちの上限。run_to と同じ。過ぎたら Stop("timeout", tid) で止めたまま）。"""
        try:
            stop = self._step(tid, deadline, clock)
            self.p.refresh_threads()          # ステップの要求ごとに 1 回（終了の確認も兼ねる）
            return stop
        except ProcessExited:
            return Stop("exit")

    def _step(self, tid: int, deadline=None, clock=time.monotonic) -> Stop:
        n = self.live(tid)
        if n is None:
            # 眠っている／カーネルの中: pt_regs.pc でユーザモードに戻る。まずそこへ行く。
            regs = self.get_regs(tid)
            pc, cpsr = regs[15], regs[16]
            stop, reached = self._run_to(pc & ~1, 2 if cpsr & 0x20 else 4, tid, deadline, clock, prefer_hw=True)
            if not reached:
                return stop
            n = self.live(tid)
            if n is None:
                return stop
        pc0 = 0
        for _ in range(self.max_step_retries):
            pc0 = self.core[n].pc
            insn, width, thumb = self._fetch(n)
            if self._is_svc(insn, thumb):
                stop, reached = self._run_to(pc0 + width, width, tid, deadline, clock)
                return Stop("trap", tid, stop.addr) if reached else stop
            self._step_core(n)
            s = self.core[n]
            if s.moe in WATCH_MOES and s.user and s.tid == tid:
                return self._finish_watch(n)       # ステップした命令そのものが、見張りの範囲に触った
            if s.user and s.tid == tid:
                return Stop("trap", tid, s.pc)
            # ユーザモードを抜けた（データ/プリフェッチアボート、デマンドページング…）: pc0 で再開されるまで待つ。
            stop, reached = self._run_to(pc0, width, tid, deadline, clock, prefer_hw=True)
            if not reached:
                return stop
            n = self.live(tid)
            if n is None:
                return stop
        # 成功ではない: 終わったステップとして報告せず、何が起きたかを言う。
        raise EngineError("the instruction at 0x%08x did not complete after %d attempts: each time the thread left "
                          "user mode and came back to the same instruction (a fault that is not resolved?)"
                          % (pc0, self.max_step_retries))

    # -------------------------------------------------------------- detach
    def close(self) -> list:
        """ターゲットをきれいにして去る: メモリに BKPT なし、HW の bp/wp なし、キャッシュ一貫、全コアが走っている。"""
        errors = []
        # BKPT を外すにはメモリを書くので、止まっているコアが要る。走らせている間の失敗で終わるときはコアが走っている
        fns = [lambda: all(self.c.halted()) or self.c.halt_all(), self.bps.clear]
        if self.watches is not None:
            fns.append(self.watches.clear)
        fns += [self._sync, lambda: self.c.resume(range(self.n))]
        for fn in fns:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - できるだけ続ける
                errors.append(e)
        return errors
