"""Probe の道具: セミホスティング（BKPT 0xAB の処理）と、止まるまで実行して待つ run_until_halt。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。受信を貯める
背景の核（ObserveService: 貯め場・背景のスレッド・受け取り _received）の上に載る。機器に触るのは必ず _lock の中。

- 止まった理由が BKPT 0xAB なら処理して再開する（_run_until_halt の中と、target_resume の後の背景の見張り
  _sh_poll）。出力は ByteLog "semihosting" に入り、rtt_read(channel="semihosting") で読む。
- ファイル操作は file_root を API で明示したときだけ（仕様の x-mcp: false で MCP には出さない）。
- 背景の見張りは、動作のあるブレークポイント（条件が偽・無視する回数・ログ）の処理も同じ場所で行う
  （BreakpointService の _break_action・_break_hooks）。

仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md
"""

import time

from pyocd.core.target import Target

from .debug import PyocdAccess, Stop
from .probe_observe import SEMIHOSTING
from .probe_service import HostView
from .semihosting import FileAccess, SemihostingHost, SemihostingResult

__all__ = ["SemihostingService"]


class _PyocdSemihosting:
    """pyOCD のターゲットをセミホスティングの読み手（SemihostingTarget）に合わせる。"""

    def __init__(self, target):
        self.target = target
        self.access = PyocdAccess(target)

    def read_core_register(self, name: str) -> int:
        return self.target.read_core_register(name)

    def write_core_register(self, name: str, value: int) -> None:
        self.target.write_core_register(name, value)

    def read_memory(self, address: int, length: int) -> bytes:
        return self.access.read_memory(address, length)

    def write_memory(self, address: int, data: bytes) -> None:
        self.access.write_memory(address, data)

    def resume(self) -> None:
        self.target.resume()


class SemihostingService:
    """セミホスティングの設定・処理と、止まるまで実行して待つ道具（Probe のサービス）。"""

    API = ("semihosting_configure", "semihosting_status")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_console", "_run_until_halt", "_service", "_service_in_background", "_sh_exit", "_sh_poll", "_sh_watch",
              "_stdin", "_stdin_write", "_watch_stops", "_with_fault")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_actions", "_break_action", "_break_hooks", "_debug", "_ensure_poller", "_lock", "_observe", "_received",
             "_record_host", "_stop_dict", "dbg", "target", "target_fault")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._sh_enabled = True
        self._sh_files = FileAccess()
        self._sh_cmdline = ""
        self._sh_host: SemihostingHost | None = None
        self._sh_watch = False                                     # 再開した後、背景で止まるのを見張る（BKPT 0xAB・動作のあるブレークポイント）
        self._sh_requests = 0
        self._sh_exit: dict | None = None
        self._sh_stdin = bytearray()

    # --- 設定・状態 --------------------------------------------------------------------------

    def semihosting_configure(self, enabled: bool = True, file_root: str | None = None, writable: bool = False,
                              cmdline: str = "") -> dict:
        """セミホスティング（BKPT 0xAB）の処理の設定。既定で有効（出力は rtt_read の channel "semihosting"）。
        ファイル操作は file_root を指定したときだけ、その下に限って許す（writable で書き込みも）。file_root を省いて
        呼ぶとファイル操作は無効に戻る（AI の道具からは file_root を指定できないので、AI が設定し直すと無効になる）。"""
        with self._host._lock:
            self._sh_enabled = enabled
            self._sh_files = FileAccess(file_root, writable)
            self._sh_cmdline = cmdline
            if self._sh_host is not None:
                self._sh_host.close()
                self._sh_host = None
            return self.semihosting_status()

    def semihosting_status(self) -> dict:
        """セミホスティングの設定・処理した要求の数・最後の終了（SYS_EXIT の終了コード）。"""
        root = self._sh_files.root
        return {"enabled": self._sh_enabled, "file_root": str(root) if root else None,
                "writable": self._sh_files.writable, "requests": self._sh_requests, "exit": self._sh_exit,
                "watching": self._sh_watch}

    # --- 要求の処理 --------------------------------------------------------------------------

    def _semihosting(self) -> SemihostingHost:
        if self._sh_host is None:
            self._sh_host = SemihostingHost(console=self._console, stdin=self._stdin, files=self._sh_files,
                                            cmdline=self._sh_cmdline)
        return self._sh_host

    def _console(self, stream: str, data: bytes) -> None:
        self._host._received(SEMIHOSTING, data, time.monotonic())

    def _stdin(self, n: int) -> bytes:
        with self._host._observe:
            data = bytes(self._sh_stdin[:n])
            del self._sh_stdin[:n]
            return data

    def _stdin_write(self, data: bytes) -> None:
        """セミホスティングの標準入力に data を足す（rtt_write の channel "semihosting"）。"""
        with self._host._observe:
            self._sh_stdin += data

    def _service(self) -> SemihostingResult | None:
        """止まった位置が BKPT 0xAB なら要求を処理する（_lock の中で）。処理しなかったら None。"""
        if not self._sh_enabled:
            return None
        result = self._semihosting().handle(_PyocdSemihosting(self._host.target))
        if not result.handled:
            return None
        self._sh_requests += 1
        if result.exited:
            self._sh_exit = {"code": result.exit_code, "reason": result.exit_reason}
            self._host._record_host(SEMIHOSTING, "exit", time.monotonic(), a=result.exit_code or 0)
        return result

    # --- 再開した後の見張り --------------------------------------------------------------------

    def _sh_poll(self) -> None:
        """背景のスレッドの 1 周期の見張り（ObserveService._poll_once から、_lock の中で）。切断されていれば止める。"""
        if self._host._debug is None and self._sh_watch:           # 切断された
            self._sh_watch = False
        if self._sh_watch:
            self._service_in_background()

    def _service_in_background(self) -> None:
        """再開した後の見張り（_lock の中で）: BKPT 0xAB（セミホスティング）か、止まらない動作のブレークポイント
        （条件が偽・無視する回数・ログ）で止まっていたら処理して再開する。ほかの理由の停止なら見張りを終える。"""
        target = self._host.target
        if target.get_state() != Target.State.HALTED:
            return
        if target.get_halt_reason() == Target.HaltReason.BREAKPOINT:
            result = self._service()
            if result is not None and not result.exited:
                target.resume()                                    # BKPT 0xAB は PC を進めてある
                return
            if result is None and self._host._break_action():
                self._host.dbg.resume()                                  # ブレークポイントを 1 命令踏み越えて続ける
                return
        self._sh_watch = False

    def _watch_stops(self, on: bool) -> None:
        """target_resume 等の後に on、止める操作で off。"""
        self._sh_watch = on and (self._sh_enabled or bool(self._host._actions) or bool(self._host._break_hooks))
        if self._sh_watch:
            self._host._ensure_poller()

    # --- 止まるまで実行 ----------------------------------------------------------------------

    def _run_until_halt(self, timeout_s: float) -> dict:
        """実行して止まるまで待つ。BKPT 0xAB（セミホスティング）なら処理して続ける。SYS_EXIT なら reason = "exit"。
        待つ間は機器のロックを放す（背景の RTT が読み続ける）。"""
        started = time.monotonic()
        deadline = started + timeout_s
        with self._host._lock:
            dbg = self._host.dbg
            self._sh_watch = False                                 # ここで処理する（背景と取り合わない）
        while True:
            stop = dbg.run_until_halt(max(0.0, deadline - time.monotonic()), lock=self._host._lock)
            with self._host._lock:
                result = self._service() if stop.reason == "breakpoint" else None
                if result is None and stop.reason == "breakpoint" and self._host._break_action():
                    continue                                       # 止まらない動作: 踏み越えて続ける（次の run で）
            if result is None or result.exited:
                stop = Stop(stop.reason, stop.pc, stop.where, round(time.monotonic() - started, 4))
                out = self._host._stop_dict(stop)
                if result is not None:
                    out.update(reason="exit", exit_code=result.exit_code)
                elif stop.reason != "timeout":
                    self._with_fault(out)
                return out

    def _with_fault(self, out: dict) -> dict:
        """故障のハンドラ（・ベクタキャッチ）で止まっていれば、原因の要約（target.fault の）を out に添える。"""
        try:
            with self._host._lock:
                report = self._host.target_fault()
        except Exception:                                          # 故障レジスタが読めない: 要約を付けないだけ
            return out
        if report["faulted"]:
            out["fault"] = {"causes": [c["bit"] for c in report["causes"]],
                            "at": (report.get("frame") or {}).get("pc")}
        return out
