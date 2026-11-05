"""`HaltSession` の部品: 手元の gdb がつなぐ RSP サーバ（`gdb_serve`・`gdb_status`・`gdb_stop`・`wait_gdb`）と、持ち主の後始末。

別のスレッドで 127.0.0.1 の口を開き、gdb がつないだら全コアを止めて応える。終わるとき（detach・切断・stop・誤り）は、BKPT と
HW の bp/wp を外して全コアを走らせ、停止デバッグを切る（DAP の誤りなら DP をつなぎ直してもう 1 度）。後始末は
`linux.halt.debug`（`halt_debug`）の持ち主も使う。
"""
from __future__ import annotations

import contextlib
import dataclasses
import socket
import threading
from dataclasses import dataclass
from typing import Callable

from . import bp as bpmod
from .bp import UNMAPPED_POLICIES, BreakpointTable
from .engine import Engine, EngineError
from .halt_facts import journal_facts, report_dict
from .halt_options import ACCEPT_POLL_S, KERNEL_ISAS, THREAD_IDS
from .kernel import KernelLayoutError, ProcessNotFound
from .process import Process, ProcessExited
from ..raw_dap import DapError
from .watch import WatchTable


@dataclass
class _Gdb:
    """gdb のサーバの事実（gdb_status が返す）。"""
    port: int
    pid: int
    comm: str
    pgd: int
    threads: int
    recovered: dict
    stop_for_attach: dict | None
    state: str = "listening"              # listening（接続待ち）/ serving（gdb がつないだ）/ ended
    ended_by: str | None = None           # detach（gdb の D・k）/ disconnect（gdb が切れた）/ stop（gdb.stop・close）/ error
    error: str | None = None
    cleanup_errors: list = dataclasses.field(default_factory=list)
    # 後始末が DAP の転送の誤り（DapError）で失敗したとき、DP をつなぎ直してもう 1 度後始末したか（線の化けで DAP が丸ごと使えなく
    # なった後に、BKPT・HW の bp と停止デバッグを残さないため。最初の失敗は cleanup_errors に残す）と、その 2 度目の誤り
    cleanup_reconnected: bool = False
    cleanup_errors_after_reconnect: list = dataclasses.field(default_factory=list)
    attach_stop: dict | None = None       # gdb がつないだときの全コアの停止（Engine.attach）


class HaltGdbMixin:
    """gdb のサーバと持ち主の後始末（`HaltSession` の状態 `cores`・`options`・`_gdb` 等を使う）。"""

    def gdb_serve(self, *, pid: int, port: int, skew_limit_ns: int, thread_id: str, kernel_isa: str,
                  max_step_retries: int, bp_unmapped: str, log: Callable[[str], None] | None = None) -> dict:
        """プロセス pid の gdb RSP サーバを 127.0.0.1:port で開く（別のスレッド。手元の gdb が target remote でつなぐ）。
        CLI の attach と同じ順: 全コアを止めてプロセスを読み、前のデバッガが残した BKPT を戻し、全コアを走らせて gdb を待ち、
        つないだら全コアを止めて応える。gdb が去る（detach・切断）・gdb_stop・close で、BKPT と HW の bp/wp を外し、全コアを
        走らせ、停止デバッグを切る。"""
        self._not_busy("gdb.serve")
        self._check_engine_choices(thread_id, kernel_isa, bp_unmapped)
        srv = socket.socket()
        try:
            srv.bind(("127.0.0.1", port))        # 外に出さない: このポートはターゲットのメモリを読み書きできる
            srv.listen(1)
            srv.settimeout(ACCEPT_POLL_S)
            port = srv.getsockname()[1]          # 開いたポート（0 を渡したら OS が選んだもの）
            self._ensure_halting_debug()
            o, cores = self.options, self.cores
            journal = journal_facts(o.journal)

            def prepare():
                return self._prepare_engine(pid=pid, skew_limit_ns=skew_limit_ns, thread_id=thread_id,
                                            kernel_isa=kernel_isa, max_step_retries=max_step_retries,
                                            bp_unmapped=bp_unmapped)
            if all(cores.halted()):
                (proc, result, engine), stop = prepare(), None
                cores.resume_all()                            # gdb を待つあいだシステムを生かしておく（CLI と同じ）
            else:
                (proc, result, engine), stop, _ = self._reading(prepare)
        except BaseException:
            srv.close()
            raise
        self._engine, self._listen, self._conn = engine, srv, None
        self._stop.clear()
        self._gdb = _Gdb(port=port, pid=proc.tgid, comm=proc.comm, pgd=proc.pgd, threads=len(proc.threads),
                         recovered={"restored": result.restored, "stale": result.stale, "journal_before": journal},
                         stop_for_attach=stop)
        self._thread = threading.Thread(target=self._serve, args=(log or (lambda m: None),),
                                        name="fprobe-linux-halt-gdb", daemon=True)
        self._thread.start()
        return {**self.gdb_status(), "connect": "target remote 127.0.0.1:%d" % port}

    def _prepare_engine(self, *, pid, skew_limit_ns, thread_id, kernel_isa, max_step_retries, bp_unmapped):
        """（全コアが止まっている間に）プロセスを読み、前のデバッガが残した BKPT を journal で戻し、Engine を組む。
        gdb のサーバと linux.halt.debug の共通。-> (proc, recover の結果, engine)。"""
        o, cores = self.options, self.cores
        kernel = self._kernel()
        proc = Process(kernel, cores, pid, fmt=o.mmu_format)
        result = bpmod.recover(cores, o.journal)              # 前のデバッガが残した BKPT があれば戻す（無ければ何もしない）
        bps = BreakpointTable(cores, journal=o.journal, hw_slots=cores.hw_slots, unmapped=bp_unmapped)
        bps.attach(proc.tgid, proc.mem)
        engine = Engine(cores, kernel, proc, bps, skew_limit_ns=skew_limit_ns, thread_id=thread_id,
                        kernel_thumb=kernel_isa == "thumb", max_step_retries=max_step_retries,
                        watches=WatchTable(cores, hw_slots=cores.watch_slots))
        return proc, result, engine

    @staticmethod
    def _check_engine_choices(thread_id: str, kernel_isa: str, bp_unmapped: str) -> None:
        if thread_id not in THREAD_IDS:
            raise EngineError("thread_id must be one of %s" % (THREAD_IDS,))
        if kernel_isa not in KERNEL_ISAS:
            raise EngineError("kernel_isa must be one of %s" % (KERNEL_ISAS,))
        if bp_unmapped not in UNMAPPED_POLICIES:
            raise EngineError("bp_unmapped must be one of %s" % (UNMAPPED_POLICIES,))

    def _serve(self, log) -> None:
        from .rsp import Link, Server
        g, engine = self._gdb, self._engine
        try:
            conn = None
            while not self._stop.is_set():
                try:
                    conn, _ = self._listen.accept()
                    break
                except socket.timeout:
                    continue
            if conn is None:
                g.ended_by = "stop"
                return
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._conn = conn
            if self._stop.is_set():              # 止める要求と接続が行き違った
                g.ended_by = "stop"
                return
            log("gdb connected")
            g.state = "serving"
            stop = self._stop

            class StoppableLink(Link):
                """gdb_stop の要求で、パケットの待ち・実行中の ^C の確かめから ConnectionError で抜ける（ソケットの shutdown が
                別のスレッドの select を起こすことに頼らない。Windows では起きなかった）。"""
                def _fill(self, timeout):
                    if stop.is_set():
                        raise ConnectionError("gdb.stop")
                    return super()._fill(timeout)
            link = StoppableLink(conn, log)
            engine.interrupted = link.interrupted
            g.attach_stop = report_dict(engine.attach().report)
            log("all cores halted, serving")
            Server(engine, link, backend_errors=self.backend_errors, log=log).serve()
            g.ended_by = "detach"
        except (ConnectionError, OSError):
            g.ended_by = "stop" if self._stop.is_set() else "disconnect"
        except (ProcessNotFound, ProcessExited, EngineError, KernelLayoutError) as e:
            g.ended_by, g.error = "error", str(e)
        except BaseException as e:  # noqa: BLE001 - 後始末を必ず通る。理由は事実として残す
            g.ended_by, g.error = "error", "%s: %s" % (type(e).__name__, e)
        finally:
            done = self._cleanup_reconnecting(engine)
            g.cleanup_errors = done["cleanup_errors"]
            g.cleanup_reconnected = done["cleanup_reconnected"]
            g.cleanup_errors_after_reconnect = done["cleanup_errors_after_reconnect"]
            for s in (self._conn, self._listen):
                if s is not None:
                    with contextlib.suppress(OSError):
                        s.close()
            close_log = getattr(log, "close", None)
            if close_log is not None:
                with contextlib.suppress(OSError):
                    close_log()
            g.state = "ended"

    def _cleanup_reconnecting(self, engine) -> dict:
        """持ち主（gdb のサーバ・linux.halt.debug）の後始末。DAP の転送の誤りで失敗したら、DP をつなぎ直してもう 1 度だけ。"""
        failures = self._cleanup(engine)
        out = {"cleanup_errors": [str(e) for e in failures], "cleanup_reconnected": False,
               "cleanup_errors_after_reconnect": []}
        if any(isinstance(e, DapError) for e in failures) and hasattr(self._raw, "reconnect"):
            # 線の化けで DP の電源の要求が消える等、DAP が丸ごと使えないと後始末が全部失敗し、do_exit の HW の bp と停止デバッグが
            # 残ってターゲットが固まる（Pi 2B、2026-11-02）。後始末のためにだけ DP をつなぎ直し、もう 1 度だけ後始末する
            try:
                self.cores.reconnect()
                out["cleanup_reconnected"] = True
                out["cleanup_errors_after_reconnect"] = [str(e) for e in self._cleanup(engine)]
            except Exception as e:  # noqa: BLE001 - つなぎ直しの失敗も事実として返す
                out["cleanup_errors_after_reconnect"] = ["reconnect: %s: %s" % (type(e).__name__, e)]
        return out

    def _cleanup(self, engine) -> list:
        """gdb のサーバの後始末: BKPT・HW の bp/wp を外して全コアを走らせ（Engine.close）、停止デバッグを切る。失敗した例外の並び。"""
        failures = list(engine.close())
        try:
            self.cores.disable_halting_debug()                # 持ち主が終わるときは切る（BKPT が残ってもターゲットが固まらない）
            self.halting_debug = False
        except Exception as e:  # noqa: BLE001
            failures.append(e)
        return failures

    def gdb_status(self) -> dict | None:
        """gdb のサーバの事実（コアは読まない）。開いたことが無ければ None。last_stop は最後の全コアの停止（方式・上限・根拠と、
        skew_limit_ns との比較 skew_verdict）、stats は Engine の数え（停止の種類ごとの上限の最大・分からない・超えた回数等）。"""
        g = self._gdb
        if g is None:
            return None
        e = self._engine
        last = e.last_stop_result if e is not None else None
        stats = e.stats() if e is not None else None
        return {**dataclasses.asdict(g),
                "last_stop": None if last is None else {**report_dict(last.report), "skew_verdict": last.skew_verdict,
                                                        "skew_limit_ns": e.skew_limit_ns},
                "stats": None if stats is None else dataclasses.asdict(stats)}

    def gdb_stop(self) -> dict | None:
        """gdb のサーバを止める（接続を切る）。後始末（BKPT・HW の bp/wp を外し、全コアを走らせ、停止デバッグを切る）が終わる
        まで待つ（時間で打ち切らない）。止めた後の事実（gdb_status）を返す。動いていなければ、そのときの事実だけ。"""
        thread = self._thread
        if thread is None:
            return self.gdb_status()
        self._stop.set()
        conn = self._conn
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)       # 読みの select が起き、recv が空を返して Link が ConnectionError
        thread.join()
        return self.gdb_status()

    def wait_gdb(self, timeout_s: float | None = None) -> bool:
        """gdb のサーバが終わるまで待つ（CLI の attach）。終わったら True。"""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_s)
        return not thread.is_alive()
