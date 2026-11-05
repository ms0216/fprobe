"""JTAG で全コアを止めて Linux のアプリをデバッグする操作の中身（CLI `python -m fprobe.linux_halt` とデーモンの API `linux.halt.*` が
同じものを呼ぶ。二重の実装をしない）。設計: docs/superpowers/specs/2026-11-01-linux-halt-api-ui-design.md。

  * `HaltOptions`: つなぐ設定（CLI の共通の引数と API の `linux.halt.open` の引数が一対一）。**既定の値は持たない**。`check()` が
    組の欠け・組み合わせの誤りを、CLI と API で同じ文で断る。
  * `HaltSession`: 開いたバックエンド（コア）の上の操作。status・halt_all・resume_all・processes・threads・recover・rescue・gdb_serve
    （RSP サーバを別のスレッドで開き、手元の gdb がつなぐ）・gdb_status・gdb_stop・close。応答は事実の dict（停止の報告は
    `StopReport` の method・cores_halted・skew_upper_bound_ns・basis をそのまま）。
  * コアの操作は 1 回ずつ `lock`（デーモンでは Probe の機器の錠）を取る。gdb のスレッドとデーモンの他の操作が、同じ USB の口を
    同時に使わない（待ちは 1 回 20 ms まで: `Engine._wait` の `wait_event(0.02)`）。
  * `debug_*`（API の linux.halt.debug.*）: gdb を通さずに AI が止めて調べる持ち主（`HaltDebugger`。
    設計: docs/superpowers/specs/2026-11-03-linux-halt-ai-tools-design.md）。gdb のサーバと同じく、持ち主は 1 つ。
  * gdb がつながっている間（listening / serving）と debug が開いている間、コアを動かす・読む操作は断る（all-stop の Engine の
    状態と、FPGA の停止の見張りを乱さない）。gdb.stop が接続を切り、Engine の後始末（BKPT・HW の bp/wp を外し、全コアを走らせる）と停止デバッグの
    切断が終わるまで待つ（時間で打ち切らない: BKPT を残さないため）。

部品: 設定 `halt_options`・応答の事実 `halt_facts`・バックエンドを開く `halt_backend`・gdb のサーバ `halt_gdb`・debug_* `halt_debug`。
このファイルは `HaltSession`（開閉・状態・読む操作・rescue）と、それらの名前の再公開。
"""
from __future__ import annotations

import contextlib
import socket
import threading

from . import bp as bpmod
from .debugger import HaltDebugger
from .engine import Engine, EngineError
from .halt_backend import _Locked, open_backend
from .halt_debug import HaltDebugMixin
from .halt_facts import FileLog, journal_facts, process_rows, report_dict
from .halt_gdb import HaltGdbMixin, _Gdb
from .halt_options import (ACCEPT_POLL_S, BACKENDS, CTI_BOARDS, KERNEL_ISAS, MMU_FORMATS, THREAD_IDS, UNMEASURED, HaltOptions,
                           OptionsError)
from .kernel import Kernel, load_layout_json
from .process import Process

# 部品に分けた名前の再公開（旧来の `fprobe.linux_halt.service.<名前>` を保つ）
__all__ = ["ACCEPT_POLL_S", "BACKENDS", "CTI_BOARDS", "KERNEL_ISAS", "MMU_FORMATS", "THREAD_IDS", "UNMEASURED", "Busy", "FileLog",
           "HaltOptions", "HaltSession", "OptionsError", "journal_facts", "open_backend", "process_rows", "report_dict"]


class Busy(RuntimeError):
    """gdb の接続か linux.halt.debug がコアを使っている間の、コアを動かす・読む操作。"""


class HaltSession(HaltGdbMixin, HaltDebugMixin):
    """開いたバックエンドの上の操作（CLI と API の中身）。"""

    def __init__(self, options: HaltOptions, layout, cores, close, backend_errors: tuple, info: dict, lock=None):
        self.options, self.layout, self.info = options, layout, info
        self._raw = cores
        self._lock = lock if lock is not None else contextlib.nullcontext()
        self._device_lock = lock                               # HaltDebugger の鍵（機器の錠と 1 つにする）
        self.cores = _Locked(cores, self._lock)
        self._close_backend, self.backend_errors = close, backend_errors
        self.halting_debug = True             # ArmACores.open が全コアの停止デバッグ（HDBGen）を立てる
        self._gdb: _Gdb | None = None
        self._engine: Engine | None = None
        self._thread: threading.Thread | None = None
        self._listen: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self._debug: HaltDebugger | None = None
        self.closed = False

    @classmethod
    def open(cls, options: HaltOptions, *, dap_link=None, device=None, lock=None) -> "HaltSession":
        options.check()
        layout = load_layout_json(options.offsets, page_offset=options.page_offset, phys_offset=options.phys_offset,
                                  linear_end=options.linear_map_end)
        if lock is None:
            cores, close, errors, info = open_backend(options, layout, dap_link=dap_link, device=device)
        else:
            with lock:
                cores, close, errors, info = open_backend(options, layout, dap_link=dap_link, device=device)
        return cls(options, layout, cores, close, errors, info, lock)

    # ------------------------------------------------------------ 共通
    @property
    def serving(self) -> bool:
        """gdb のサーバが動いている（接続待ちか、gdb がつないでいる）。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def debugging(self) -> bool:
        """linux.halt.debug が開いている（attach してから close まで）。"""
        return self._debug is not None and not self._debug.closed

    def _not_busy(self, what: str) -> None:
        if self.closed:
            raise EngineError("the session is closed")
        if self.serving:
            g = self._gdb
            raise Busy("%s: the gdb server (127.0.0.1:%d, pid %d, %s) is using the cores; stop it first (gdb.stop)"
                       % (what, g.port, g.pid, g.state))
        if self.debugging:
            raise Busy("%s: linux.halt.debug (pid %d) is using the cores; close it first (linux.halt.debug.close)"
                       % (what, self._debug.proc.tgid))

    def _ensure_halting_debug(self) -> None:
        """rescue・gdb の終わりで切った停止デバッグを、コアを止めて読む前に立て直す（ArmACores.open と同じ状態に）。"""
        if not self.halting_debug:
            enable = getattr(self.cores, "enable_halting_debug", None)
            if enable is not None:
                enable()
            self.halting_debug = True

    def _kernel(self) -> Kernel:
        k = Kernel(self.cores.read_kernel, self.layout, max_tasks=self.options.max_tasks,
                   max_threads=self.options.max_threads)
        k.verify()
        return k

    def _reading(self, fn):
        """全コアが止まっていれば止め直さずに fn を実行し、走らせない。そうでなければ全コアを止めて fn を実行し、全コアを走らせる
        （CLI の ps・recover と同じ）。-> (fn の結果, stop = 止めたときの StopReport か None, resumed)。"""
        self._ensure_halting_debug()
        if all(self.cores.halted()):
            return fn(), None, False
        report = self.cores.halt_all()
        if len(report.cores_halted) != self.cores.n_cores:
            self.cores.resume_all()
            raise EngineError("not every core halted (%s); resumed" % list(report.cores_halted))
        try:
            return fn(), report_dict(report), True
        finally:
            self.cores.resume_all()

    # ------------------------------------------------------------ 操作
    def status(self) -> dict:
        """読むだけ（コアを止めない）: 開いた事実・設定・ジャーナル・各コアが走っているか・残っている HW の bp/wp・gdb のサーバ。
        gdb のサーバが動いている間はコアを読まない（cores = None と、その理由）。"""
        out = {**self.info, "options": self.options.as_dict(), "halting_debug": self.halting_debug,
               "journal": journal_facts(self.options.journal), "gdb": self.gdb_status(),
               "debug": None if self._debug is None else self._debug_summary()}
        if self.serving:
            out["cores"], out["cores_unread"] = None, "the gdb server is using the cores (they are not read meanwhile)"
        elif not hasattr(self.cores, "programmed"):
            out["cores"], out["cores_unread"] = None, "the core status is read through the fprobe backend only"
        else:
            p = self.cores.programmed()
            out["cores"] = [{"core": n, "running": running, "breakpoints": p["breakpoints"].get(n, []),
                             "watchpoints": p["watchpoints"].get(n, [])} for n, running in enumerate(p["running"])]
        return out

    def halt_all(self) -> dict:
        """全コアを止める（止めたまま。resume_all で走らせる）。停止の報告をそのまま返す。"""
        self._not_busy("halt_all")
        self._ensure_halting_debug()
        return {"stop": report_dict(self.cores.halt_all())}

    def resume_all(self) -> dict:
        self._not_busy("resume_all")
        self.cores.resume_all()
        return {"resumed": list(range(self.cores.n_cores))}

    def processes(self) -> dict:
        """プロセスの一覧（スレッドグループのリーダー。idle を除く）。並びはカーネルのタスク一覧の順。has_mm = false はカーネルスレッド。"""
        self._not_busy("processes")
        rows, stop, resumed = self._reading(lambda: process_rows(self._kernel()))
        return {"processes": rows, "stop": stop, "resumed": resumed}

    def threads(self, pid: int) -> dict:
        """プロセス pid のスレッド（カーネルのスレッド一覧の順）。"""
        self._not_busy("threads")

        def read():
            proc = Process(self._kernel(), self.cores, pid, fmt=self.options.mmu_format)
            return {"pid": proc.tgid, "comm": proc.comm, "pgd": proc.pgd,
                    "threads": [{"tid": t.pid, "comm": t.comm, "task": t.addr} for t in proc.threads]}
        out, stop, resumed = self._reading(read)
        return {**out, "stop": stop, "resumed": resumed}

    def recover(self) -> dict:
        """死んだデバッガが残した BKPT を、ジャーナルの元の命令で戻す（今も BKPT がある所だけ。stale = 戻さなかった数）。"""
        self._not_busy("recover")
        journal = journal_facts(self.options.journal)
        result, stop, resumed = self._reading(lambda: bpmod.recover(self.cores, self.options.journal))
        return {"restored": result.restored, "stale": result.stale, "journal_before": journal, "stop": stop,
                "resumed": resumed}

    def rescue(self) -> dict:
        """強制終了されたデバッガがコアを止めたまま残したとき: ターゲットのメモリは触らず、bp/wp を消し、停止デバッグを切り、
        止まったコアを再開する（CTI を使う設定なら、先に CTI の停止要求を解く）。BKPT は recover が戻す。"""
        self._not_busy("rescue")
        if not hasattr(self.cores, "rescue"):
            raise EngineError("rescue is implemented for the fprobe backend only")
        released = False
        if self.options.cti:
            self.cores.release_cross_halt()
            released = True
        resumed = self.cores.rescue()
        self.halting_debug = False
        return {"resumed": list(resumed), "cti_released": released, "breakpoints_cleared": True,
                "watchpoints_cleared": True, "halting_debug": False,
                "journal": journal_facts(self.options.journal)}

    # ------------------------------------------------------------ gdb

    def close(self) -> dict:
        """閉じる: gdb のサーバが動いていれば止めて後始末を待ち、バックエンドを閉じる。コアの走り・止まりは変えない（halt_all で
        止めたままのコアは止まったまま）。閉じる前に読めたコアの状態を返す。"""
        if self.closed:
            return {"closed": True, "gdb": self.gdb_status(), "halted": None}
        gdb = self.gdb_stop()
        debug = self._debug.close() if self.debugging else None
        halted = None
        try:
            halted = list(self.cores.halted())
        except Exception:  # noqa: BLE001 - 読めなくても閉じる（読めなかったことは None で）
            pass
        try:
            with self._lock:
                self._close_backend()
        finally:
            self.closed = True
        return {"closed": True, "gdb": gdb, "debug": debug, "halted": halted}
