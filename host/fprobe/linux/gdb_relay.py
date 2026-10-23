"""ターゲットの gdbserver（標準入出力）と、PC の gdb の接続を中継する（段階 3。RSP は解さず、バイト列のまま）。

PC の 127.0.0.1 の port で接続を 1 つ待ち、つながったら SSH の中で remote_cmd（gdbserver --once --attach - PID 等）を起動する。
どちらかが閉じたら、もう片方も閉じる。PC のポートは 127.0.0.1 だけで開く（ほかの機器から見えない）。"""
from __future__ import annotations

import socket
import threading

from .ssh import SshTarget

STDERR_TAIL = 2000

EMPTY_STATUS = {"port": None, "connected": False, "target_running": False, "exit_code": None, "bytes_to_target": 0,
                "bytes_from_target": 0, "closed_by": None, "error": None, "stderr_tail": ""}


class GdbRelay:
    def __init__(self, ssh: SshTarget, remote_cmd: str, port: int, stdin_prefix: bytes = b""):
        self.ssh, self.remote_cmd, self.port = ssh, remote_cmd, port
        self.stdin_prefix = stdin_prefix                   # sudo -S のパスワードの行（gdbserver の前に sudo が読む）
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listen.bind(("127.0.0.1", port))
            self._listen.listen(1)
        except OSError as e:
            self._listen.close()
            raise RuntimeError(f"ポート {port} が使えない（127.0.0.1）: {e}") from e
        self._lock = threading.Lock()
        self._conn: socket.socket | None = None
        self._proc = None
        self.bytes_to_target = self.bytes_from_target = 0
        self.closed_by: str | None = None
        self.error: str | None = None
        self._stderr = b""
        threading.Thread(target=self._run, daemon=True, name=f"gdb-relay-{port}").start()

    def _run(self) -> None:
        try:
            conn, _ = self._listen.accept()
        except OSError:
            return                                           # stop で閉じた
        self._listen.close()
        with self._lock:
            if self.closed_by is not None:                   # 受けた直後に stop された: gdbserver を起動しない
                conn.close()
                return
            self._conn = conn
            try:
                self._proc = self.ssh.open(self.remote_cmd)
                if self.stdin_prefix:
                    self._proc.stdin.write(self.stdin_prefix)
                    self._proc.stdin.flush()
                    self.stdin_prefix = b""                   # 書いた後は持たない
            except Exception as e:                           # noqa: BLE001  gdb を待たせない（閉じて事実を残す）
                self.error = f"gdbserver を起動できない: {type(e).__name__}: {e}"
        if self._proc is None:
            self._close("error")
            return
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._to_target, daemon=True).start()
        self._from_target()

    def _drain_stderr(self) -> None:
        for line in self._proc.stderr:
            self._stderr = (self._stderr + line)[-STDERR_TAIL:]

    def _close(self, by: str) -> None:
        with self._lock:
            if self.closed_by is None:
                self.closed_by = by
            conn, proc = self._conn, self._proc
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(5)
            except Exception:                                # noqa: BLE001  終わらなければ止める
                proc.kill()
                proc.wait()

    def _note(self, where: str, e: Exception) -> None:
        if self.closed_by is None:                           # 閉じている途中の誤りは誤りではない
            self.error = self.error or f"{where}: {e}"

    def _to_target(self) -> None:
        try:
            while data := self._conn.recv(4096):
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
                self.bytes_to_target += len(data)
        except ConnectionResetError:
            pass                                             # gdb の側が閉じた（誤りではない）
        except (OSError, ValueError) as e:
            self._note("gdb → ターゲット", e)
        self._close("gdb")

    def _from_target(self) -> None:
        try:
            while data := self._proc.stdout.read1(4096):
                self._conn.sendall(data)
                self.bytes_from_target += len(data)
        except (OSError, ValueError) as e:
            self._note("ターゲット → gdb", e)
        self._close("target")

    def status(self) -> dict:
        with self._lock:
            proc, conn, closed = self._proc, self._conn, self.closed_by is not None
        running = proc is not None and proc.poll() is None
        return {"port": self.port, "connected": conn is not None and not closed, "target_running": running,
                "exit_code": None if proc is None else proc.poll(), "bytes_to_target": self.bytes_to_target,
                "bytes_from_target": self.bytes_from_target, "closed_by": self.closed_by, "error": self.error,
                "stderr_tail": self._stderr.decode(errors="replace")}

    def stop(self) -> dict:
        try:
            self._listen.shutdown(socket.SHUT_RDWR)           # Linux では close だけでは accept が起きない
        except OSError:
            pass
        try:
            self._listen.close()
        except OSError:
            pass
        self._close("stop")
        return self.status()
