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
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable

from . import bp as bpmod
from .bp import UNMAPPED_POLICIES, BreakpointTable
from .debugger import HaltDebugger
from .engine import Engine, EngineError
from .image import ElfImage
from .kernel import Kernel, KernelLayoutError, ProcessNotFound, load_layout_json
from .process import Process, ProcessExited
from ..raw_dap import DapError
from .watch import WatchTable

UNMEASURED = "unmeasured"                 # 上限を測っていない（既定の値ではなく、使う側の明示の指定）
BACKENDS = ("fprobe", "openocd")
MMU_FORMATS = ("short", "lpae")
CTI_BOARDS = ("zynq7000",)
THREAD_IDS = ("stack", "contextidr")
KERNEL_ISAS = ("arm", "thumb")
ACCEPT_POLL_S = 0.25                      # gdb の接続を待つ間、止める要求に気づくまでの間隔（rsp.Link.POLL と同じ）


class OptionsError(ValueError):
    """設定の組が欠けている・組み合わせが誤り（CLI は argparse の誤りに、API は E_DEVICE の誤りの文にする）。"""


class Busy(RuntimeError):
    """gdb の接続か linux.halt.debug がコアを使っている間の、コアを動かす・読む操作。"""


def _ocd_boards() -> dict:
    from fprobe.plugins.openocd_boards import BOARDS
    return BOARDS


@dataclass(frozen=True)
class HaltOptions:
    """つなぐ設定。CLI の共通の引数（`--backend` 等）と API の `linux.halt.open` の引数が同じ名前で一対一。None は「渡していない」。
    `fpga_halt_watch_bound_ns`・`cti_event_bound_ns` は ns の整数か "unmeasured"（測っていない。None = 渡していない とは別）。"""
    backend: str
    jtag_khz: int
    offsets: str
    page_offset: int
    phys_offset: int
    linear_map_end: int
    max_tasks: int
    max_threads: int
    mmu_format: str
    journal: str
    fpga_request_times: bool
    part: str | None = None
    fpga_halt_watch_bound_ns: int | str | None = None
    fpga_halt_watch_poll_s: float | None = None
    cti_board: str | None = None
    cti_request_bound_ns: int | None = None
    cti_event_bound_ns: int | str | None = None
    cti_hw_wait_s: float | None = None
    cti_ack_polls: int | None = None
    ocd_board: str | None = None
    ocd_port: int | None = None
    ocd_timeout_s: float | None = None
    hw_slots: int | None = None
    watch_slots: int | None = None

    @property
    def fpga_halt_watch(self) -> bool:
        return self.fpga_halt_watch_bound_ns is not None and self.fpga_halt_watch_poll_s is not None

    @property
    def cti(self) -> bool:
        return all(getattr(self, n) is not None for n in _CTI_FIELDS)

    def check(self) -> "HaltOptions":
        """組の欠け・組み合わせの誤りを断る（OptionsError）。CLI と API の同じ文。"""
        if self.backend not in BACKENDS:
            raise OptionsError("backend must be one of %s" % (BACKENDS,))
        if not 1 <= self.jtag_khz <= 100000:
            raise OptionsError("--jtag-khz must be 1..100000")
        if self.mmu_format not in MMU_FORMATS:
            raise OptionsError("mmu_format must be one of %s" % (MMU_FORMATS,))
        if self.backend == "openocd":
            for flag in ("ocd_board", "ocd_port", "ocd_timeout_s", "hw_slots", "watch_slots"):
                if getattr(self, flag) is None:
                    raise OptionsError("--backend openocd needs --%s" % flag.replace("_", "-"))
            if self.ocd_board not in _ocd_boards():
                raise OptionsError("--ocd-board must be one of %s" % sorted(_ocd_boards()))
        elif self.part is None:
            raise OptionsError("--backend fprobe needs --part")
        for name in ("fpga_halt_watch_bound_ns", "cti_event_bound_ns"):
            v = getattr(self, name)
            if v is not None and v != UNMEASURED and not (isinstance(v, int) and v >= 0):
                raise OptionsError("--%s is a number of ns or %s" % (name.replace("_", "-"), UNMEASURED))
        watch = (self.fpga_halt_watch_bound_ns is not None, self.fpga_halt_watch_poll_s is not None)
        if any(watch) and not all(watch):
            raise OptionsError("the FPGA halt watch needs both --fpga-halt-watch-bound-ns and --fpga-halt-watch-poll-s")
        if any(watch) and self.backend != "fprobe":
            raise OptionsError("the FPGA halt watch is a feature of the fprobe backend")
        cti = [getattr(self, n) is not None for n in _CTI_FIELDS]
        if any(cti) and not all(cti):
            raise OptionsError("the CTI needs every one of --cti-board --cti-request-bound-ns --cti-event-bound-ns "
                               "--cti-hw-wait-s --cti-ack-polls")
        if any(cti) and self.backend != "fprobe":
            raise OptionsError("--cti-* belong to the fprobe backend")
        if any(cti) and self.cti_board not in CTI_BOARDS:
            raise OptionsError("--cti-board must be one of %s" % (CTI_BOARDS,))
        if any(cti) and self.fpga_halt_watch:
            raise OptionsError("the CTI and the FPGA halt watch are not used together")
        if self.fpga_request_times and self.backend != "fprobe":
            raise OptionsError("--fpga-request-times is a feature of the fprobe backend")
        if self.fpga_request_times and self.cti:
            raise OptionsError("--fpga-request-times is for the stop without a CTI (the CTI halts in hardware)")
        return self

    def as_dict(self) -> dict:
        """渡した設定（渡していないものは除く）。応答に、そのまま返す。"""
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


_CTI_FIELDS = ("cti_board", "cti_request_bound_ns", "cti_event_bound_ns", "cti_hw_wait_s", "cti_ack_polls")


def _ns(bound):
    """"unmeasured" → None（バックエンドの「分からない」）。"""
    return None if bound == UNMEASURED else bound


def report_dict(report) -> dict:
    """StopReport をそのまま（method・cores_halted・skew_upper_bound_ns・basis・assumptions・bound_without_assumptions_ns）。"""
    return {"method": report.method, "cores_halted": list(report.cores_halted),
            "skew_upper_bound_ns": report.skew_upper_bound_ns, "basis": report.basis,
            "assumptions": list(report.assumptions),
            "bound_without_assumptions_ns": report.bound_without_assumptions_ns}


def process_rows(kernel: Kernel) -> list[dict]:
    """スレッドグループのリーダー（idle は除く）。並びはカーネルのタスク一覧の順。has_mm = false はカーネルスレッド。"""
    return [{"tgid": t.tgid, "comm": t.comm, "has_mm": bool(t.mm)} for t in kernel.leaders() if t.pid]


def journal_facts(path: str) -> dict:
    """ブレークポイントのジャーナルのファイルの事実（読むだけ。書き戻すのは recover）。entries は、死んだデバッガが BKPT を
    書いたかもしれない物理番地（今もメモリに BKPT があるかは読まない）。"""
    if not os.path.exists(path):
        return {"path": path, "exists": False, "entries": []}
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        return {"path": path, "exists": True,
                "entries": [{"pa": e["pa"], "width": e["width"], "owners": e.get("owners", [])} for e in entries]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"path": path, "exists": True, "entries": None, "error": "%s: %s" % (type(e).__name__, e)}


class FileLog:
    """gdb のパケットのログのファイル（gdb のサーバが終わると閉じる）。"""

    def __init__(self, path: str):
        self._f = open(path, "w", buffering=1, encoding="utf-8")

    def __call__(self, message: str) -> None:
        print(message, file=self._f, flush=True)

    def close(self) -> None:
        self._f.close()


class _Locked:
    """コアの口の各メソッドを、1 回ずつ lock を取って呼ぶ（属性はそのまま）。gdb のスレッドと他の操作を同じ口で混ぜない。"""

    def __init__(self, cores, lock):
        object.__setattr__(self, "_cores", cores)
        object.__setattr__(self, "_lock", lock)

    def __getattr__(self, name):
        value = getattr(self._cores, name)
        if not callable(value):
            return value
        lock = self._lock

        def locked(*args, **kwargs):
            with lock:
                return value(*args, **kwargs)
        return locked

    def __setattr__(self, name, value):
        setattr(self._cores, name, value)


def open_backend(o: HaltOptions, layout, *, dap_link: Callable[[], Any] | None, device) -> tuple:
    """o のバックエンドを開く。dap_link: fprobe の DAP の接続口を開く関数（Session.dap_access）、device: fprobe の機器
    （FPGA の停止の見張り・イベント記録）。-> (cores, close, backend_errors, info)。info は開いた事実。"""
    off, phys, end = layout.o["page_offset"], layout.o["phys_offset"], layout.o["linear_end"]
    if o.backend == "openocd":
        from .backend_openocd import OcdError, OpenOcdCores
        cores = OpenOcdCores.open("127.0.0.1", o.ocd_port, board=_ocd_boards()[o.ocd_board], jtag_khz=o.jtag_khz,
                                  page_offset=off, phys_offset=phys, linear_end=end, hw_slots=o.hw_slots,
                                  watch_slots=o.watch_slots, timeout=o.ocd_timeout_s)
        try:
            cores.setup()
        except BaseException:
            cores.o.close()
            raise
        info = {"backend": "openocd", "n_cores": cores.n_cores, "hw_slots": cores.hw_slots,
                "watch_slots": cores.watch_slots, "actual_jtag_khz": cores.actual_jtag_khz}

        def close():
            try:
                cores.release()
            finally:
                cores.o.close()
        return cores, close, (OcdError,), info
    from fprobe.arm_a_target import arm_a_part
    from fprobe.arm_debug import ArmDebugError
    from .backend_arma import ArmACores
    cores = ArmACores.open(dap_link(), arm_a_part(o.part), frequency_hz=o.jtag_khz * 1000,
                           page_offset=off, phys_offset=phys, linear_end=end)
    info = {"backend": "fprobe", "part": o.part, "n_cores": cores.n_cores, "hw_slots": cores.hw_slots,
            "watch_slots": cores.watch_slots, "debug_ap": cores.debug_ap, "discovery": cores.discovery,
            "cti": None, "fpga_halt_watch": None, "fpga_request_times": None}
    try:
        if o.cti:
            from fprobe.plugins import zynq_cti
            from .cti import ApbIo
            board = zynq_cti.ZYNQ7000
            apb_offset = board.apb_offset_for(cores.discovery)
            cross = zynq_cti.cross_halt(board, ApbIo(cores._dap, cores.debug_ap), apb_offset=apb_offset,
                                        ack_timeout_polls=o.cti_ack_polls)
            cores.use_cross_halt(cross, request_skew_upper_bound_ns=o.cti_request_bound_ns,
                                 event_skew_upper_bound_ns=_ns(o.cti_event_bound_ns), event_trigin=board.dbgack_in,
                                 hardware_halt_wait_s=o.cti_hw_wait_s)
            info["cti"] = {"board": o.cti_board, "apb_offset": apb_offset, "event_trigin": board.dbgack_in}
        if o.fpga_halt_watch:
            from fprobe.halt_watch import HaltWatch
            cores.use_fpga_halt_watch(HaltWatch(device), event_skew_upper_bound_ns=_ns(o.fpga_halt_watch_bound_ns),
                                      poll_s=o.fpga_halt_watch_poll_s)
            info["fpga_halt_watch"] = {"bound_ns": o.fpga_halt_watch_bound_ns, "poll_s": o.fpga_halt_watch_poll_s}
        if o.fpga_request_times:
            from .request_times import EventRequestTimes
            times = EventRequestTimes.open(device)
            cores.use_fpga_request_times(times)
            # 記録の無い構成: 上限はどれも PC の時計で、停止の報告の根拠の文がそう言う
            info["fpga_request_times"] = {"event_log": times.log is not None}
    except BaseException:
        cores.close()
        raise
    return cores, cores.close, (ArmDebugError,), info


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


class HaltSession:
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

    # ------------------------------------------------------------ linux.halt.debug
    def _debugger(self) -> HaltDebugger:
        if self.closed:
            raise EngineError("the session is closed")
        if self._debug is None:
            raise EngineError("linux.halt.debug is not attached (linux.halt.debug.attach first)")
        return self._debug

    def _debug_summary(self) -> dict:
        d = self._debug
        return {"pid": d.proc.tgid, "comm": d.proc.comm, "closed": d.closed, "running": d.engine.running,
                "breakpoints": len(d.breakpoints)}

    def debug_attach(self, *, pid: int, elf: str, skew_limit_ns: int, thread_id: str, kernel_isa: str,
                     max_step_retries: int, bp_unmapped: str, max_halt_s: float) -> dict:
        """プロセス pid を gdb を通さずに止めて調べる持ち主を開く: 全コアを止めてプロセスを読み、前のデバッガが残した BKPT を
        戻し、ELF（PC の上のパス）の読み込み位置を補助ベクタから求め、全コアを**止めたまま**返す。max_halt_s 秒、呼び出しが
        無いまま止めたままなら、見張りが全コアを走らせる。"""
        self._not_busy("linux.halt.debug.attach")
        self._check_engine_choices(thread_id, kernel_isa, bp_unmapped)
        if not (isinstance(max_halt_s, (int, float)) and max_halt_s > 0):
            raise EngineError("max_halt_s must be a number of seconds > 0 (got %r)" % (max_halt_s,))
        if not (isinstance(skew_limit_ns, int) and skew_limit_ns >= 0):
            raise EngineError("skew_limit_ns must be an integer >= 0 (got %r)" % (skew_limit_ns,))
        if not (isinstance(max_step_retries, int) and max_step_retries >= 1):
            raise EngineError("max_step_retries must be an integer >= 1 (got %r)" % (max_step_retries,))
        try:
            image = ElfImage(elf)                              # コアに触る前に ELF を確かめる
        except (OSError, ValueError) as e:
            raise EngineError("elf %r: %s" % (elf, e)) from None
        self._ensure_halting_debug()
        journal = journal_facts(self.options.journal)

        def prepare():
            return self._prepare_engine(pid=pid, skew_limit_ns=skew_limit_ns, thread_id=thread_id,
                                        kernel_isa=kernel_isa, max_step_retries=max_step_retries,
                                        bp_unmapped=bp_unmapped)
        if all(self.cores.halted()):
            (proc, result, engine), stop = prepare(), None
        else:
            (proc, result, engine), stop, _ = self._reading(prepare)
        try:
            attach = engine.attach()                          # 全コアを止める（止めたまま返す）
            debugger = HaltDebugger(engine, image, skew_limit_ns=skew_limit_ns, max_halt_s=float(max_halt_s),
                                    lock=self._device_lock,
                                    attach_stop=attach, cleanup=self._cleanup_reconnecting)
        except BaseException:
            self._cleanup_reconnecting(engine)                # 途中で失敗: BKPT・HW の bp を外して全コアを走らせる
            raise
        self._debug = debugger
        return {**debugger.facts(), "threads": debugger.threads_brief(),
                "recovered": {"restored": result.restored, "stale": result.stale, "journal_before": journal},
                "stop_for_reading": stop}

    def debug_status(self) -> dict:
        return self._debugger().facts()

    def debug_break_insert(self, *, location: str, kind: str) -> dict:
        return self._debugger().break_insert(location, kind)

    def debug_break_remove(self, *, id: int) -> dict:  # noqa: A002 - API の引数名
        return self._debugger().break_remove(id)

    def debug_run(self, *, wait_s: float) -> dict:
        return self._debugger().run(wait_s)

    def debug_halt(self) -> dict:
        return self._debugger().halt()

    def debug_threads(self, *, variables: bool, expressions: list, max_elements: int) -> dict:
        return self._debugger().threads(variables, list(expressions), max_elements)

    def debug_read(self, *, expressions: list, tid: int, frame: int, max_elements: int) -> dict:
        return self._debugger().read(list(expressions), tid, frame, max_elements)

    def debug_memory(self, *, address: int, length: int) -> dict:
        return self._debugger().memory(address, length)

    def debug_step(self, *, tid: int, mode: str, max_instructions: int, timeout_s: float) -> dict:
        return self._debugger().step(tid, mode, max_instructions, timeout_s)

    def debug_select(self, *, tid: int) -> dict:
        return self._debugger().select_thread(tid)

    def debug_close(self) -> dict:
        return self._debugger().close()
