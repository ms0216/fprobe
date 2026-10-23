"""ターゲットのエージェント（agent/fprobe_agent.c）の導入と、SSH の中の JSON の行のやりとり（道 A）。

要求 {"id", "op", ...} → 返事 {"id", "ok", ...}。出来事 {"ev", ...} は on_event に渡す（読む側のスレッドから）。"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import threading
from collections.abc import Callable
from pathlib import Path

from .ssh import SshTarget

SOURCE = Path(__file__).with_name("agent") / "fprobe_agent.c"
REMOTE_DIR = ".fprobe"                               # ターゲットの利用者のホームからの相対
BINARY = f"{REMOTE_DIR}/fprobe_agent"
STDERR_TAIL = 4096


def deploy(ssh: SshTarget) -> dict:
    """源を置いてターゲットの gcc で作る。{binary, compiler, source_sha256}。gcc が無ければ RuntimeError。"""
    source = SOURCE.read_bytes()
    r = ssh.run("command -v gcc && gcc --version | head -n 1")
    if r.returncode == 255:                          # ssh そのものの失敗（gcc の有無と取り違えない）
        raise RuntimeError(f"{ssh.user}@{ssh.host} に入れない: {r.stderr.decode(errors='replace').strip()}")
    lines = r.stdout.decode(errors="replace").splitlines()
    if r.returncode or not lines:
        raise RuntimeError("ターゲットに gcc が無い（エージェントを作れない。クロスでの作り方は段階 2）")
    ssh.run(f"mkdir -p {REMOTE_DIR}").check_returncode()
    ssh.put(SOURCE, f"{BINARY}.c")
    r = ssh.run(f"gcc -O2 -Wall -o {BINARY} {BINARY}.c", timeout=120)
    if r.returncode:
        raise RuntimeError(f"エージェントを作れなかった: {r.stderr.decode(errors='replace').strip()}")
    return {"binary": BINARY, "compiler": lines[-1], "source_sha256": hashlib.sha256(source).hexdigest()}


class Agent:
    def __init__(self, ssh: SshTarget, binary: str, sudo: bool, on_event: Callable[[dict], None],
                 sudo_input: bytes | None = None):
        """sudo_input があれば sudo -S で起動し、それ（パスワードの行）を stdin の最初に送る。無ければ sudo -n。"""
        self.ssh, self.binary, self.sudo, self.on_event = ssh, binary, sudo, on_event
        self.sudo_input = sudo_input
        self._proc = None
        self._lock = threading.Lock()
        self._next = 0
        self._pending: dict[int, list] = {}          # id → [threading.Event, 返事]
        self._stderr: collections.deque[bytes] = collections.deque()
        self._stderr_len = 0
        self.exit_code: int | None = None
        self.event_errors = 0                        # on_event が例外を出した数（その出来事は失われた）
        self.last_event_error: str | None = None
        self.bad_lines = 0                           # JSON のオブジェクトでない行の数（捨てた）
        self._stopping = threading.Event()
        self.watchdog_s: float | None = None         # 効いている watchdog（status に出す）
        self._write_lock = threading.Lock()          # 要求と ping の行を混ぜない

    def start(self, watchdog_s: float | None = None) -> dict:
        """起動して hello の返事を返す。watchdog_s を渡すと、エージェントは入力（要求・ping）がその秒数来なければデーモンが
        いなくなったとみなす（linger のリングがあれば ptrace しているプロセスが終わるまで続け、無ければ bp を戻して終わる）。
        デーモンはその 3 分の 1 ごとに ping を送る。"""
        if watchdog_s is not None and not (math.isfinite(watchdog_s) and watchdog_s > 0):
            raise ValueError(f"watchdog_s は正の有限の秒数（{watchdog_s!r}）")
        prefix = "sudo -S -k -p '' " if self.sudo_input else ("sudo -n " if self.sudo else "")
        self._proc = self.ssh.open(f"{prefix}{self.binary}")
        if self.sudo_input:
            self._proc.stdin.write(self.sudo_input)
            self._proc.stdin.flush()
            self.sudo_input = None                           # 書いた後は持たない
        threading.Thread(target=self._read_out, daemon=True, name="linux-agent-out").start()
        threading.Thread(target=self._read_err, daemon=True, name="linux-agent-err").start()
        hello = self.request("hello")
        if watchdog_s is not None:
            try:
                self.request("watchdog", seconds=watchdog_s)
            except RuntimeError as e:                        # watchdog を知らない古いエージェント: watchdog なしで残さない
                self.stop()
                raise RuntimeError(f"エージェントが watchdog を受け付けない（{e}。linux.agent.deploy で作り直す）") from e
            self.watchdog_s = watchdog_s
            threading.Thread(target=self._ping, args=(watchdog_s / 3,), daemon=True, name="linux-agent-ping").start()
        return hello

    def _ping(self, interval: float) -> None:
        while self._proc is not None and self._proc.poll() is None and not self._stopping.wait(interval):
            try:
                with self._write_lock:
                    self._proc.stdin.write(b'{"id":0,"op":"ping"}\n')    # id 0: 返事を待たない
                    self._proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _read_out(self) -> None:
        for raw in self._proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                self.bad_lines += 1                  # 捨てた数を status に出す
                continue
            if "ev" in msg:                          # 出来事を先に見る（出来事の中身の名前と取り違えない）
                try:
                    self.on_event(msg)
                except Exception as e:               # noqa: BLE001  受け手の誤りで読む側を止めない（数と最後の誤りを出す）
                    self.event_errors += 1
                    self.last_event_error = f"{type(e).__name__}: {e}"
            elif "id" in msg:
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot:
                    slot[1] = msg
                    slot[0].set()
        self.exit_code = self._proc.wait()
        with self._lock:
            pending, self._pending = self._pending, {}
        for slot in pending.values():                # 待っている要求を終わらせる（返事無し = 落ちた）
            slot[0].set()

    def _read_err(self) -> None:
        for raw in self._proc.stderr:
            with self._lock:
                self._stderr.append(raw)
                self._stderr_len += len(raw)
                while self._stderr_len > STDERR_TAIL and len(self._stderr) > 1:
                    self._stderr_len -= len(self._stderr.popleft())

    def request(self, op: str, timeout: float = 10, **args) -> dict:
        if self._proc is None or self.exit_code is not None:
            raise RuntimeError(f"エージェントが動いていない（{self._why()}）")
        with self._lock:
            self._next += 1
            rid = self._next
            slot = self._pending[rid] = [threading.Event(), None]
        try:
            with self._write_lock:
                self._proc.stdin.write((json.dumps({"id": rid, "op": op, **args}, ensure_ascii=False) + "\n").encode())
                self._proc.stdin.flush()
        except OSError as e:
            raise RuntimeError(f"エージェントに送れない: {e}（{self._why()}）") from e
        if not slot[0].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise RuntimeError(f"エージェントが {timeout} 秒で {op} に答えなかった")
        reply = slot[1]
        if reply is None:
            raise RuntimeError(f"エージェントが {op} の途中で終わった（{self._why()}）")
        if not reply.get("ok"):
            raise RuntimeError(f"エージェント: {reply.get('error')}")
        return reply

    def _why(self) -> str:
        st = self.status()
        return f"終了コード {st['exit_code']}、stderr: {st['stderr_tail'].strip()[-200:]}"

    def status(self) -> dict:
        with self._lock:
            tail = b"".join(self._stderr).decode(errors="replace")
        code = self._proc.poll() if self._proc is not None else None
        return {"running": self._proc is not None and code is None, "exit_code": code, "stderr_tail": tail,
                "watchdog_s": self.watchdog_s, "event_errors": self.event_errors, "last_event_error": self.last_event_error, "bad_lines": self.bad_lines}

    def stop(self) -> None:
        self._stopping.set()
        if self._proc is None:
            return
        if self.exit_code is None:
            try:
                self.request("quit", timeout=3)
            except RuntimeError:
                pass
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(5)
        except Exception:                            # noqa: BLE001  終わらなければ止める
            self._proc.kill()
