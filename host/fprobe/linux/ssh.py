"""SSH・scp を子のプロセスで使う（Windows 標準の OpenSSH。鍵は利用者の ~/.ssh と ssh-agent。新しい依存を足さない）。

実行するファイルは環境変数 FPROBE_SSH / FPROBE_SCP で差し替えられる（.py なら Python で起動する: 試験の偽物）。"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CONNECT_TIMEOUT_S = 10                 # 接続を待つ上限（argv に見える）


class SshTimeout(RuntimeError):
    """時間切れ。途中までの stdout・stderr を持つ（捨てない）"""

    def __init__(self, message: str, stdout: bytes, stderr: bytes):
        super().__init__(message)
        self.stdout, self.stderr = stdout or b"", stderr or b""


def _exe(env: str, default: str) -> list[str]:
    path = os.environ.get(env, default)
    return [sys.executable, path] if path.endswith(".py") else [path]


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str
    port: int = 22
    key: str | None = None

    def __post_init__(self) -> None:
        # 目的地 user@host は ssh・scp の 1 つの引数。- で始まると ssh のオプション（-oProxyCommand=… は PC で命令を走らせる）に
        # なり、空白・制御文字は引数・scp の user@host:path を割る。どちらも断る（名前の形の推定はしない: 断るのはこの 2 つだけ）
        for what, value in (("user", self.user), ("host", self.host)):
            if not value or value.startswith("-") or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in value):
                raise ValueError(f"ssh の {what} {value!r} は空でなく、- で始まらず、空白・制御文字を含まない")

    def _options(self, port_flag: str) -> list[str]:
        out = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", port_flag, str(self.port)]
        return out + (["-i", self.key] if self.key else [])

    def argv(self, remote_cmd: str) -> list[str]:
        return _exe("FPROBE_SSH", "ssh") + self._options("-p") + [f"{self.user}@{self.host}", remote_cmd]

    def run(self, remote_cmd: str, input: bytes | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(self.argv(remote_cmd), input=input, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise SshTimeout(f"{self.user}@{self.host} が {timeout} 秒で終わらなかった: {remote_cmd[:80]}",
                             e.stdout, e.stderr) from e

    def open(self, remote_cmd: str) -> subprocess.Popen:
        return subprocess.Popen(self.argv(remote_cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def _scp(self, src: str, dst: str, timeout: float) -> None:
        try:
            r = subprocess.run(_exe("FPROBE_SCP", "scp") + self._options("-P") + [src, dst],
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"scp が {timeout} 秒で終わらなかった: {src} → {dst}") from e
        if r.returncode:
            raise RuntimeError(f"scp が失敗した（{r.returncode}）: {r.stderr.decode(errors='replace').strip()}")

    def put(self, local: Path, remote: str, timeout: float = 60) -> None:
        self._scp(str(local), f"{self.user}@{self.host}:{remote}", timeout)

    def get(self, remote: str, local: Path, timeout: float = 120) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._scp(f"{self.user}@{self.host}:{remote}", str(local), timeout)


class TargetHalted(RuntimeError):
    """対象の全コアが止まっている（linux.halt）: ssh・エージェントは答えないので叩かない。"""


class GuardedSsh:
    """ssh・scp・エージェントの口を開く前に、対象が止まっていないかを確かめる（止まっていれば叩かずに TargetHalted。
    止まった対象の ssh は ConnectTimeout まで作業スレッドを塞いでから失敗する: 2026-11-03 実機）。ほかは SshTarget のまま。"""

    def __init__(self, ssh: SshTarget, halted: Callable[[], str | None]):
        self._ssh, self._halted = ssh, halted

    def __getattr__(self, name: str):
        return getattr(self._ssh, name)

    def check(self) -> None:
        reason = self._halted()
        if reason is not None:
            raise TargetHalted(reason)

    def run(self, remote_cmd: str, input: bytes | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        self.check()
        return self._ssh.run(remote_cmd, input=input, timeout=timeout)

    def open(self, remote_cmd: str) -> subprocess.Popen:
        self.check()
        return self._ssh.open(remote_cmd)

    def put(self, local: Path, remote: str, timeout: float = 60) -> None:
        self.check()
        self._ssh.put(local, remote, timeout)

    def get(self, remote: str, local: Path, timeout: float = 120) -> None:
        self.check()
        self._ssh.get(remote, local, timeout)
